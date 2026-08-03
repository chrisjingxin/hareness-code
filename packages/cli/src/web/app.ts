/** 浏览器表现层：lifecycle-first 接管 bootstrap 与最小 Web 聊天行为。 */

import {
  Capability,
  EventType,
  MAX_FRAME_BYTES,
  Method,
  PROTOCOL_VERSION,
  type AgentEvent,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type JsonRpcMessage,
} from "@za38/protocol"

import { AgentClient, type AgentRun } from "../ipc/client"
import { AsyncQueue, type RpcTransport } from "../ipc/transport"
import {
  parseBootstrapFragment,
  validateAgentEndpoint,
} from "./bootstrap-url"
import { createBrowserConnectionSupervisor } from "./connection-supervisor"
import type { LifecycleBrowserMessage } from "./handoff-coordinator"

class WebSocketRpcTransport implements RpcTransport {
  private readonly queue = new AsyncQueue<unknown>()
  readonly messages: AsyncIterable<unknown> = this.queue

  constructor(private readonly socket: WebSocket) {
    socket.addEventListener("message", event => {
      try {
        this.queue.push(JSON.parse(String(event.data)))
      } catch {
        this.queue.push(event.data)
      }
    })
    socket.addEventListener("close", () => this.queue.end())
    socket.addEventListener("error", () => this.queue.fail(new Error("AgentHost WebSocket failed")))
  }

  async send(message: JsonRpcMessage): Promise<void> {
    const text = JSON.stringify(message)
    if (new TextEncoder().encode(text).byteLength > MAX_FRAME_BYTES) {
      throw new Error(`JSON-RPC frame exceeds ${MAX_FRAME_BYTES} bytes`)
    }
    if (this.socket.readyState !== WebSocket.OPEN) throw new Error("AgentHost WebSocket is closed")
    this.socket.send(text)
  }

  async close(): Promise<void> {
    this.socket.close()
    this.queue.end()
  }
}

/** 1. 在任何 socket 操作前同步解析 fragment 并清除 hash。 */
const bootstrap = parseBootstrapFragment(location.hash)
history.replaceState(null, "", location.pathname)
const handoffMatch = location.pathname.match(/^\/web\/h\/([^/]+)$/)
if (!bootstrap) throw new Error("Attachment URL is incomplete")
if (!handoffMatch) throw new Error("Invalid handoff path")
if (!validateAgentEndpoint(bootstrap.endpoint)) {
  throw new Error("Attachment endpoint is not a local loopback")
}
const { endpoint, token, attachmentId, threadId: initialThread } = bootstrap
const handoffId = handoffMatch[1]

/** 3. 连接 lifecycle；accepted 前不创建 Agent socket。 */
const lifecycle = new WebSocket(`ws://${location.host}/web/h/${handoffId}/lifecycle`)
const supervisor = createBrowserConnectionSupervisor(lifecycle)
window.addEventListener("pagehide", () => supervisor.abort("pagehide"))
lifecycle.addEventListener("close", () => {
  setStatus("已与 TUI 断开")
  setEnabled(false)
})

void main().catch(error => {
  supervisor.abort("bootstrap-failed")
  setStatus(errorMessage(error))
  setEnabled(false)
})

async function main(): Promise<void> {
  await supervisor.waitForAccepted()
  const socket = await authenticate(endpoint, token, supervisor.signal)
  const client = new AgentClient(new WebSocketRpcTransport(socket))
  supervisor.bindAgent(() => client.destroy())
  client.handleInteractions(handleInteraction)
  await client.initialize({
    protocol: {
      major: PROTOCOL_VERSION.major,
      min_minor: 0,
      max_minor: PROTOCOL_VERSION.minor,
    },
    client: { name: "harness-web", version: "0.1.0", kind: "web" },
    capabilities: {
      requests: [
        Capability.HOST_CONTROL,
        Capability.RUN_CANCEL,
        Capability.THREADS_READ,
        Capability.CONFIG_READ,
        Capability.CONTEXT_MANAGE,
        Capability.SKILLS_READ,
        Capability.MODELS_READ,
        Capability.MODELS_SELECT,
      ],
      handles: ["approval", "question"],
    },
  })
  const control = await client.request(Method.HOST_CONTROL_ACQUIRE, {})
  if (
    control.state !== "attached"
    || control.holder.attachment_id !== attachmentId
  ) {
    throw new Error("Host control was not acquired")
  }

  /** 4. 有初始 Thread 用 threads.open 恢复历史；无 Thread 渲染空首页。 */
  let threadId: string | null = initialThread
  if (threadId !== null) {
    const opened = await client.openThread(threadId)
    for (const message of opened.messages) appendMessage(message.kind, message.content)
  }

  /** 5. 安装页面 handler 后再发 ready。 */
  installComposer(client, () => threadId, value => { threadId = value })
  installReturnButton(client)
  lifecycleSend({ type: "ready" })
  setStatus(threadId === null ? "空首页 · 已就绪" : "已就绪")
  setEnabled(true)
}

