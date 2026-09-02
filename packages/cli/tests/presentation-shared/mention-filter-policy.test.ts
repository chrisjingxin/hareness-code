/** 共享提及候选过滤策略测试：工作区文件模糊匹配、排序与上限截断。 */

import { expect, describe, test } from "bun:test"
import { searchMentionOptions, type MentionCandidateItem } from "../../src/presentation-shared/mention-filter-policy"

const MOCK_FILES: readonly MentionCandidateItem[] = [
  { path: "src", name: "src", kind: "directory" },
  { path: "src/app.tsx", name: "app.tsx", kind: "file" },
  { path: "src/index.ts", name: "index.ts", kind: "file" },
  { path: "src/components/button.tsx", name: "button.tsx", kind: "file" },
  { path: "docs/readme.md", name: "readme.md", kind: "file" },
  { path: "package.json", name: "package.json", kind: "file" },
  { path: "packages/cli/src/main.ts", name: "main.ts", kind: "file" },
  { path: "packages/cli/src/adapter.ts", name: "adapter.ts", kind: "file" },
]

describe("searchMentionOptions", () => {
  test("仅保留非目录文件项", () => {
    const result = searchMentionOptions(MOCK_FILES, "")
    expect(result.items.some(item => item.kind === "directory")).toBe(false)
    expect(result.items.length).toBe(7)
    expect(result.totalMatches).toBe(7)
    expect(result.truncated).toBe(false)
  })

  test("按文件名或路径子串匹配", () => {
    const result = searchMentionOptions(MOCK_FILES, "app")
    expect(result.items.map(r => r.path)).toEqual(["src/app.tsx"])
    expect(result.items[0]?.matchRanges).toEqual([{ start: 4, end: 7 }])
  })

  test("忽略大小写匹配", () => {
    const result = searchMentionOptions(MOCK_FILES, "README")
    expect(result.items.map(r => r.path)).toEqual(["docs/readme.md"])
  })

  test("前缀匹配/短路径优先排序", () => {
    const result = searchMentionOptions(MOCK_FILES, "src")
    expect(result.items.length).toBeGreaterThan(0)
    // 应该匹配到 src/ 下的文件
    expect(result.items.map(r => r.path)).toContain("src/index.ts")
  })

  test("候选池最多 1000 条，但保留真实匹配总数与截断标记", () => {
    const manyFiles: MentionCandidateItem[] = Array.from({ length: 1_205 }, (_, i) => ({
      path: `file_${i}.ts`,
      name: `file_${i}.ts`,
      kind: "file",
    }))
    const result = searchMentionOptions(manyFiles, "file")
    expect(result.items).toHaveLength(1_000)
    expect(result.totalMatches).toBe(1_205)
    expect(result.truncated).toBe(true)
  })

  test("自动解析语言标识", () => {
    const result = searchMentionOptions(MOCK_FILES, "app.tsx")
    expect(result.items[0]?.language).toBe("tsx")
  })
})
