import { describe, expect, it } from "bun:test"
import { processHighlightRequest } from "../../../src/web/syntax/worker"

describe("Shiki Syntax Worker", () => {
  it("应该能对支持的语言（JS/TS/Python/JSON/Bash/HTML）正确高亮并产出 Spans", async () => {
    const jsRes = await processHighlightRequest({
      requestId: 1,
      language: "javascript",
      code: "const greeting = 'hello';",
    })
    expect(jsRes.type).toBe("highlighted")
    if (jsRes.type === "highlighted") {
      expect(jsRes.spans.length).toBeGreaterThan(0)
    }

    const pyRes = await processHighlightRequest({
      requestId: 2,
      language: "py",
      code: "def add(a, b):\n  return a + b",
    })
    expect(pyRes.type).toBe("highlighted")

    const jsonRes = await processHighlightRequest({
      requestId: 3,
      language: "json",
      code: '{"name": "harness"}',
    })
    expect(jsonRes.type).toBe("highlighted")
  })

  it("应该对未知语言安全降级为 plain 且带有 unknown-language 原因", async () => {
    const unknownRes = await processHighlightRequest({
      requestId: 4,
      language: "some-nonexistent-language-12345",
      code: "some code",
    })
    expect(unknownRes.type).toBe("plain")
    if (unknownRes.type === "plain") {
      expect(unknownRes.reason).toBe("unknown-language")
    }
  })

  it("应该对超长代码块安全降级为 plain 且带有 too-large 原因", async () => {
    const hugeCode = "const a = 1;\n".repeat(2005)
    const largeRes = await processHighlightRequest({
      requestId: 5,
      language: "js",
      code: hugeCode,
    })
    expect(largeRes.type).toBe("plain")
    if (largeRes.type === "plain") {
      expect(largeRes.reason).toBe("too-large")
    }
  })
})
