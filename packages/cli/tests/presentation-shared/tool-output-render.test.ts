/** Tool 输出渲染模型：JSON 美化、行数统计、折叠阈值与分工具结构化解析。 */

import { expect, test } from "bun:test"

import {
  prettifyJson,
  TOOL_OUTPUT_COLLAPSE_LINES,
  toolOutputView,
} from "../../src/presentation-shared/tool-output-render"

// ---------- 通用回退（text / json） ----------

test("非 JSON 文本原样返回，行数按换行统计", () => {
  const view = toolOutputView("some_tool", "a.txt\nb.txt")
  expect(view.kind).toBe("text")
  expect(view.text).toBe("a.txt\nb.txt")
  expect(view.lineCount).toBe(2)
  expect(view.metaLabel).toBe("2 行")
  expect(view.collapsible).toBe(false)
})

test("JSON 对象输出美化两空格缩进，kind 标记为 json", () => {
  const view = toolOutputView("web_search", "{\"ok\":true,\"path\":\"/tmp/a.md\"}")
  expect(view.kind).toBe("json")
  expect(view.text).toBe("{\n  \"ok\": true,\n  \"path\": \"/tmp/a.md\"\n}")
  expect(view.lineCount).toBe(4)
})

test("JSON 标量与非法 JSON 回退原文", () => {
  expect(toolOutputView("some_tool", "42").kind).toBe("text")
  expect(toolOutputView("some_tool", "{\"broken\"").kind).toBe("text")
  expect(toolOutputView("some_tool", "plain text").kind).toBe("text")
})

test("空输出返回空文本与 0 行", () => {
  const view = toolOutputView("some_tool", "")
  expect(view.text).toBe("")
  expect(view.lineCount).toBe(0)
  expect(view.collapsible).toBe(false)
})

test("超过折叠阈值行数时 collapsible 为 true，边界值不折叠", () => {
  const exact = Array.from({ length: TOOL_OUTPUT_COLLAPSE_LINES }, (_, i) => `line${i + 1}`).join("\n")
  expect(toolOutputView("some_tool", exact).collapsible).toBe(false)
  const over = exact + "\nextra"
  expect(toolOutputView("some_tool", over).collapsible).toBe(true)
  expect(toolOutputView("some_tool", over).lineCount).toBe(TOOL_OUTPUT_COLLAPSE_LINES + 1)
})

test("prettifyJson：对象/数组美化，其余返回 null", () => {
  expect(prettifyJson("{\"a\":1}")).toBe("{\n  \"a\": 1\n}")
  expect(prettifyJson("[1,2]")).toBe("[\n  1,\n  2\n]")
  expect(prettifyJson("\"str\"")).toBeNull()
  expect(prettifyJson("not json")).toBeNull()
  expect(prettifyJson("")).toBeNull()
})

// ---------- read_file → file-content ----------

const READ_OUTPUT = JSON.stringify({
  ok: true,
  path: "/tmp/handoff.md",
  snapshot_id: "snap_1",
  shown_lines: { start_line: 1, end_line: 3 },
  total_lines: 10,
  line_count: 10,
  byte_length: 100,
  content: "1\t# 标题\n2\t\n3\t正文",
  truncated: false,
})

test("read_file 输出解析为 file-content：元信息与行号 gutter 数据", () => {
  const view = toolOutputView("read_file", READ_OUTPUT)
  expect(view.kind).toBe("file-content")
  expect(view.fileContent?.path).toBe("/tmp/handoff.md")
  expect(view.fileContent?.shownStart).toBe(1)
  expect(view.fileContent?.shownEnd).toBe(3)
  expect(view.fileContent?.totalLines).toBe(10)
  expect(view.fileContent?.truncated).toBe(false)
  expect(view.fileContent?.lines).toEqual([
    { number: 1, text: "# 标题" },
    { number: 2, text: "" },
    { number: 3, text: "正文" },
  ])
  expect(view.lineCount).toBe(3)
  expect(view.metaLabel).toBe("3 行")
  // 复制给原文（含行号前缀的 raw output）。
  expect(view.text).toBe(READ_OUTPUT)
})

