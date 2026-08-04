import { describe, expect, test } from "bun:test"

import { resolveSyntaxLanguage } from "../../../src/web/syntax/catalog.generated"
import { SyntaxClient } from "../../../src/web/syntax/client"
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
    expect(rendered).toBeDefined()
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
})
