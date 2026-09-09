import { expect, test } from "bun:test"

import { currentThreadDisplayTitle, threadDisplayTitle, threadMatchesQuery } from "../../src/presentation-shared/thread-title"

test("有 title 时用短标题", () => {
  expect(threadDisplayTitle({
    title: "修索引",
    first_message: "请检查当前改动并且这段很长",
    latest_message: "好的",
  })).toBe("修索引")
})

test("title 为空时回退 first_message，再否则 latest_message，再否则无标题", () => {
  expect(threadDisplayTitle({ title: null, first_message: "第一条", latest_message: "最新" })).toBe("第一条")
  expect(threadDisplayTitle({ title: "  ", first_message: "", latest_message: "最新" })).toBe("最新")
  expect(threadDisplayTitle({ title: null, first_message: "", latest_message: "" })).toBe("（无标题）")
})

test("搜索能命中短标题", () => {
  const thread = { title: "修索引", first_message: "请检查当前改动", latest_message: "好的" }
  expect(threadMatchesQuery(thread, "修")).toBe(true)
  expect(threadMatchesQuery(thread, "检查")).toBe(true)
  expect(threadMatchesQuery(thread, "没有")).toBe(false)
})

test("当前会话显示名优先短标题，没有 thread 时为新会话", () => {
  const threads = [
    {
      thread_id: "thread-1",
      title: "修索引",
      first_message: "请检查当前改动",
      latest_message: "好的",
    },
  ]
  expect(currentThreadDisplayTitle({ currentThreadId: "thread-1", threads })).toBe("修索引")
  expect(currentThreadDisplayTitle({
    currentThreadId: "thread-1",
    threads: [{ ...threads[0]!, title: null }],
  })).toBe("请检查当前改动")
  expect(currentThreadDisplayTitle({ currentThreadId: null, threads })).toBe("新会话")
  expect(currentThreadDisplayTitle({ currentThreadId: "missing", threads })).toBe("新会话")
})
