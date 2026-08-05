/** Web composition root：按 lifecycle → attachment → controller → React 的顺序启动。 */
/** @jsxImportSource react */

import {
  Capability,
  Method,
  PROTOCOL_VERSION,
} from "@za38/protocol"
import { createRoot, type Root } from "react-dom/client"
import { useEffect, useState } from "react"

import { AgentClient } from "../ipc/client"
import { AgentClientGateway } from "../infrastructure/agent-client-gateway"
import { createInteractiveController } from "../interactive/controller"
import { createInteractiveRuntime } from "../interactive/runtime"
import { createWebInteractiveAdapter, type WebInteractiveAdapter } from "./application/adapter"
import { WebSocketRpcTransport, authenticate } from "./agent-transport"
import { parseBootstrapFragment, validateAgentEndpoint } from "./bootstrap-url"
import { createBrowserConnectionSupervisor } from "./connection-supervisor"
import type { WebBootstrapStage } from "./handoff-coordinator"
import { createWebHandoffPort, type WebHandoffPort } from "./handoff-port"
import { PresentationErrorBoundary } from "./presentation/error-boundary"
import { WebApp } from "./presentation/web-app"
import { closeHighlightService } from "./syntax/highlight-service"
import "./presentation/styles.css"


const WEB_CAPABILITIES = [
  Capability.HOST_CONTROL,
  Capability.RUN_CANCEL,
  Capability.THREADS_READ,
  Capability.CONFIG_READ,
  Capability.CONFIG_WRITE,
  Capability.CONTEXT_MANAGE,
  Capability.MODELS_READ,
  Capability.MODELS_SELECT,
  Capability.SKILLS_READ,
  Capability.SKILLS_MANAGE,
  Capability.MCP_READ,
  Capability.MCP_MANAGE,
] as const

/**
 * 启动当前 handoff 页面；URL fragment 在创建任何 WebSocket 前被清除。
 * 该函数在无 DOM 的构建/测试环境中不会自动执行，便于检查 bundle。
 */
export async function bootstrapWebApp(): Promise<void> {
  const rootElement = document.querySelector<HTMLElement>("#root")
  if (!rootElement) throw new Error("Web root is missing")

  const bootstrap = parseBootstrapFragment(window.location.hash)
  // token、endpoint 和 attachment 永不进入 React props；hash 先清除再校验 socket。
  window.history.replaceState(null, "", window.location.pathname)
  const handoffMatch = window.location.pathname.match(/^\/web\/h\/([^/]+)$/)
  if (!bootstrap || !handoffMatch || !validateAgentEndpoint(bootstrap.endpoint)) {
    renderStatic(rootElement, "Web 接管链接无效，请返回 TUI 后重新执行 /web。", "fatal")
    return
  }

  const handoffId = handoffMatch[1]!
  const lifecycle = new WebSocket(`ws://${window.location.host}/web/h/${encodeURIComponent(handoffId)}/lifecycle`)
  const supervisor = createBrowserConnectionSupervisor(lifecycle)
  let client: AgentClient | undefined
  let controller: ReturnType<typeof createInteractiveController> | undefined
  let adapter: WebInteractiveAdapter | undefined
  let handoff: WebHandoffPort | undefined
  let root: Root | undefined
  let closed = false
  let stage: WebBootstrapStage = "lifecycle.accepted"

  const closeGate = async (message?: string): Promise<void> => {
    if (closed) return
    closed = true
    await adapter?.close()
    root?.unmount()
    closeHighlightService()
    await controller?.close()
    client?.destroy()
    handoff?.close()
    supervisor.abort("web-close")
    supervisor.dispose()
    if (message) renderStatic(rootElement, message, "closed")
  }

  supervisor.signal.addEventListener("abort", () => {
    void closeGate("本次 Web 接管已结束。请返回 TUI 后重新执行 /web。")
  }, { once: true })
  window.addEventListener("pagehide", () => { void closeGate() }, { once: true })

  try {
    // lifecycle accepted 之前不触碰 Python Agent endpoint。
    stage = "lifecycle.accepted"
    await supervisor.waitForAccepted()
    stage = "attachment.auth"
    const socket = await authenticate(bootstrap.endpoint, bootstrap.token, supervisor.signal)
    client = new AgentClient(new WebSocketRpcTransport(socket))
    supervisor.bindAgent(() => client?.destroy())
    client.on("close", () => supervisor.abort("agent-closed"))
    client.handleInteractions(() => Promise.reject(new Error("Interaction handler is not ready")))
    stage = "agent.initialize"
    const initialized = await client.initialize({
      protocol: {
        major: PROTOCOL_VERSION.major,
        min_minor: 0,
        max_minor: PROTOCOL_VERSION.minor,
      },
      client: { name: "harness-web", version: "0.1.0", kind: "web" },
      capabilities: { requests: [...WEB_CAPABILITIES], handles: ["approval", "question"] },
    })
    stage = "host.control.acquire"
    const control = await client.request(Method.HOST_CONTROL_ACQUIRE, {})
    if (control.state !== "attached" || control.holder.attachment_id !== bootstrap.attachmentId) {
      throw new Error("Host control was not acquired")
    }

    const runtime = createInteractiveRuntime(initialized, workspaceFromInitialize(initialized), { cliVersion: "0.1.0" })
    const gateway = new AgentClientGateway(client)
    controller = createInteractiveController({ gateway, runtime })
    if (bootstrap.threadId !== null) {
      stage = "thread.restore"
      await controller.dispatch({ type: "thread.open", threadId: bootstrap.threadId })
    }
    handoff = createWebHandoffPort({
      supervisor,
      sendLifecycle: async message => {
        if (lifecycle.readyState !== WebSocket.OPEN) throw new Error("Lifecycle socket is closed")
        lifecycle.send(JSON.stringify(message))
      },
      release: async () => {
        const released = await client!.request(Method.HOST_CONTROL_RELEASE, {})
        if (released.state !== "owner") throw new Error("Host control was not returned to TUI")
      },
    })
    adapter = createWebInteractiveAdapter({ controller, handoff })
    stage = "react.mount"
    root = createRoot(rootElement)
    const onActivationFailure = () => {
      void closeGate("Web 接管确认失败，请返回 TUI 后重新执行 /web。")
    }
    root.render(<WebBootstrapRoot adapter={adapter} handoff={handoff} onFailure={onActivationFailure} />)
  } catch (error) {
    // 页面只显示阶段，不显示 endpoint、token、原始 JSON-RPC 或企业路径；详细错误留在本地 DevTools。
    const safeError = safeBootstrapError(error)
    console.error("[harness-web] bootstrap failed", { stage, error: safeError })
    if (lifecycle.readyState === WebSocket.OPEN) {
      lifecycle.send(JSON.stringify({
        type: "diagnostic",
        stage,
        error_name: safeError.name,
        error_message: safeError.message,
      }))
    }
    await closeGate(`Web 接管失败（${stage}），请返回 TUI 后重新执行 /web。`)
  }
}

