/** write/edit 参数只抽出路径和正文，不把整段 JSON 当展示。 */

import { expect, test } from "bun:test"

import { parseFileDiff } from "../../../src/presentation-shared/file-diff"
import { parseFileMutationArgs, parseToolResultPreview, unifiedDiffFromReplacement } from "../../../src/tui/presentation/tools/file-mutation-args"

test("从 write_file 参数抽出路径和正文", () => {
  const raw = JSON.stringify({
    file_path: "/examples/python/jsondiff_usage.py",
    content: "print('hello')\nprint('world')\n",
  })
  expect(parseFileMutationArgs(raw)).toEqual({
    path: "/examples/python/jsondiff_usage.py",
    content: "print('hello')\nprint('world')\n",
    oldString: null,
    newString: null,
  })
})

test("流式未闭合 JSON 也能抽出路径和已到达的正文", () => {
  const raw = '{"file_path":"/examples/python/jsondiff_usage.py","content":"print(\'hello\')\\nprint(\'wo'
  expect(parseFileMutationArgs(raw)).toEqual({
    path: "/examples/python/jsondiff_usage.py",
    content: "print('hello')\nprint('wo",
    oldString: null,
    newString: null,
  })
})

test("接受 path 字段别名；畸形 JSON 返回空", () => {
  expect(parseFileMutationArgs(JSON.stringify({ path: "src/app.ts", content: "x" }))).toEqual({
    path: "src/app.ts",
    content: "x",
    oldString: null,
    newString: null,
  })
  expect(parseFileMutationArgs("{not-json")).toEqual({ path: null, content: null, oldString: null, newString: null })
  expect(parseFileMutationArgs("")).toEqual({ path: null, content: null, oldString: null, newString: null })
})

test("从 edit_file 参数抽出 old/new，并生成可解析 unified diff", () => {
  const parsed = parseFileMutationArgs(JSON.stringify({
    file_path: "/file-organizer.py",
    old_string: "documents",
    new_string: "media",
  }))
  expect(parsed).toMatchObject({ path: "/file-organizer.py", oldString: "documents", newString: "media" })
  const diff = unifiedDiffFromReplacement(parsed.path ?? "file", parsed.oldString ?? "", parsed.newString ?? "")
  expect(parseFileDiff(diff).status).toBe("parsed")
  expect(diff).toContain("-documents")
  expect(diff).toContain("+media")
})

test("从工具结果 JSON 抽出恢复用路径和正文窗口", () => {
  const raw = JSON.stringify({
    ok: true,
    path: "/file-organizer.py",
    snapshot_id: "snap-1",
    content: "    '.csv': 'documents',\n",
  })
  expect(parseToolResultPreview(raw)).toMatchObject({
    path: "/file-organizer.py",
    content: "    '.csv': 'documents',\n",
  })
})
