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
import { AgentClientInteractiveAdapter } from "../interactive/agent-port"
import { createInteractiveController } from "../interactive/controller"
import { createInteractiveRuntime } from "../interactive/runtime"
import { createWebInteractiveAdapter, type WebInteractiveAdapter } from "./application/adapter"
import { WebSocketRpcTransport, authenticate } from "./agent-transport"
import { parseBootstrapFragment, validateAgentEndpoint } from "./bootstrap-url"
import { createBrowserConnectionSupervisor } from "./connection-supervisor"
import { createWebHandoffPort, type WebHandoffPort } from "./handoff-port"
import { PresentationErrorBoundary } from "./presentation/error-boundary"
import { WebApp } from "./presentation/web-app"
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

  const closeGate = async (message?: string): Promise<void> => {
    if (closed) return
    closed = true
    await adapter?.close()
    root?.unmount()
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
    await supervisor.waitForAccepted()
    const socket = await authenticate(bootstrap.endpoint, bootstrap.token, supervisor.signal)
    client = new AgentClient(new WebSocketRpcTransport(socket))
    supervisor.bindAgent(() => client?.destroy())
    client.on("close", () => supervisor.abort("agent-closed"))
    client.handleInteractions(() => Promise.reject(new Error("Interaction handler is not ready")))
    const initialized = await client.initialize({
      protocol: {
        major: PROTOCOL_VERSION.major,
        min_minor: 0,
        max_minor: PROTOCOL_VERSION.minor,
      },
      client: { name: "harness-web", version: "0.1.0", kind: "web" },
      capabilities: { requests: [...WEB_CAPABILITIES], handles: ["approval", "question"] },
    })
    const control = await client.request(Method.HOST_CONTROL_ACQUIRE, {})
    if (control.state !== "attached" || control.holder.attachment_id !== bootstrap.attachmentId) {
      throw new Error("Host control was not acquired")
    }

    const runtime = createInteractiveRuntime(initialized, workspaceFromInitialize(initialized), { cliVersion: "0.1.0" })
    const agent = new AgentClientInteractiveAdapter(client)
    controller = createInteractiveController({ agent, runtime })
    if (bootstrap.threadId !== null) {
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
    root = createRoot(rootElement)
    const onActivationFailure = () => {
      void closeGate("Web 接管确认失败，请返回 TUI 后重新执行 /web。")
    }
    root.render(<WebBootstrapRoot adapter={adapter} handoff={handoff} onFailure={onActivationFailure} />)
  } catch {
    await closeGate("Web 接管失败，请返回 TUI 后重新执行 /web。")
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
