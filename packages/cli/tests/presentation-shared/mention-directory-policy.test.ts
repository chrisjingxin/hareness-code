/** @ 目录浏览策略测试：只列直接子项、目录优先并保持稳定路径。 */

import { describe, expect, test } from "bun:test"

import {
  mentionOptionsForQuery,
  parentMentionDirectory,
  type MentionCandidateItem,
} from "../../src/presentation-shared/mention-filter-policy"

const ENTRIES: readonly MentionCandidateItem[] = [
  { path: "src", name: "src", kind: "directory" },
  { path: "src/components", name: "components", kind: "directory" },
  { path: "src/components/button.tsx", name: "button.tsx", kind: "file" },
  { path: "src/app.tsx", name: "app.tsx", kind: "file" },
  { path: "docs", name: "docs", kind: "directory" },
  { path: "docs/readme.md", name: "readme.md", kind: "file" },
  { path: "package.json", name: "package.json", kind: "file" },
]

describe("mention directory policy", () => {
  test("空查询在根目录只返回第一层，并按目录后文件排序", () => {
    const result = mentionOptionsForQuery(ENTRIES, "", "")
    expect(result.items.map(item => [item.path, item.kind])).toEqual([
      ["docs", "directory"],
      ["src", "directory"],
      ["package.json", "file"],
    ])
  })

  test("进入目录后只返回该目录的直接子项", () => {
    const result = mentionOptionsForQuery(ENTRIES, "", "src")
    expect(result.items.map(item => item.path)).toEqual([
      "src/components",
      "src/app.tsx",
    ])
  })

  test("非空查询忽略浏览目录，对全工作区文件做搜索且不返回目录", () => {
    const result = mentionOptionsForQuery(ENTRIES, "button", "docs")
    expect(result.items.map(item => item.path)).toEqual(["src/components/button.tsx"])
  })

  test("父目录计算在根目录保持根，在嵌套目录逐级返回", () => {
    expect(parentMentionDirectory("")).toBe("")
    expect(parentMentionDirectory("src")).toBe("")
    expect(parentMentionDirectory("src/components")).toBe("src")
  })
})