function installComposer(
  client: AgentClient,
  currentThread: () => string | null,
  setThread: (threadId: string) => void,
): void {
  const form = document.querySelector<HTMLFormElement>("#composer")!
  const textarea = document.querySelector<HTMLTextAreaElement>("#prompt")!
  let activeRun: AgentRun | undefined

  form.addEventListener("submit", event => {
    event.preventDefault()
    const message = textarea.value.trim()
    if (!message || activeRun) return
    textarea.value = ""
    appendMessage("user", message)
    setEnabled(false)
    setStatus("运行中")
    const run = client.startRun({ message, threadId: currentThread() ?? undefined })
    activeRun = run
    void run.accepted.then(() => {
      // 空首页第一条 Run 创建 Thread，并通知 CLI 当前 Thread 变化。
      if (currentThread() === null) {
        setThread(run.ref.threadId)
        lifecycleSend({ type: "thread.changed", thread_id: run.ref.threadId })
      }
    })
    void consumeRun(run)
    void waitForRun(run, () => { activeRun = undefined })
  })
  textarea.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      form.requestSubmit()
    }
  })
}

function installReturnButton(client: AgentClient): void {
  const button = document.querySelector<HTMLButtonElement>("#return")!
  button.addEventListener("click", async () => {
    if (button.disabled) return
    button.disabled = true
    try {
      const status = await client.request(Method.HOST_CONTROL_RELEASE, {})
      if (status.state !== "owner") throw new Error("Host holder is not owner")
      lifecycleSend({ type: "released" })
      setStatus("已归还控制权")
      setEnabled(false)
    } catch (error) {
      button.disabled = false
      setStatus(errorMessage(error))
    }
  })
}

async function consumeRun(run: AgentRun): Promise<void> {
  try {
    for await (const event of run.events) {
      if (event.type === EventType.CONTENT_DELTA) appendDelta(event.payload.text)
      if (event.type === EventType.TOOL_STARTED) {
        appendMessage("tool", `调用工具：${event.payload.name}`)
      }
    }
  } catch (error) {
    setStatus(errorMessage(error))
  }
}

async function waitForRun(run: AgentRun, clear: () => void): Promise<void> {
  try {
    await run.accepted
    await run.completion
  } catch (error) {
    setStatus(errorMessage(error))
  } finally {
    clear()
    setEnabled(true)
    setStatus("已就绪")
  }
}

/** 认证 Agent attachment；abort 时立即关闭 socket。 */
function authenticate(url: string, credential: string, signal: AbortSignal): Promise<WebSocket> {
  const socket = new WebSocket(url)
  return new Promise<WebSocket>((resolve, reject) => {
    let done = false
    const cleanup = () => {
      signal.removeEventListener("abort", onAbort)
      socket.removeEventListener("open", onOpen)
      socket.removeEventListener("message", onMessage)
      socket.removeEventListener("close", onClose)
      socket.removeEventListener("error", onError)
    }
    const settle = (callback: () => void) => () => {
      if (done) return
      done = true
      cleanup()
      callback()
    }
    const onAbort = () => {
      done = true
      cleanup()
      socket.close()
      reject(new Error("Attachment authentication aborted"))
    }
    const onOpen = () => socket.send(JSON.stringify({ type: "auth", token: credential }))
    const onMessage = (event: MessageEvent) => {
      let message: unknown
      try {
        message = JSON.parse(String(event.data))
      } catch {
        settle(() => reject(new Error("Attachment authentication failed")))()
        return
      }
      if (isRecord(message) && message.type === "ready") {
        settle(() => resolve(socket))()
      } else {
        settle(() => reject(new Error("Attachment authentication failed")))()
      }
    }
    const onClose = () => settle(() => reject(new Error("AgentHost socket closed")))()
    const onError = () => settle(() => reject(new Error("无法连接 AgentHost")))()
    socket.addEventListener("open", onOpen, { once: true })
    socket.addEventListener("message", onMessage)
    socket.addEventListener("close", onClose)
    socket.addEventListener("error", onError)
    signal.addEventListener("abort", onAbort, { once: true })
  })
}

async function handleInteraction(request: InteractionRequestEnvelope): Promise<InteractionResponse> {
  if (request.type === "approval") {
    return {
      type: "approval",
      request_id: request.request_id,
      decision: confirm(request.payload.description) ? "approve_once" : "reject",
    }
  }
  const answers: Record<string, string[]> = {}
  for (const question of request.payload.questions) {
    const answer = prompt(question.question)
    answers[question.id] = answer === null ? [] : [answer]
  }
  return { type: "question", request_id: request.request_id, answers }
}

function lifecycleSend(message: LifecycleBrowserMessage): void {
  if (lifecycle.readyState === WebSocket.OPEN) {
    lifecycle.send(JSON.stringify(message))
  }
}

function appendMessage(kind: string, content: string): void {
  const article = document.createElement("article")
  article.className = kind
  article.textContent = content
  document.querySelector("#messages")!.append(article)
  article.scrollIntoView({ block: "end" })
}

function appendDelta(text: string): void {
  const messages = document.querySelector("#messages")!
  let article = messages.lastElementChild as HTMLElement | null
  if (!article || !article.classList.contains("assistant")) {
    appendMessage("assistant", "")
    article = messages.lastElementChild as HTMLElement
  }
  article.textContent += text
}

function setStatus(value: string): void {
  document.querySelector("#status")!.textContent = value
}

function setEnabled(enabled: boolean): void {
  document.querySelector<HTMLTextAreaElement>("#prompt")!.disabled = !enabled
  document.querySelector<HTMLButtonElement>("#send")!.disabled = !enabled
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
