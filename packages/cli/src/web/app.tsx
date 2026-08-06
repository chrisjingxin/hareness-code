/** Web composition root：按 UI token → gateway WS → 视图缓存 → React 的顺序启动。 */
/** @jsxImportSource react */

import { createRoot, type Root } from "react-dom/client"
import { useLayoutEffect, useState } from "react"

import { createWebInteractiveAdapter, type WebInteractiveAdapter } from "./application/adapter"
import { PresentationErrorBoundary } from "./presentation/error-boundary"
import { WebApp } from "./presentation/web-app"
import { closeHighlightService } from "./syntax/highlight-service"
import { createWebUiClient, readUiToken, type WebUiClient } from "./ui-client"
import type { PresentationState } from "../presentation-coordinator"
import "./presentation/styles.css"

/**
 * 启动当前 handoff 页面；URL fragment 在创建任何 WebSocket 前被清除。
 * 该函数在无 DOM 的构建/测试环境中不会自动执行，便于检查 bundle。
 */
export async function bootstrapWebApp(): Promise<void> {
  const rootElement = document.querySelector<HTMLElement>("#root")
  if (!rootElement) throw new Error("Web root is missing")

  const handoffMatch = window.location.pathname.match(/^\/web\/h\/([^/]+)$/)
  // token 在创建 socket 前从 fragment 剥离并写入 sessionStorage；不进入 React props。
  const handoffId = handoffMatch?.[1]
  const token = handoffId ? readUiToken(handoffId) : undefined
  if (!handoffId || !token) {
    renderStatic(rootElement, "Web 接管链接无效，请返回 TUI 后重新执行 /web。", "fatal")
    return
  }

  let client: WebUiClient | undefined
  let adapter: WebInteractiveAdapter | undefined
  let root: Root | undefined
  let closed = false
  let resolveFirstState: (() => void) | undefined
  const firstState = new Promise<void>(resolve => { resolveFirstState = resolve })

  const closeGate = (message?: string): void => {
    if (closed) return
    closed = true
    void adapter?.close()
    root?.unmount()
    closeHighlightService()
    client?.close()
    if (message) renderStatic(rootElement, message, "closed")
  }

  let readyGate: ReturnType<typeof createReadyGate> | undefined
  client = createWebUiClient({
    socket: new WebSocket(
      `ws://${window.location.host}/web/h/${encodeURIComponent(handoffId)}/ui?ui=${encodeURIComponent(token)}`,
    ),
    onState: () => {
      // onState 在消息回调中触发，此时 readyGate 已赋值。
      readyGate?.onState()
      // 首帧到达即武装看门狗：覆盖 opening-web 帧与 replace 一起丢失的镜像场景
      // （页面停在默认 tui-active，handoff 订阅不触发，只能靠重载试探重连）。
      armWatchdog()
      const resolve = resolveFirstState
      resolveFirstState = undefined
      resolve?.()
    },
    onClosed: reason => {
      void closeGate("本次 Web 接管已结束。请返回 TUI 后重新执行 /web。")
      void reason
    },
  })

  // 接管确认看门狗：页面停留在 opening-web 超过宽限期时整页重载重连同一 handoff。
  // 覆盖两类单帧丢失：a) 页面已发 ready 但 web-active 帧被浏览器冻结/休眠/扩展
  // 干扰丢弃（服务端已 active，重连后网关首帧直接下发 web-active 恢复可写）；
  // b) 首帧不完整、ready 从未发出（服务端仍在 opening-web，旧连接关闭会让
  // Coordinator 立即收敛回 TUI，重连被拒后显示脱敏引导）。UI token 在 sessionStorage
  // 且 TTL 60s 内可复用，重载窗口远小于 TTL；限制重载次数防止服务端异常时死循环。
  const takeoverConfirmMs = 5_000
  const maxReloads = 3
  const reloadKey = `harness-takeover-reloads:${handoffId}`
  let watchdogTimer: ReturnType<typeof setTimeout> | undefined
  let reloadCount = 0
  try {
    reloadCount = Number(sessionStorage.getItem(reloadKey) ?? 0) || 0
  } catch {
    // sessionStorage 不可用时看门狗照常工作，只是不累计重载次数。
  }
  const cancelWatchdog = (): void => {
    if (watchdogTimer !== undefined) {
      clearTimeout(watchdogTimer)
      watchdogTimer = undefined
    }
  }
  const armWatchdog = (): void => {
    if (watchdogTimer !== undefined) return
    watchdogTimer = setTimeout(() => {
      watchdogTimer = undefined
      const phase = client?.getHandoffState().phase
      // opening-web 与从未收到 handoff.state 的默认 tui-active 都在重载范围内；
      // 任何真实 handoff.state（web-active/returning-tui/tui-active 收敛）都会先
      // 通过订阅取消看门狗。
      if (phase !== "opening-web" && phase !== "tui-active") return
      reloadCount += 1
      if (reloadCount > maxReloads) return
      try {
        sessionStorage.setItem(reloadKey, String(reloadCount))
      } catch {
        // 存储失败不阻止重载恢复。
      }
      window.location.reload()
    }, takeoverConfirmMs)
  }

  readyGate = createReadyGate(client)
  client.subscribeHandoff(state => {
    readyGate!.onHandoffState()
    if (state.phase === "opening-web") armWatchdog()
    else cancelWatchdog()
  })

  window.addEventListener("pagehide", () => { void closeGate() }, { once: true })

  // 首帧 state.replace 到达前不渲染业务树；失败只显示通用文案，不暴露端点或 token。
  await firstState
  if (closed) return

  adapter = createWebInteractiveAdapter({ client })
  root = createRoot(rootElement)
  const onFailure = () => {
    void closeGate("Web 接管确认失败，请返回 TUI 后重新执行 /web。")
  }
  root.render(<WebBootstrapRoot adapter={adapter} client={client} onFailure={onFailure} />)
}