test("read_file content 行缺行号前缀时 number 为 null（容错不崩）", () => {
  const output = JSON.stringify({ path: "/a.txt", content: "1\tok\nwrapped" })
  const view = toolOutputView("read_file", output)
  expect(view.kind).toBe("file-content")
  expect(view.fileContent?.lines[1]).toEqual({ number: null, text: "wrapped" })
})

test("read_file 非法 JSON 回退 text；缺 content 字段回退 json 美化", () => {
  expect(toolOutputView("read_file", "read failed").kind).toBe("text")
  expect(toolOutputView("read_file", "{\"ok\":true}").kind).toBe("json")
})

// ---------- ls / glob → path-list ----------

test("ls Python repr 列表解析为 path-list，目录尾斜杠保留", () => {
  const view = toolOutputView("ls", "['/.DS_Store', '/.git/', '/AGENTS.md']")
  expect(view.kind).toBe("path-list")
  expect(view.pathList?.entries).toEqual(["/.DS_Store", "/.git/", "/AGENTS.md"])
  expect(view.lineCount).toBe(3)
  expect(view.metaLabel).toBe("3 项")
})

test("path-list 兼容双引号与转义；空列表为 0 项", () => {
  expect(toolOutputView("glob", "[\"/a\", \"/b\"]").pathList?.entries).toEqual(["/a", "/b"])
  const empty = toolOutputView("ls", "[]")
  expect(empty.kind).toBe("path-list")
  expect(empty.pathList?.entries).toEqual([])
  expect(empty.metaLabel).toBe("0 项")
})

test("glob 单条结果解析；非列表文本回退 text", () => {
  expect(toolOutputView("glob", "['/tmp/handoff.md']").pathList?.entries).toEqual(["/tmp/handoff.md"])
  expect(toolOutputView("ls", "not a list").kind).toBe("text")
})

// ---------- grep → grep-matches / path-list ----------

test("grep content 模式解析为分组匹配（路径头 + 行号 + 内容）", () => {
  const output = "/a.ts:\n  12: const foo = 1\n  30: foo()\n/b.ts:\n  3: foo"
  const view = toolOutputView("grep", output)
  expect(view.kind).toBe("grep-matches")
  expect(view.grepMatches?.groups).toEqual([
    { path: "/a.ts", matches: [{ line: 12, text: "const foo = 1" }, { line: 30, text: "foo()" }] },
    { path: "/b.ts", matches: [{ line: 3, text: "foo" }] },
  ])
  expect(view.lineCount).toBe(5)
})

test("grep count 模式解析为路径 + 命中数", () => {
  const view = toolOutputView("grep", "/a.ts: 3\n/b.ts: 1")
  expect(view.kind).toBe("grep-matches")
  expect(view.grepMatches?.mode).toBe("count")
  expect(view.grepMatches?.groups).toEqual([
    { path: "/a.ts", matches: [{ line: null, text: "", count: 3 }] },
    { path: "/b.ts", matches: [{ line: null, text: "", count: 1 }] },
  ])
})

test("grep files_with_matches 模式按 path-list 渲染；无命中哨兵回退 text", () => {
  const files = toolOutputView("grep", "/a.ts\n/b.ts")
  expect(files.kind).toBe("path-list")
  expect(files.pathList?.entries).toEqual(["/a.ts", "/b.ts"])
  expect(toolOutputView("grep", "No matches found").kind).toBe("text")
})

test("结构化解析不改变折叠阈值语义（按渲染行数计）", () => {
  const entries = Array.from({ length: TOOL_OUTPUT_COLLAPSE_LINES + 1 }, (_, i) => `'/f${i}.ts'`)
  const view = toolOutputView("glob", `[${entries.join(", ")}]`)
  expect(view.kind).toBe("path-list")
  expect(view.collapsible).toBe(true)
})

// ---------- edit_file / write_file → diff（红绿行来自 arguments） ----------

const EDIT_OUTPUT = JSON.stringify({
  ok: true,
  path: "/src/a.ts",
  shown_lines: { start_line: 1, end_line: 3 },
  total_lines: 3,
  content: "1\tconst a = 1\n2\tconst b = 2\n3\tconst c = 3",
  truncated: false,
})

