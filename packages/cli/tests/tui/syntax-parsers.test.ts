import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { expect, test } from "bun:test"
import { TreeSitterClient, infoStringToFiletype } from "@opentui/core"

import manifest from "../../src/tui/assets/syntax/manifest.json" with { type: "json" }
import {
  getBundledSyntaxParsers,
  registerCommonSyntaxParsers,
  SUPPORTED_SYNTAX_LANGUAGES,
} from "../../src/tui/syntax-parsers"

test("精简语言清单只包含企业常用栈与 OpenTUI 内置 parser", () => {
  expect(SUPPORTED_SYNTAX_LANGUAGES).toEqual([
    "markdown", "javascript", "typescript", "zig",
    "python", "go", "cpp", "bash", "c", "java", "html", "json", "yaml", "css",
  ])
  expect(getBundledSyntaxParsers()).toHaveLength(10)
  for (const parser of getBundledSyntaxParsers()) {
    expect(parser.wasm).not.toStartWith("http")
    expect(parser.queries.highlights[0]).not.toStartWith("http")
  }
})

test("常用 fenced-code 别名会解析到已注册的 filetype", () => {
  expect(infoStringToFiletype("py")).toBe("python")
  expect(infoStringToFiletype("sh")).toBe("bash")
  expect(infoStringToFiletype("zsh")).toBe("bash")
  expect(infoStringToFiletype("c++")).toBe("cpp")
  expect(infoStringToFiletype("cxx")).toBe("cpp")
  expect(infoStringToFiletype("yml")).toBe("yaml")
  expect(infoStringToFiletype("ts")).toBe("typescript")
})

test("离线资源的内容与已提交 SHA-256 清单一致", async () => {
  const root = resolve(import.meta.dir, "../../src/tui/assets/syntax")
  for (const parser of manifest.parsers) {
    const resources = [parser.wasm, parser.highlights, ...("injections" in parser && parser.injections ? [parser.injections] : [])]
    for (const resource of resources) {
      const content = await readFile(resolve(root, resource.path))
      const hash = createHash("sha256").update(content).digest("hex")
      expect(hash).toBe(resource.sha256)
    }
  }
})

test("本地 WASM 可高亮全部首版语言，不依赖运行时下载", async () => {
  registerCommonSyntaxParsers()
  const dataPath = resolve(import.meta.dir, "../../../.test-tree-sitter")
  const client = new TreeSitterClient({ dataPath, initTimeout: 15_000 })
  const snippets: Record<string, string> = {
    python: "def greet(name: str) -> str:\n    return f'hi {name}'\n",
    go: "package main\nfunc main() { println(\"hi\") }\n",
    cpp: "#include <string>\nint main() { return 0; }\n",
    bash: "#!/usr/bin/env bash\necho \"hello\"\n",
    c: "#include <stdio.h>\nint main(void) { return 0; }\n",
    java: "class Main { static void main(String[] args) {} }\n",
    html: "<html lang=\"zh-CN\"><style>body { color: #2563eb; margin: 0; }</style><script>const app = true</script></html>\n",
    json: "{\"enabled\": true, \"count\": 2}\n",
    yaml: "enabled: true\nitems:\n  - one\n",
    css: ".app { color: #70a4ff; }\n",
  }

  try {
    await client.initialize()
    for (const [filetype, content] of Object.entries(snippets)) {
      const result = await client.highlightOnce(content, filetype)
      expect(result.error).toBeUndefined()
      expect(result.warning).toBeUndefined()
      expect(result.highlights?.length ?? 0).toBeGreaterThan(0)
      if (filetype === "html") {
        expect(result.highlights?.some(item => item[2] === "tag")).toBeTrue()
        expect(result.highlights?.some(item => item[2] === "attribute")).toBeTrue()
        expect(result.highlights?.some(item => item[2] === "property" && item[3]?.isInjection && item[3]?.injectionLang === "css")).toBeTrue()
        expect(result.highlights?.some(item => item[2] === "keyword" && item[3]?.isInjection && item[3]?.injectionLang === "javascript")).toBeTrue()
      }
    }
  } finally {
    await client.destroy()
  }
}, 60_000)