/**
 * ready 上报门：只有"已收到首帧视图"且"处于 opening-web"才发送 handoff.ready。
 * 服务端按 state.replace → handoff.state 顺序首帧，因此 handoff.state(opening-web)
 * 到达时视图必已就绪；重连（web-active）与已发送过 ready 时都保持静默。
 */
export function createReadyGate(client: Pick<WebUiClient, "ready" | "getHandoffState">): {
  onState(): void
  onHandoffState(): void
} {
  let haveState = false
  let sent = false
  const maybeSendReady = (): void => {
    if (sent || !haveState) return
    if (client.getHandoffState().phase !== "opening-web") return
    sent = true
    client.ready()
  }
  return {
    onState() {
      haveState = true
      maybeSendReady()
    },
    onHandoffState() {
      maybeSendReady()
    },
  }
}

/** 根组件：订阅 handoff 状态决定页面是否可交互；只读等待期间保持挂载。 */
function WebBootstrapRoot(props: { adapter: WebInteractiveAdapter; client: WebUiClient; onFailure: () => void }) {
  // 初始值从 client 当前状态读取：重连（如看门狗重载后）时 handoff.state 可能在
  // React 挂载前就已到达，仅靠订阅会永久错过 web-active。
  const [phase, setPhase] = useState<PresentationState["phase"]>(props.client.getHandoffState().phase)
  // 必须用 useLayoutEffect（同步提交）而不是 useEffect（被动效果异步 flush）：
  // 被动效果的订阅注册晚于下一次宏任务，handoff.state(web-active) 帧可落在
  // "挂载完成 → 订阅注册"的空隙里被永久错过（真实页面历史越多渲染越慢，空隙越大）。
  useLayoutEffect(() => {
    // 已收到 opening-web 即上报 ready；之后 phase 变化驱动 active 开关。
    return props.client.subscribeHandoff(state => setPhase(state.phase))
  }, [props.client])
  return (
    <PresentationErrorBoundary onError={() => props.onFailure()}>
      <WebApp adapter={props.adapter} active={phase === "web-active"} />
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

if (typeof window !== "undefined" && typeof document !== "undefined") {
  void bootstrapWebApp()
}