test("edit_file 成功且参数含 old/new 时渲染 diff：公共行 context、删除/新增计数正确", () => {
  const args = JSON.stringify({
    file_path: "/src/a.ts",
    snapshot_id: "snap_1",
    old_string: "const b = 1",
    new_string: "const b = 2\nconst b2 = 3",
  })
  const view = toolOutputView("edit_file", EDIT_OUTPUT, args)
  expect(view.kind).toBe("diff")
  expect(view.diff?.path).toBe("/src/a.ts")
  expect(view.diff?.added).toBe(2)
  expect(view.diff?.removed).toBe(1)
  expect(view.diff?.rows).toEqual([
    { type: "remove", text: "const b = 1" },
    { type: "add", text: "const b = 2" },
    { type: "add", text: "const b2 = 3" },
  ])
  expect(view.metaLabel).toBe("+2 −1")
  expect(view.lineCount).toBe(3)
})

test("edit_file diff 保留公共上下文行", () => {
  const args = JSON.stringify({ file_path: "/a.txt", old_string: "a\nb\nc", new_string: "a\nX\nc" })
  const view = toolOutputView("edit_file", EDIT_OUTPUT, args)
  expect(view.diff?.rows).toEqual([
    { type: "context", text: "a" },
    { type: "remove", text: "b" },
    { type: "add", text: "X" },
    { type: "context", text: "c" },
  ])
})

test("write_file 新建文件渲染全新增 diff", () => {
  const output = JSON.stringify({ ok: true, path: "/new.md", created: true, content: "1\t# hi", total_lines: 1 })
  const args = JSON.stringify({ file_path: "/new.md", content: "# hi" })
  const view = toolOutputView("write_file", output, args)
  expect(view.kind).toBe("diff")
  expect(view.diff?.added).toBe(1)
  expect(view.diff?.removed).toBe(0)
  expect(view.metaLabel).toBe("+1 −0")
})

test("edit/write 失败（ok 非 true）或缺参数时不伪造 diff，回退 file-content/json", () => {
  const failed = JSON.stringify({ ok: false, error: "SNAPSHOT_STALE" })
  const args = JSON.stringify({ file_path: "/a", old_string: "x", new_string: "y" })
  expect(toolOutputView("edit_file", failed, args).kind).toBe("json")
  // 缺 arguments 时回退到输出的 file-content（编辑后上下文窗口）。
  expect(toolOutputView("edit_file", EDIT_OUTPUT).kind).toBe("file-content")
  // 参数非法 JSON 同样回退。
  expect(toolOutputView("edit_file", EDIT_OUTPUT, "{broken").kind).toBe("file-content")
})

// ---------- execute / monitor → terminal ----------

test("execute 输出解析为 terminal：命令行、exit code 与尾部标记剥离", () => {
  const output = "hello world\n[Command succeeded with exit code 0]"
  const args = JSON.stringify({ command: "echo hello world" })
  const view = toolOutputView("execute", output, args)
  expect(view.kind).toBe("terminal")
  expect(view.terminal?.command).toBe("echo hello world")
  expect(view.terminal?.exitCode).toBe(0)
  expect(view.terminal?.truncated).toBe(false)
  expect(view.terminal?.lines).toEqual(["hello world"])
  expect(view.metaLabel).toBe("exit 0")
})

test("execute 非零退出码与截断标记", () => {
  const output = "err text\n[Command failed with exit code 2]\n[Output was truncated due to size limits]"
  const view = toolOutputView("execute", output)
  expect(view.kind).toBe("terminal")
  expect(view.terminal?.exitCode).toBe(2)
  expect(view.terminal?.truncated).toBe(true)
  expect(view.terminal?.lines).toEqual(["err text"])
  expect(view.metaLabel).toBe("exit 2")
})

test("execute 无标记输出仍是 terminal（exitCode null），monitor 同型", () => {
  const view = toolOutputView("execute", "partial output")
  expect(view.kind).toBe("terminal")
  expect(view.terminal?.exitCode).toBeNull()
  expect(view.metaLabel).toBe("1 行")
  expect(toolOutputView("monitor", "{\"success\":true}", JSON.stringify({ command: "x" })).kind).toBe("json")
})
