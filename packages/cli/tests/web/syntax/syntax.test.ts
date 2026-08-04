import { describe, expect, test } from "bun:test"
import { isValidElement } from "react"

import { resolveSyntaxLanguage } from "../../../src/web/syntax/catalog.generated"
import { acquireSyntaxClient, closeSyntaxClient, releaseSyntaxClient, SyntaxClient } from "../../../src/web/syntax/client"
import { captureToScope, processHighlightRequest } from "../../../src/web/syntax/worker"
import { renderSpans } from "../../../src/web/presentation/code-block"

describe("Web Syntax Catalog & Resolution", () => {
  test("准确解析主语言与别名", () => {
    expect(resolveSyntaxLanguage("python")?.filetype).toBe("python")
    expect(resolveSyntaxLanguage("py")?.filetype).toBe("python")
    expect(resolveSyntaxLanguage("c++")?.filetype).toBe("cpp")
    expect(resolveSyntaxLanguage("sh")?.filetype).toBe("bash")
    expect(resolveSyntaxLanguage("yml")?.filetype).toBe("yaml")
    expect(resolveSyntaxLanguage("unknown_lang")).toBeNull()
  })

  test("captureToScope 正确映射 query capture 节点", () => {
    expect(captureToScope("comment.line")).toBe("comment")
    expect(captureToScope("keyword.control")).toBe("keyword")
    expect(captureToScope("function.method")).toBe("function")
    expect(captureToScope("variable.parameter")).toBe("variable")
    expect(captureToScope("string.quoted")).toBe("string")
    expect(captureToScope("number.float")).toBe("number")
    expect(captureToScope("type.class")).toBe("type")
    expect(captureToScope("custom.unknown")).toBe("plain")
  })
})

describe("UTF-8 to UTF-16 Span Rendering", () => {
  test(" renderSpans 输出内容与原始包含多字节字符代码完全一致", () => {
    const code = "def hello():\n    # 中文注释\n    print('你好')"
    const encoder = new TextEncoder()
    const bytes = encoder.encode(code)

    // 假定一段 UTF-8 字节范围 (注释部分)
    const commentStartByte = encoder.encode("def hello():\n    ").length
    const commentEndByte = commentStartByte + encoder.encode("# 中文注释").length

    const spans = [
      { startByte: 0, endByte: 3, scope: "keyword" as const },
      { startByte: commentStartByte, endByte: commentEndByte, scope: "comment" as const },
    ]

    const rendered = renderSpans(code, spans)
    expect(renderedText(rendered)).toBe(code)
  })

  test("字节边界错误或重叠 span 整块 plain 降级，不截断或猜测优先级", () => {
    const code = "const 中文 = 1"
    const encoder = new TextEncoder()
    const chineseStart = encoder.encode("const ").length
    expect(renderSpans(code, [{ startByte: chineseStart + 1, endByte: chineseStart + 2, scope: "variable" }])).toBe(code)
    expect(renderSpans(code, [
      { startByte: 0, endByte: 5, scope: "keyword" },
      { startByte: 3, endByte: 7, scope: "variable" },
    ])).toBe(code)
  })
})

