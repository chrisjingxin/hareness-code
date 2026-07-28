/** 浏览器表现层：通过 WebSocket adapter 复用同一个 v3 AgentClient。 */

import {
  Capability,
  EventType,
  MAX_FRAME_BYTES,
  PROTOCOL_VERSION,
  type AgentEvent,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type JsonRpcMessage,
} from "@za38/protocol"

import { AgentClient, type AgentRun } from "../ipc/client"
import { AsyncQueue, type RpcTransport } from "../ipc/transport"

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

const fragment = new URLSearchParams(location.hash.slice(1))
history.replaceState(null, "", location.pathname)
const endpoint = fragment.get("endpoint")
const token = fragment.get("token")
const initialThread = fragment.get("thread")
if (!endpoint || !token || !initialThread) throw new Error("Attachment URL is incomplete")

const socket = await authenticate(endpoint, token)
const client = new AgentClient(new WebSocketRpcTransport(socket))
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
      Capability.RUN_CANCEL,
      Capability.RUN_MULTITHREAD,
      Capability.CONFIG_READ,
      Capability.THREADS_READ,
      Capability.CONTEXT_MANAGE,
      Capability.SKILLS_READ,
      Capability.MODELS_READ,
      Capability.MODELS_SELECT,
    ],
    handles: ["approval", "question"],
  },
})

const watch = await client.watchThread(initialThread)
const threadId = watch.snapshot.thread.thread_id
let activeRun: AgentRun | undefined
for (const message of watch.snapshot.messages) appendMessage(message.kind, message.content)
void consumeWatch(watch.events)
setStatus("已连接")
setEnabled(true)

const form = document.querySelector<HTMLFormElement>("#composer")!
const textarea = document.querySelector<HTMLTextAreaElement>("#prompt")!
form.addEventListener("submit", event => {
  event.preventDefault()
  const message = textarea.value.trim()
  if (!message || activeRun) return
  textarea.value = ""
  appendMessage("user", message)
  setEnabled(false)
  setStatus("运行中")
  const run = client.startRun({ message, threadId })
  activeRun = run
  void drain(run.events)
  void waitForRun(run)
})
textarea.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault()
    form.requestSubmit()
  }
})

async function authenticate(url: string, credential: string): Promise<WebSocket> {
  const socket = new WebSocket(url)
  await new Promise<void>((resolve, reject) => {
    socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "auth", token: credential })), { once: true })
    socket.addEventListener("error", () => reject(new Error("无法连接 AgentHost")), { once: true })
    socket.addEventListener("message", event => {
      try {
        if (JSON.parse(String(event.data)).type === "ready") resolve()
        else reject(new Error("Attachment authentication failed"))
      } catch {
        reject(new Error("Attachment authentication failed"))
      }
    }, { once: true })
  })
  return socket
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

async function consumeWatch(events: AsyncIterable<AgentEvent>): Promise<void> {
  try {
    for await (const event of events) {
      if (event.thread_id !== threadId) continue
      if (event.type === EventType.CONTENT_DELTA) appendDelta(event.payload.text)
      if (event.type === EventType.TOOL_STARTED) appendMessage("tool", `调用工具：${event.payload.name}`)
      if (event.type === EventType.RUN_COMPLETED || event.type === EventType.RUN_CANCELLED || event.type === EventType.RUN_FAILED) {
        setStatus(event.type)
      }
    }
  } catch (error) {
    setStatus(errorMessage(error))
    setEnabled(false)
  }
}

async function waitForRun(run: AgentRun): Promise<void> {
  try {
    await run.accepted
    await run.completion
  } catch (error) {
    setStatus(errorMessage(error))
  } finally {
    if (activeRun === run) activeRun = undefined
    setEnabled(true)
  }
}

async function drain(events: AsyncIterable<AgentEvent>): Promise<void> {
  try {
    for await (const _event of events) {
      // ThreadWatch 是页面唯一渲染源；这里仅消费 AgentRun 队列避免重复展示。
    }
  } catch {
    // waitForRun 负责展示同一个连接错误。
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
