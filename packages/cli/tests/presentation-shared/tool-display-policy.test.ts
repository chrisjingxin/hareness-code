/** 共享 Tool 显示策略的行为测试：动词标签、副作用基调、主参数提取与未知工具回退。 */

import { expect, test } from "bun:test"
import { toolDisplay, toolPrimaryArgument } from "../../src/presentation-shared/tool-display-policy"

test("内置文件工具映射为动词化中文标签与读写基调", () => {
  expect(toolDisplay("read_file")).toEqual({ label: "读取文件", icon: "file-read", tone: "read", known: true })
  expect(toolDisplay("ls")).toEqual({ label: "列出目录", icon: "folder", tone: "read", known: true })
  expect(toolDisplay("write_file").tone).toBe("write")
  expect(toolDisplay("edit_file").tone).toBe("write")
  expect(toolDisplay("delete_file").tone).toBe("write")
  expect(toolDisplay("execute")).toEqual({ label: "执行命令", icon: "terminal", tone: "write", known: true })
})

test("未登记工具回退为 wrench 图标与原始工具名", () => {
  const display = toolDisplay("mcp__github__create_issue")
  expect(display).toEqual({ label: "mcp__github__create_issue", icon: "wrench", tone: "neutral", known: false })
})

test("已知工具按目录优先级取第一个标量主参数", () => {
  expect(toolPrimaryArgument("read_file", "{\"file_path\":\"/a/b.ts\",\"offset\":1}")).toBe("/a/b.ts")
  expect(toolPrimaryArgument("glob", "{\"pattern\":\"**/*.py\",\"path\":\"/x\"}")).toBe("**/*.py")
  expect(toolPrimaryArgument("execute", "{\"command\":\"bun test\"}")).toBe("bun test")
  // 首选键缺失时取次选键。
  expect(toolPrimaryArgument("lsp", "{\"file_path\":\"/a.ts\",\"action\":\"hover\"}")).toBe("hover")
})

test("主参数是嵌套值或缺键时回退到参数摘要", () => {
  // write_file 的 file_path 若给到对象（畸形），跳过并回退摘要。
  const summary = toolPrimaryArgument("write_file", "{\"file_path\":{\"x\":1},\"content\":\"abc\"}")
  expect(summary).toContain("content: abc")
  // 无候选键的工具（write_todos）直接走摘要。
  expect(toolPrimaryArgument("write_todos", "{\"todos\":[1,2]}")).toBe("todos: {2}")
})

test("非 JSON 与空 arguments 的确定性行为", () => {
  expect(toolPrimaryArgument("read_file", "not json")).toBe("not json")
  expect(toolPrimaryArgument("read_file", undefined)).toBeNull()
  expect(toolPrimaryArgument("read_file", "")).toBeNull()
})

test("主参数收敛为单行并按 maxChars 截断", () => {
  const longPath = `/repo/${"a".repeat(100)}.ts`
  const result = toolPrimaryArgument("read_file", JSON.stringify({ file_path: longPath }), 40)
  expect(result).toBe(`${longPath.slice(0, 39)}…`)
  expect(toolPrimaryArgument("execute", "{\"command\":\"echo  a\\n b\"}")).toBe("echo a b")
})


test("已下线假工具不再登记展示目录", () => {
  expect(toolDisplay("monitor").known).toBe(false)
  expect(toolDisplay("task_output").known).toBe(false)
  expect(toolDisplay("task_stop").known).toBe(false)
})