describe("SyntaxClient Fallback", () => {
  test("超出大小或无 Worker 环境平滑降级", async () => {
    const client = new SyntaxClient()
    const largeCode = "x".repeat(70 * 1024)
    const result = await client.highlight("python", largeCode)
    expect(result.type).toBe("plain")
    expect(result.reason).toBe("too-large")
    client.close()
  })

  test("超过 2,000 行在主线程与 Worker 入口都 plain 降级", async () => {
    const code = Array.from({ length: 2_001 }, () => "x").join("\n")
    const client = new SyntaxClient()
    const clientResult = await client.highlight("python", code)
    const workerResult = await processHighlightRequest({ requestId: 7, language: "python", code })
    expect(clientResult).toMatchObject({ type: "plain", reason: "too-large" })
    expect(workerResult).toMatchObject({ type: "plain", requestId: 7, reason: "too-large" })
    client.close()
  })

  test("close 终止 Worker、释放待决请求；全局 Worker 不可用时不泄漏", async () => {
    const originalWorker = globalThis.Worker
    const workers: FakeWorker[] = []
    globalThis.Worker = class extends FakeWorker {
      constructor(...args: ConstructorParameters<typeof FakeWorker>) {
        super(...args)
        workers.push(this)
      }
    } as unknown as typeof Worker
    try {
      const client = new SyntaxClient()
      const pending = client.highlight("python", "print('pending')")
      client.close()
      await expect(pending).resolves.toMatchObject({ type: "plain", reason: "load-failed" })
      expect(workers[0]?.terminated).toBe(true)
    } finally {
      globalThis.Worker = originalWorker
    }
  })

  test("最后一个 CodeBlock 释放共享 client 时终止 Worker", async () => {
    const originalWorker = globalThis.Worker
    const workers: FakeWorker[] = []
    globalThis.Worker = class extends FakeWorker {
      constructor(...args: ConstructorParameters<typeof FakeWorker>) {
        super(...args)
        workers.push(this)
      }
    } as unknown as typeof Worker
    try {
      const client = acquireSyntaxClient()
      const pending = client.highlight("python", "print('pending')")
      releaseSyntaxClient()
      await expect(pending).resolves.toMatchObject({ type: "plain", reason: "load-failed" })
      expect(workers[0]?.terminated).toBe(true)
    } finally {
      closeSyntaxClient()
      globalThis.Worker = originalWorker
    }
  })

  test("高 span 密度时按 4 MiB 上限淘汰 LRU，不只依赖条数", async () => {
    const originalWorker = globalThis.Worker
    const worker = new RespondingWorker()
    globalThis.Worker = class {
      constructor() { return worker }
    } as unknown as typeof Worker
    try {
      const client = new SyntaxClient()
      const firstCode = `0${"x".repeat(60 * 1024)}`
      await client.highlight("python", firstCode)
      for (let index = 1; index <= 60; index += 1) {
        await client.highlight("python", `${index}${"x".repeat(60 * 1024)}`)
      }
      const postsBeforeRetry = worker.postCount
      await client.highlight("python", firstCode)
      expect(worker.postCount).toBe(postsBeforeRetry + 1)
      client.close()
    } finally {
      globalThis.Worker = originalWorker
    }
  })
})

/** 按 React Node 结构取纯文本，用于验证 span 渲染不会损失 UTF-16 内容。 */
function renderedText(value: React.ReactNode): string {
  if (typeof value === "string" || typeof value === "number") return String(value)
  if (Array.isArray(value)) return value.map(renderedText).join("")
  if (isValidElement<{ children?: React.ReactNode }>(value)) return renderedText(value.props.children)
  return ""
}

/** 不回包的 Worker，用于验证 close 的生命周期处理。 */
class FakeWorker {
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: ErrorEvent) => void) | null = null
  terminated = false

  constructor(_url: string, _options?: WorkerOptions) {}

  postMessage(_message: unknown): void {}

  terminate(): void {
    this.terminated = true
  }
}

/** 同步回包的 Worker；每条响应故意附带大量 span 以覆盖字节容量淘汰。 */
class RespondingWorker extends FakeWorker {
  postCount = 0

  override postMessage(message: unknown): void {
    const request = message as { type?: string; requestId?: number; language?: string }
    if (request.type !== "highlight" || request.requestId === undefined || !request.language) return
    this.postCount += 1
    this.onmessage?.({
      data: {
        type: "highlighted",
        requestId: request.requestId,
        language: request.language,
        spans: Array.from({ length: 1_000 }, () => ({ startByte: 0, endByte: 1, scope: "keyword" })),
      },
    } as MessageEvent)
  }
}
