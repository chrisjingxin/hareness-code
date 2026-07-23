import { expect, test } from "bun:test"
import { type TextareaRenderable } from "@opentui/core"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, createRef } from "react"

import { DialogShell, SearchPicker } from "../../src/tui/overlays"

type PickerItem = {
  id: string
  label: string
  detail: string
}

const items: readonly PickerItem[] = [
  { id: "model-a", label: "Model A", detail: "支持工具调用" },
  { id: "model-b", label: "Model B", detail: "长上下文" },
]

test("SearchPicker<T> 聚合焦点、Enter 选择、领域行渲染与标准尺寸", async () => {
  const searchRef = createRef<TextareaRenderable>()
  let selected: string | undefined
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(SearchPicker<PickerItem>, {
      visible: true,
      loading: false,
      items,
      query: "",
      selectedIndex: 1,
      terminalWidth: 100,
      terminalHeight: 30,
      searchRef,
      searchId: "model-search",
      title: "Models",
      searchPlaceholder: "搜索模型...",
      emptyMessage: "没有匹配的模型",
      itemKey: item => item.id,
      renderItem: (item, context) => createElement(
        "box",
        { flexDirection: "row", gap: 1 },
        createElement("text", { fg: context.selected ? "#090a0c" : "#70a4ff" }, item.label),
        !context.compact ? createElement("text", { fg: "#9297a3" }, item.detail) : null,
      ),
      onSearch: () => undefined,
      onSelect: item => { selected = item.id },
      onHover: () => undefined,
      onClose: () => undefined,
    }), { width: 100, height: 30 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Models")
    expect(frame).toContain("搜索模型")
    expect(frame).toContain("支持工具调用")
    expect(searchRef.current?.focused).toBeTrue()

    await act(async () => {
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(selected).toBe("model-b")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("SearchPicker<T> 在窄终端保持单列，并统一呈现加载、错误和空态", async () => {
  const renderPicker = async (options: { loading: boolean; error?: string; items: readonly PickerItem[] }) => {
    let setup: Awaited<ReturnType<typeof testRender>>
    await act(async () => {
      setup = await testRender(createElement(SearchPicker<PickerItem>, {
        visible: true,
        query: "",
        selectedIndex: 0,
        terminalWidth: 58,
        terminalHeight: 18,
        searchRef: createRef<TextareaRenderable>(),
        searchId: "compact-search",
        title: "Models",
        searchPlaceholder: "搜索模型...",
        emptyMessage: "没有匹配的模型",
        itemKey: item => item.id,
        renderItem: (item, context) => createElement("text", undefined, context.compact ? item.label : item.detail),
        onSearch: () => undefined,
        onSelect: () => undefined,
        onHover: () => undefined,
        onClose: () => undefined,
        ...options,
      }), { width: 58, height: 18 })
    })
    try {
      await act(async () => { await setup.flush() })
      return setup.captureCharFrame()
    } finally {
      await act(async () => { setup.renderer.destroy() })
    }
  }

  expect(await renderPicker({ loading: false, items })).toContain("Model A")
  expect(await renderPicker({ loading: true, items })).toContain("正在读取 Models")
  expect(await renderPicker({ loading: false, error: "模型目录不可用", items })).toContain("模型目录不可用")
  expect(await renderPicker({ loading: false, items: [] })).toContain("没有匹配的模型")
})

test("DialogShell 使用统一确认布局，并允许紧邻动作插入自定义内容", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(DialogShell, {
      visible: true,
      title: "开始新的 Thread？",
      message: "确认后将先取消任务。",
      terminalWidth: 58,
      terminalHeight: 18,
      onConfirm: () => undefined,
      onCancel: () => undefined,
    }, createElement("text", { fg: "#9297a3" }, "可由 /compact 复用的额外内容")), { width: 58, height: 18 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("开始新的 Thread？")
    expect(frame).toContain("确认后将先取消任务")
    expect(frame).toContain("可由 /compact 复用的额外内容")
    expect(frame).toContain("Enter 确认")
    expect(frame).toContain("Esc 取消")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})