function safeBootstrapError(error: unknown): { name: string; message: string } {
  const name = error instanceof Error && error.name ? error.name : "Error"
  const rawMessage = error instanceof Error ? error.message : String(error)
  const redactedMessage = rawMessage
    .replaceAll(/wss?:\/\/[^\s"']+/gi, "[redacted-url]")
    .replaceAll(/\b(token|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi, "$1=[redacted]")
    .replaceAll(/(?:\/[A-Za-z0-9._~-]+){3,}/g, "[redacted-path]")
  return {
    name: name.slice(0, 80),
    message: redactedMessage.length > 200 ? `${redactedMessage.slice(0, 200)}…` : redactedMessage,
  }
}

function WebBootstrapRoot(props: { adapter: WebInteractiveAdapter; handoff: WebHandoffPort; onFailure: () => void }) {
  const [active, setActive] = useState(false)
  useEffect(() => {
    let mounted = true
    // 首次 React commit 后才报告最终 Thread；active ack 前页面保持只读。
    void props.handoff.activate(props.adapter.getSnapshot().interactive.currentThreadId)
      .then(() => {
        if (mounted) setActive(true)
      })
      .catch(() => {
        if (mounted) props.onFailure()
      })
    return () => { mounted = false }
  }, [props.adapter, props.handoff, props.onFailure])
  return (
    <PresentationErrorBoundary onError={() => props.onFailure()}>
      <WebApp adapter={props.adapter} active={active} />
    </PresentationErrorBoundary>
  )
}

function renderStatic(root: HTMLElement, message: string, kind: "fatal" | "closed"): void {
  root.replaceChildren()
  const container = document.createElement("main")
  container.className = `web-static-state ${kind}`
  const heading = document.createElement("h1")
  heading.textContent = kind === "fatal" ? "Web 接管不可用" : "Web 接管已结束"
  const detail = document.createElement("p")
  detail.textContent = message
  container.append(heading, detail)
  root.append(container)
}

function workspaceFromInitialize(value: { config_summary: Record<string, unknown> | null }): string {
  const workspace = value.config_summary?.workspace
  return typeof workspace === "string" && workspace ? workspace : "当前工作区"
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  void bootstrapWebApp()
}
