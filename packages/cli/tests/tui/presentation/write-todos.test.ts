import { expect, test } from "bun:test"

import { currentTodo, parseWriteTodos, todoMarker } from "../../../src/tui/presentation/tools/write-todos"

test("从 write_todos 参数抽出清单，不依赖铺开 JSON", () => {
  const items = parseWriteTodos(JSON.stringify({
    todos: [
      { content: "实现核心 diff 模块", status: "in_progress" },
      { content: "实现 CLI", status: "pending" },
      { content: "编写 pytest 测试", status: "completed" },
    ],
  }))
  expect(items).toEqual([
    { content: "实现核心 diff 模块", status: "in_progress" },
    { content: "实现 CLI", status: "pending" },
    { content: "编写 pytest 测试", status: "completed" },
  ])
})

test("畸形或空参数得到空清单", () => {
  expect(parseWriteTodos("")).toEqual([])
  expect(parseWriteTodos("{")).toEqual([])
  expect(parseWriteTodos(JSON.stringify({ todos: "nope" }))).toEqual([])
})

test("状态标记是 ASCII，当前项优先进行中", () => {
  expect(todoMarker("pending")).toBe("[ ]")
  expect(todoMarker("in_progress")).toBe("[>]")
  expect(todoMarker("completed")).toBe("[x]")
  expect(currentTodo([
    { content: "已做", status: "completed" },
    { content: "正在做", status: "in_progress" },
    { content: "待做", status: "pending" },
  ])?.content).toBe("正在做")
})
