/** Timeline：用户/Assistant/Tool/Interaction 时间线 + Tool 折叠 dispatch + ARIA 属性。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act, useState } from "react"
import { createElement, type ReactElement } from "react"

import { Timeline } from "../../../src/web/presentation/timeline"
import { toolKey, type WebAdapterSnapshot, type WebIntent } from "../../../src/web/application/adapter"
import type {
  ConversationMessage,
  InteractionCard,
  TimelineItem,
  ToolCard,
} from "../../../src/interactive/state"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function message(item: Partial<ConversationMessage> & { id: string; role: ConversationMessage["role"]; content: string }): TimelineItem {
  return { type: "message", message: item }
}

function toolItem(item: Partial<ToolCard> & { id: string; name: string }): TimelineItem {
  return {
    type: "tool",
    tool: {
      id: item.id,
      runId: item.runId ?? "run-1",
      name: item.name,
      arguments: item.arguments ?? "",
      output: item.output ?? "",
      status: item.status ?? "completed",
    },
  }
}

function interactionItem(item: Partial<InteractionCard> & { id: string }): TimelineItem {
  return {
    type: "interaction",
    interaction: {
      id: item.id,
      runId: item.runId ?? "run-1",
      type: item.type ?? "approval",
      status: item.status ?? "approved",
      description: item.description,
      question: item.question,
    },
  }
}

describe("Timeline", () => {
  test("渲染 user/assistant/tool/interaction 四种时间线条目", () => {
    const interactive = makeInteractive({
      timeline: [
        message({ id: "u1", role: "user", content: "你好" }),
        message({ id: "a1", role: "assistant", content: "我很好" }),
        toolItem({ id: "t1", name: "read_file" }),
        interactionItem({ id: "i1", type: "approval", status: "approved" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const messages = handle.container.querySelectorAll(".timeline-message")
      expect(messages.length).toBe(2)
      expect(handle.container.querySelector(".tool-row")).not.toBeNull()
      expect(handle.container.querySelector(".interaction-card")).not.toBeNull()
      expect(handle.container.textContent).toContain("你好")
      expect(handle.container.textContent).toContain("我很好")
      expect(handle.container.textContent).toContain("读取文件")
    } finally {
      handle.unmount()
    }
  })

  test("自动滚动跟随：贴底时新消息滚到底，用户上滚后保持位置（滚动容器是 .timeline-scroll）", () => {
    // 生产结构：.timeline-scroll 是滚动容器，Timeline 的 .timeline 是不滚动的内容层。
    const first = [message({ id: "a1", role: "assistant", content: "第一条", runId: "run-1" })]
    let append: (() => void) | null = null
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({
        interactive: makeInteractive({ timeline: first }),
      }))
      append = () => {
        setSnapshot(prev => makeSnapshot({
          interactive: makeInteractive({
            timeline: [...prev.interactive.timeline, message({
              id: `a${prev.interactive.timeline.length + 1}`,
              role: "assistant",
              content: "后续",
              runId: "run-1",
            })],
          }),
        }))
      }
      return (
        <div className="timeline-scroll">
          <Timeline snapshot={snapshot} dispatch={() => {}} />
        </div>
      )
    }
    const handle = render(<Harness />)
    const setGeometry = (scroller: Element, scrollHeight: number): void => {
      Object.defineProperty(scroller, "scrollHeight", { value: scrollHeight, configurable: true })
      Object.defineProperty(scroller, "clientHeight", { value: 400, configurable: true })
    }
    try {
      const scroller = handle.container.querySelector(".timeline-scroll")
      expect(scroller === null).toBe(false)
      if (!scroller) return
      setGeometry(scroller, 1000)
      // 贴底状态下新消息 → 跟随滚到底（作用于滚动容器而非内容层）。
      act(() => { append?.() })
      expect((scroller as HTMLElement).scrollTop).toBe(1000)
      // 用户手动上滚（尚无新内容）→ 出现中性「回到底部」，不谎称有新输出。
      ;(scroller as HTMLElement).scrollTop = 100
      setGeometry(scroller, 1300)
      act(() => { scroller.dispatchEvent(new Event("scroll")) })
      const neutralButton = handle.container.querySelector<HTMLButtonElement>(".scroll-to-bottom")
      expect(neutralButton === null).toBe(false)
      expect(neutralButton?.textContent).toBe("回到底部")
      expect(neutralButton?.getAttribute("data-new")).toBe("false")
      // 上滚期间来了新内容 → 按钮转为「有新输出」强调态，视口仍不拉动。
      act(() => { append?.() })
      expect((scroller as HTMLElement).scrollTop).toBe(100)
      const button = handle.container.querySelector<HTMLButtonElement>(".scroll-to-bottom")
      expect(button === null).toBe(false)
      expect(button?.textContent).toBe("有新输出")
      expect(button?.getAttribute("data-new")).toBe("true")
      // 点击按钮回到底部，之后恢复跟随。
      act(() => { button?.click() })
      expect((scroller as HTMLElement).scrollTop).toBe(1300)
      expect(handle.container.querySelector(".scroll-to-bottom") === null).toBe(true)
      setGeometry(scroller, 1600)
      act(() => { scroller.dispatchEvent(new Event("scroll")) })
      act(() => { append?.() })
      expect((scroller as HTMLElement).scrollTop).toBe(1600)
    } finally {
      handle.unmount()
    }
  })

  test("Tool 默认折叠，只显示名称与状态；点击 dispatch tool-toggle", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "read_file", arguments: "{\"path\":\"/x\"}", output: "hello" }),
      ],
    })
    const intents: WebIntent[] = []
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={intent => intents.push(intent)} />,
    )
    try {
      const card = handle.container.querySelector<HTMLDivElement>(".tool-row")
      expect(card).not.toBeNull()
      expect(card?.querySelector(".tool-row-label")?.textContent).toBe("读取文件")
      expect(card?.querySelector(".tool-row-details")).toBeNull()
      const header = card?.querySelector<HTMLButtonElement>(".tool-row-header")
      expect(header?.getAttribute("aria-expanded")).toBe("false")
      act(() => { header?.click() })
      expect(intents).toEqual([{ type: "tool-toggle", runId: "run-1", toolId: "t1" }])
    } finally {
      handle.unmount()
    }
  })

  test("task 折叠行显示派出对象与任务，不含 JSON 键", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({
          id: "t-task",
          name: "task",
          arguments: JSON.stringify({
            description: "查找代码压缩实现",
            subagent_type: "general-purpose",
          }),
          status: "running",
        }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const row = handle.container.querySelector<HTMLDivElement>(".tool-row")
      expect(row?.querySelector(".tool-row-label")?.textContent).toBe("派出 general-purpose")
      expect(row?.querySelector(".tool-row-label")?.getAttribute("title")).toBe("task")
      expect(row?.querySelector(".tool-row-args")?.textContent).toBe("查找代码压缩实现")
      expect(row?.textContent).not.toContain("subagent_type")
      expect(row?.textContent).not.toContain("\"description\"")
    } finally {
      handle.unmount()
    }
  })

  test("task 展开后用结构化字段代替 JSON 参数", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({
          id: "t-task",
          name: "task",
          arguments: JSON.stringify({
            description: "查找代码压缩实现",
            subagent_type: "explore",
          }),
          output: "- **命令是否成功**: 未执行成功",
        }),
      ],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-row-header")
      act(() => { header?.click() })
      const dispatchCard = handle.container.querySelector(".tool-detail-card[data-section=\"dispatch\"]")
      expect(dispatchCard === null).toBe(false)
      expect(dispatchCard?.textContent).toContain("explore")
      expect(dispatchCard?.textContent).toContain("任务")
      expect(dispatchCard?.textContent).toContain("查找代码压缩实现")
      expect(dispatchCard?.textContent).toContain("结论")
      expect(dispatchCard?.querySelector("strong")?.textContent).toBe("命令是否成功")
      expect(dispatchCard?.textContent).toContain("未执行成功")
      expect(dispatchCard?.textContent).not.toContain("**命令是否成功**")
      expect(handle.container.querySelector("details.tool-detail-card[data-section=\"arguments\"]")).toBeNull()
      expect(handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("Tool 行展示动词标签、主参数与副作用基调；原始工具名挂 tooltip", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "execute", arguments: "{\"command\":\"bun test\"}", status: "failed" }),
        toolItem({ id: "t2", name: "mcp__docs__search", arguments: "{\"q\":\"x\"}", status: "completed" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const rows = handle.container.querySelectorAll<HTMLDivElement>(".tool-row")
      expect(rows).toHaveLength(2)
      // 已知工具：动词标签 + 主参数 + write 基调 + 失败行标记。
      const first = rows[0]!
      expect(first.getAttribute("data-tone")).toBe("write")
      expect(first.classList.contains("tool-row-failed")).toBe(true)
      expect(first.querySelector(".tool-row-label")?.textContent).toBe("执行命令")
      expect(first.querySelector(".tool-row-label")?.getAttribute("title")).toBe("execute")
      expect(first.querySelector(".tool-row-args")?.textContent).toBe("bun test")
      expect(first.querySelector(".tool-row-status")?.getAttribute("aria-label")).toBe("失败")
      // 未知 MCP 工具：回退为原始名 + neutral 基调；完成态 aria-label 可读。
      const second = rows[1]!
      expect(second.getAttribute("data-tone")).toBe("neutral")
      expect(second.querySelector(".tool-row-label")?.textContent).toBe("mcp__docs__search")
      expect(second.querySelector(".tool-row-status")?.getAttribute("aria-label")).toBe("已完成")
    } finally {
      handle.unmount()
    }
  })

  test("两个 Run 中相同 toolId 的展开状态互不影响", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "read_file", runId: "run-a" }),
        toolItem({ id: "t1", name: "read_file", runId: "run-b" }),
      ],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => {
            const key = toolKey(intent.runId, intent.toolId)
            const next = new Set(prev.expandedTools)
            if (next.has(key)) next.delete(key)
            else next.add(key)
            return { ...prev, expandedTools: next }
          })
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    const headers = (): NodeListOf<HTMLButtonElement> => handle.container.querySelectorAll<HTMLButtonElement>(".tool-row-header")
    const clickAt = (index: number): void => {
      act(() => { headers()[index]?.click() })
    }
    try {
      expect(headers().length).toBe(2)
      expect(headers()[0]?.getAttribute("aria-expanded")).toBe("false")
      clickAt(0)
      expect(headers()[0]?.getAttribute("aria-expanded")).toBe("true")
      expect(headers()[1]?.getAttribute("aria-expanded")).toBe("false")
      clickAt(1)
      expect(headers()[0]?.getAttribute("aria-expanded")).toBe("true")
      expect(headers()[1]?.getAttribute("aria-expanded")).toBe("true")
      clickAt(0)
      expect(headers()[0]?.getAttribute("aria-expanded")).toBe("false")
      expect(headers()[1]?.getAttribute("aria-expanded")).toBe("true")
    } finally {
      handle.unmount()
    }
  })

  test("Tool 展开后通过 expandedTools 集合显示参数与输出在 <pre> 内", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "exec", arguments: "{\"cmd\":\"ls\"}", output: "a.txt" }),
      ],
    })
    const intents: WebIntent[] = []
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        intents.push(intent)
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-row-header")
      act(() => { header?.click() })
      const details = handle.container.querySelector(".tool-row-details")
      expect(details).not.toBeNull()
      // 输出卡片：头部条（标题 + 行数 + 复制按钮）+ 内容体。
      const outputCard = details?.querySelector(".tool-detail-card[data-section=\"output\"]")
      expect(outputCard === null).toBe(false)
      expect(outputCard?.querySelector(".tool-detail-title")?.textContent).toBe("输出")
      expect(outputCard?.querySelector(".tool-detail-meta")?.textContent).toBe("1 行")
      const copyButton = outputCard?.querySelector<HTMLButtonElement>(".tool-detail-copy")
      expect(copyButton?.getAttribute("aria-label")).toBe("复制输出")
      expect(outputCard?.querySelector("pre.tool-details-pre")?.textContent).toBe("a.txt")
      // 参数降级为同级卡片，默认折叠，内容美化 JSON。
      const argumentsCard = details?.querySelector("details.tool-detail-card[data-section=\"arguments\"]")
      expect(argumentsCard === null).toBe(false)
      expect(argumentsCard?.querySelector(".tool-detail-title")?.textContent).toBe("参数")
      expect(argumentsCard?.hasAttribute("open")).toBe(false)
      expect(argumentsCard?.querySelector("pre.tool-details-pre")?.textContent).toBe("{\n  \"cmd\": \"ls\"\n}")
    } finally {
      handle.unmount()
    }
  })

  test("Tool 输出为 JSON 时美化渲染，非 JSON 原样展示", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "read_file", output: "{\"ok\":true}" }),
      ],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-row-header")
      act(() => { header?.click() })
      const pre = handle.container.querySelector(".tool-detail-card[data-section=\"output\"] pre.tool-details-pre")
      expect(pre?.textContent).toBe("{\n  \"ok\": true\n}")
    } finally {
      handle.unmount()
    }
  })

  test("Tool 输出超过折叠阈值时出现「展开全部/收起」切换", () => {
    const longOutput = Array.from({ length: 30 }, (_, i) => `line${i + 1}`).join("\n")
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "execute", output: longOutput }),
      ],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-row-header")
      act(() => { header?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      expect(card?.querySelector(".tool-detail-meta")?.textContent).toBe("30 行")
      const body = card?.querySelector(".tool-detail-body")
      expect(body?.getAttribute("data-clamped")).toBe("true")
      const toggle = card?.querySelector<HTMLButtonElement>(".tool-detail-expand")
      expect(toggle?.textContent).toBe("展开全部")
      act(() => { toggle?.click() })
      expect(body?.getAttribute("data-clamped")).toBe("false")
      expect(toggle?.textContent).toBe("收起")
    } finally {
      handle.unmount()
    }
  })

  test("read_file 展开渲染 file-content：元信息行 + 行号 gutter 行", () => {
    const output = JSON.stringify({
      ok: true,
      path: "/tmp/handoff.md",
      shown_lines: { start_line: 1, end_line: 2 },
      total_lines: 10,
      line_count: 10,
      byte_length: 100,
      content: "1\t# 标题\n2\t正文",
      truncated: false,
    })
    const interactive = makeInteractive({
      timeline: [toolItem({ id: "t1", name: "read_file", output })],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      const meta = card?.querySelector(".tool-file-meta")
      expect(meta?.textContent).toContain("/tmp/handoff.md")
      expect(meta?.textContent).toContain("共 10 行")
      const lines = card?.querySelectorAll(".tool-file-line")
      expect(lines?.length).toBe(2)
      expect(lines?.[0]?.querySelector(".tool-file-lineno")?.textContent).toBe("1")
      expect(lines?.[0]?.querySelector(".tool-file-text")?.textContent).toBe("# 标题")
      // 结构化渲染不再走通用 pre。
      expect(card?.querySelector("pre.tool-details-pre") === null).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("ls 展开渲染 path-list：每行文件类型图标 + 路径，meta 计「项」", () => {
    const output = "['/.DS_Store', '/.git/', '/AGENTS.md']"
    const interactive = makeInteractive({
      timeline: [toolItem({ id: "t1", name: "ls", output })],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      expect(card?.querySelector(".tool-detail-meta")?.textContent).toBe("3 项")
      const rows = card?.querySelectorAll(".tool-path-row")
      expect(rows?.length).toBe(3)
      expect(rows?.[0]?.querySelector("svg") === null).toBe(false)
      expect(rows?.[2]?.querySelector(".tool-path-text")?.textContent).toBe("/AGENTS.md")
    } finally {
      handle.unmount()
    }
  })

  test("grep content 模式展开渲染分组匹配：路径头 + 行号 + 内容行", () => {
    const output = "/a.ts:\n  12: const foo = 1\n/b.ts:\n  3: foo"
    const interactive = makeInteractive({
      timeline: [toolItem({ id: "t1", name: "grep", output })],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      const groups = card?.querySelectorAll(".tool-grep-group")
      expect(groups?.length).toBe(2)
      expect(groups?.[0]?.querySelector(".tool-grep-path")?.textContent).toBe("/a.ts")
      const lines = groups?.[0]?.querySelectorAll(".tool-grep-line")
      expect(lines?.length).toBe(1)
      expect(lines?.[0]?.querySelector(".tool-grep-lineno")?.textContent).toBe("12")
      expect(lines?.[0]?.querySelector(".tool-grep-text")?.textContent).toBe("const foo = 1")
    } finally {
      handle.unmount()
    }
  })

  test("流式光标只挂在时间线最后一项：文本后已有工具调用时不再闪烁（2026-08-18 用户截图反馈）", () => {
    // 运行中的历史文本段（streaming: true）后面跟了工具卡 → 该段不再是生成位置，不渲染光标。
    const midRun = makeInteractive({
      timeline: [
        message({ id: "a1", role: "assistant", content: "先创建一个临时文件：", streaming: true, runId: "run-1" }),
        toolItem({ id: "t1", name: "write_file", runId: "run-1" }),
      ],
    })
    const midHandle = render(
      <Timeline snapshot={makeSnapshot({ interactive: midRun })} dispatch={() => {}} />,
    )
    try {
      const bubble = midHandle.container.querySelector("[data-streaming=\"true\"]")
      expect(bubble === null).toBe(false)
      expect(bubble?.querySelector(".streaming-cursor") === null).toBe(true)
    } finally {
      midHandle.unmount()
    }
    // 流式文本是最后一项（正在生成）→ 光标保留。
    const live = makeInteractive({
      timeline: [
        message({ id: "a1", role: "assistant", content: "正在回答", streaming: true, runId: "run-1" }),
      ],
    })
    const liveHandle = render(
      <Timeline snapshot={makeSnapshot({ interactive: live })} dispatch={() => {}} />,
    )
    try {
      const cursor = liveHandle.container.querySelector(".streaming-cursor")
      expect(cursor === null).toBe(false)
    } finally {
      liveHandle.unmount()
    }
    // 文本生成位置继续后移：新文本段接在工具后，光标只在新段上。
    const moved = makeInteractive({
      timeline: [
        message({ id: "a1", role: "assistant", content: "先创建一个临时文件：", streaming: true, runId: "run-1" }),
        toolItem({ id: "t1", name: "write_file", runId: "run-1" }),
        message({ id: "a2", role: "assistant", content: "文件已创建", streaming: true, runId: "run-1" }),
      ],
    })
    const movedHandle = render(
      <Timeline snapshot={makeSnapshot({ interactive: moved })} dispatch={() => {}} />,
    )
    try {
      const cursors = movedHandle.container.querySelectorAll(".streaming-cursor")
      expect(cursors.length).toBe(1)
    } finally {
      movedHandle.unmount()
    }
  })

  test("edit_file 展开渲染 diff 视图：路径 meta + 红绿行", () => {
    const output = JSON.stringify({ ok: true, path: "/src/a.ts", content: "1\tx", total_lines: 1 })
    const args = JSON.stringify({ file_path: "/src/a.ts", old_string: "const b = 1", new_string: "const b = 2" })
    const interactive = makeInteractive({
      timeline: [toolItem({ id: "t1", name: "edit_file", arguments: args, output })],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      expect(card?.querySelector(".tool-detail-meta")?.textContent).toBe("+1 −1")
      const meta = card?.querySelector(".tool-diff-meta")
      expect(meta?.textContent).toContain("/src/a.ts")
      const rows = card?.querySelectorAll(".tool-diff-row")
      expect(rows?.length).toBe(2)
      expect(rows?.[0]?.getAttribute("data-type")).toBe("remove")
      expect(rows?.[0]?.querySelector(".tool-diff-sign")?.textContent).toBe("−")
      expect(rows?.[0]?.querySelector(".tool-diff-text")?.textContent).toBe("const b = 1")
      expect(rows?.[1]?.getAttribute("data-type")).toBe("add")
      expect(rows?.[1]?.querySelector(".tool-diff-sign")?.textContent).toBe("+")
    } finally {
      handle.unmount()
    }
  })

  test("execute 展开渲染终端块：$ 命令行 + 输出，meta 显示 exit code", () => {
    const output = "hello\n[Command succeeded with exit code 0]"
    const args = JSON.stringify({ command: "echo hello" })
    const interactive = makeInteractive({
      timeline: [toolItem({ id: "t1", name: "execute", arguments: args, output })],
    })
    const Harness = (): ReactElement => {
      const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
      const dispatch = (intent: WebIntent): void => {
        if (intent.type === "tool-toggle") {
          setSnapshot(prev => ({
            ...prev,
            expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
      const card = handle.container.querySelector(".tool-detail-card[data-section=\"output\"]")
      expect(card?.querySelector(".tool-detail-meta")?.textContent).toBe("exit 0")
      expect(card?.querySelector(".tool-terminal-cmd")?.textContent).toBe("$ echo hello")
      const lines = card?.querySelectorAll(".tool-terminal-line")
      expect(lines?.length).toBe(1)
      expect(lines?.[0]?.textContent).toBe("hello")
      // 尾部执行标记不进入渲染。
      expect(card?.textContent?.includes("Command succeeded") ?? true).toBe(false)
    } finally {
      handle.unmount()
    }
  })

  test("Tool 失败状态带「失败」文字徽章，完成状态不占文字位", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "execute", status: "failed" }),
        toolItem({ id: "t2", name: "read_file", status: "completed" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const rows = handle.container.querySelectorAll<HTMLDivElement>(".tool-row")
      const failedStatus = rows[0]?.querySelector(".tool-row-status")
      expect(failedStatus?.textContent).toContain("失败")
      expect(failedStatus?.getAttribute("aria-label")).toBe("失败")
      const completedStatus = rows[1]?.querySelector(".tool-row-status")
      expect(completedStatus?.textContent ?? "").toBe("")
    } finally {
      handle.unmount()
    }
  })

  test("不同 tool ID 渲染为独立卡片，互不合并", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "read_file" }),
        toolItem({ id: "t2", name: "exec" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const cards = handle.container.querySelectorAll(".tool-row")
      expect(cards.length).toBe(2)
      expect(cards[0]?.getAttribute("data-tool-id")).toBe("t1")
      expect(cards[1]?.getAttribute("data-tool-id")).toBe("t2")
    } finally {
      handle.unmount()
    }
  })

  test("Agent 后续的工具调用收进同一个消息气泡，并保留工具卡交互", () => {
    const interactive = makeInteractive({
      timeline: [
        message({ id: "a1", role: "assistant", content: "我先读取文件。" }),
        toolItem({ id: "t1", name: "read_file" }),
        toolItem({ id: "t2", name: "write_file" }),
        message({ id: "a2", role: "assistant", content: "文件已处理。" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const groups = handle.container.querySelectorAll(".timeline-agent-group")
      expect(groups).toHaveLength(1)
      expect(groups[0]?.querySelectorAll(".tool-row")).toHaveLength(2)
      expect(groups[0]?.querySelector(".tool-row")?.parentElement?.classList.contains("timeline-tool")).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("Agent 前置的工具调用也收进同一个消息气泡，并保持顺序", () => {
    const interactive = makeInteractive({
      timeline: [
        toolItem({ id: "t1", name: "memory_save" }),
        message({ id: "a1", role: "assistant", content: "已经记住了。" }),
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const group = handle.container.querySelector(".timeline-agent-group")
      expect(group).not.toBeNull()
      expect(group?.querySelector(".timeline-agent-group-header .message-author")?.textContent).toBe("Agent")
      expect(group?.querySelectorAll(".tool-row")).toHaveLength(1)
      expect(group?.querySelectorAll(".timeline-message")).toHaveLength(1)
      expect(group?.firstElementChild?.classList.contains("timeline-agent-group-header")).toBe(true)
      expect(group?.querySelector(".timeline-agent-group-header + .timeline-tool")).not.toBeNull()
      expect(group?.lastElementChild?.classList.contains("timeline-message")).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("Timeline 容器是 role=log 且 aria-relevant=additions；run-status-live 是 aria-live=polite", () => {
    const interactive = makeInteractive({
      activity: { kind: "running", label: "正在思考" },
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const log = handle.container.querySelector(".timeline")
      expect(log?.getAttribute("role")).toBe("log")
      expect(log?.getAttribute("aria-relevant")).toBe("additions")
      const live = handle.container.querySelector(".run-status-live")
      expect(live?.getAttribute("aria-live")).toBe("polite")
      expect(live?.textContent).toBe("正在运行")
    } finally {
      handle.unmount()
    }
  })

  test("空闲（home/idle）时 run-status-live 不渲染「就绪」：就绪即沉默，瞬态标签照常", () => {
    const idle = render(
      <Timeline snapshot={makeSnapshot({ interactive: makeInteractive({ activity: { kind: "idle", label: "就绪" } }) })} dispatch={() => {}} />,
    )
    try {
      const live = idle.container.querySelector(".run-status-live")
      expect(live?.getAttribute("aria-live")).toBe("polite")
      expect(live?.textContent).toBe("")
    } finally {
      idle.unmount()
    }
    const failed = render(
      <Timeline snapshot={makeSnapshot({ interactive: makeInteractive({ activity: { kind: "failed", label: "运行失败" } }) })} dispatch={() => {}} />,
    )
    try {
      expect(failed.container.querySelector(".run-status-live")?.textContent).toBe("运行失败")
    } finally {
      failed.unmount()
    }
  })

  test("运行期间显示事实阶段、活动时长和取消提示", () => {
    const interactive = makeInteractive({
      activeRun: { threadId: "thread-1", runId: "run-1" },
      activity: { kind: "running" },
      runProgress: { phase: "model", elapsedMs: 1_200 },
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const progress = handle.container.querySelector(".run-progress")
      expect(progress?.getAttribute("data-phase")).toBe("model")
      expect(progress?.getAttribute("role")).toBe("status")
      expect(progress?.getAttribute("aria-live")).toBe("polite")
      expect(progress?.textContent).toContain("1.2s")
      expect(progress?.textContent).toContain("Esc 取消")
    } finally {
      handle.unmount()
    }
  })

  test("流式思考中显示思考状态与文本", () => {
    const interactive = makeInteractive({
      activeRun: { threadId: "thread-1", runId: "run-1" },
      activity: { kind: "running" },
      timeline: [
        { type: "reasoning", reasoning: { id: "r-1", runId: "run-1", text: "正在检查代码路径", active: true } },
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      const reasoning = handle.container.querySelector(".reasoning")
      expect(reasoning?.getAttribute("role")).toBe("status")
      expect(reasoning?.getAttribute("aria-live")).toBe("polite")
      expect(reasoning?.getAttribute("data-active")).toBe("true")
      expect(reasoning?.textContent).toContain("思考中")
      expect(reasoning?.textContent).toContain("正在检查代码路径")
    } finally {
      handle.unmount()
    }
  })

  test("思考结束后不显示思考框", () => {
    const interactive = makeInteractive({
      activeRun: { threadId: "thread-1", runId: "run-1" },
      activity: { kind: "running" },
      timeline: [
        { type: "reasoning", reasoning: { id: "r-1", runId: "run-1", text: "第一行思考\n后续细节", active: false } },
      ],
    })
    const handle = render(
      <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
    )
    try {
      expect(handle.container.querySelector(".reasoning")).toBeNull()
      expect(handle.container.querySelector(".agent-thinking-card")).toBeNull()
    } finally {
      handle.unmount()
    }
  })
})

test("Compose activity 分组：终态默认折叠，Enter 可展开", () => {
  const interactive = makeInteractive({
    currentThreadId: "thread-1",
    activity: { kind: "running" },
    timeline: [
      {
        type: "tool",
        tool: {
          id: "call-1",
          runId: "run-1",
          name: "read_file",
          arguments: "",
          output: "secret-body",
          status: "completed",
          executionId: "child-a",
          activityId: "act-a",
          agentId: "understand",
        },
      },
      {
        type: "compose-summary",
        summary: {
          id: "sum-a",
          runId: "run-1",
          status: "passed",
          text: "理解完成：已识别目标",
          executionId: "child-a",
          activityId: "act-a",
          agentId: "understand",
          composeScope: { activityId: "act-a", stage: "understand", attempt: 1 },
        },
      },
    ],
  })
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
  )
  try {
    const group = handle.container.querySelector(".timeline-activity-group")
    expect(group).not.toBeNull()
    const header = handle.container.querySelector(".timeline-activity-header") as HTMLButtonElement
    expect(header.getAttribute("aria-expanded")).toBe("false")
    expect(header.textContent).toContain("理解")
    expect(handle.container.textContent).toContain("理解完成：已识别目标")
    // 折叠时不渲染组内 Tool 行
    expect(handle.container.textContent).not.toContain("读取文件")
    act(() => {
      header.click()
    })
    expect(handle.container.querySelector(".timeline-activity-header")?.getAttribute("aria-expanded")).toBe("true")
    expect(handle.container.textContent).toContain("读取文件")
    // 再次点击折叠
    act(() => {
      header.click()
    })
    expect(handle.container.querySelector(".timeline-activity-header")?.getAttribute("aria-expanded")).toBe("false")
    expect(handle.container.textContent).not.toContain("读取文件")
  } finally {
    handle.unmount()
  }
})

test("Compose 运行状态出现在 run-status-live 并带阶段信息", () => {
  const interactive = makeInteractive({
    currentThreadId: "thread-1",
    activeRun: { threadId: "thread-1", runId: "run-1" },
    activity: { kind: "running" },
    runProgress: { phase: "model", elapsedMs: 18_000 },
    composeState: {
      threadId: "thread-1",
      slug: "search",
      complexity: "simple",
      status: "active",
      currentStage: "implement",
      waiting: "none",
      stages: [
        { id: "requirement", state: "confirmed" },
        { id: "spec", state: "skipped" },
        { id: "plan", state: "confirmed" },
        { id: "implement", state: "current" },
        { id: "review", state: "pending" },
      ],
      documents: [],
      fixRounds: 0,
      revision: 2,
    },
  })
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
  )
  try {
    const live = handle.container.querySelector(".run-status-live")
    expect(live?.textContent).toContain("Compose")
    expect(live?.textContent).toContain("实现")
    expect(live?.textContent).toContain("Esc 取消")
    // 五阶段条仍在，但位于 live status 附近（同容器内）
    expect(handle.container.querySelector(".compose-progress")).not.toBeNull()
  } finally {
    handle.unmount()
  }
})

test("Compose 进度面板渲染五阶段、当前任务与 blocked 摘要", async () => {
  const interactive = makeInteractive({
    timeline: [],
    composeState: {
      threadId: "thread-1",
      slug: "search",
      complexity: "simple",
      status: "waiting_user",
      currentStage: "implement",
      waiting: "ask_user",
      stages: [
        { id: "requirement", state: "confirmed" },
        { id: "spec", state: "skipped" },
        { id: "plan", state: "confirmed" },
        { id: "implement", state: "failed" },
        { id: "review", state: "pending" },
      ],
      documents: [],
      fixRounds: 0,
      revision: 5,
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const column = handle.container.querySelector(".timeline-column")
    const progress = handle.container.querySelector(".compose-progress")
    const timeline = handle.container.querySelector(".timeline")
    expect(progress).not.toBeNull()
    expect(column?.firstElementChild).toBe(progress)
    expect(timeline?.contains(progress)).toBeFalse()
    const text = progress?.textContent ?? ""
    expect(text).toContain("需求")
    expect(text).toContain("规格")
    expect(text).toContain("计划")
    expect(text).toContain("实现")
    expect(text).toContain("检视")
    expect(text).toContain("失败")
    expect(text).not.toContain("✓")
    expect(text).not.toContain("▸")
    expect(progress?.querySelector(".compose-chip-failed")).not.toBeNull()
    expect(progress?.querySelector(".compose-track-filled")).not.toBeNull()
  } finally {
    handle.unmount()
  }
})

test("Compose blocked 投影展示阻塞原因", async () => {
  const interactive = makeInteractive({
    timeline: [],
    composeState: {
      threadId: "thread-1",
      slug: "search",
      complexity: "simple",
      status: "waiting_user",
      currentStage: "review",
      waiting: "review_confirm",
      stages: [
        { id: "requirement", state: "confirmed" },
        { id: "spec", state: "skipped" },
        { id: "plan", state: "confirmed" },
        { id: "implement", state: "confirmed" },
        { id: "review", state: "current" },
      ],
      documents: [],
      fixRounds: 0,
      revision: 7,
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const text = handle.container.querySelector(".compose-progress")?.textContent ?? ""
    expect(text).toContain("检视")
    expect(text).toContain("等你确认")
  } finally {
    handle.unmount()
  }
})

test("Compose 失败后 Web 渲染冻结的终态阶段面板", async () => {
  const interactive = makeInteractive({
    timeline: [],
    activeRun: null,
    lastRun: {
      runId: "run-1",
      outcome: "failed",
      composeSummary: {
        threadId: "thread-1",
        slug: "search",
        complexity: "simple",
        status: "waiting_user",
        currentStage: "implement",
        waiting: "none",
        stages: [
          { id: "requirement", state: "confirmed" },
          { id: "spec", state: "skipped" },
          { id: "plan", state: "confirmed" },
          { id: "implement", state: "failed" },
          { id: "review", state: "pending" },
        ],
        documents: [],
        fixRounds: 0,
        revision: 2,
      },
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const text = handle.container.querySelector(".compose-progress")?.textContent ?? ""
    expect(text).toContain("需求")
    expect(text).toContain("检视")
    expect(text).toContain("失败")
  } finally {
    handle.unmount()
  }
})

test("子时间线模式渲染只读顶栏与返回主对话按钮", () => {
  const interactive = makeInteractive({
    childTimelineExecutionId: "child-123",
    timeline: [toolItem({ id: "t1", name: "read_file" })],
  })
  const intents: WebIntent[] = []
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={intent => intents.push(intent)} />,
  )
  try {
    const banner = handle.container.querySelector(".child-timeline-banner")
    expect(banner).not.toBeNull()
    expect(banner?.textContent).toContain("子代理时间线（只读）")
    const backBtn = banner?.querySelector<HTMLButtonElement>(".child-timeline-back-btn")
    expect(backBtn).not.toBeNull()
    act(() => { backBtn?.click() })
    expect(intents).toEqual([{ type: "child-timeline-leave" }])
  } finally {
    handle.unmount()
  }
})

test("子时间线无事件且运行中显示专用空态并保留返回入口", () => {
  const interactive = makeInteractive({
    currentThreadId: "thread-1",
    childTimelineExecutionId: "child-not-started",
    activeRun: { threadId: "thread-1", runId: "run-1" },
    activity: { kind: "running", label: "正在运行" },
    timeline: [],
  })
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
  )
  try {
    expect(handle.container.textContent).toContain("子代理刚开始，暂无过程")
    expect(handle.container.textContent).not.toContain("发送第一条消息")
    expect(handle.container.querySelector(".child-timeline-back-btn")).not.toBeNull()
  } finally {
    handle.unmount()
  }
})

test("子时间线终结且无事件显示诊断空态并保留返回入口", () => {
  const interactive = makeInteractive({
    currentThreadId: "thread-1",
    childTimelineExecutionId: "child-missing-events",
    activity: { kind: "completed", label: "已完成" },
    timeline: [],
  })
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
  )
  try {
    expect(handle.container.textContent).toContain("未收到该子代理的过程事件")
    expect(handle.container.textContent).not.toContain("发送第一条消息")
    expect(handle.container.querySelector(".child-timeline-back-btn")).not.toBeNull()
  } finally {
    handle.unmount()
  }
})

test("task 派出卡展开后展示进入子时间线按钮并可点击", () => {
  const interactive = makeInteractive({
    timeline: [
      {
        type: "tool",
        tool: {
          id: "t-task",
          runId: "run-1",
          name: "task",
          arguments: JSON.stringify({ description: "查定义", subagent_type: "general-purpose" }),
          output: "ok",
          status: "completed",
          childExecutionId: "child-abc",
          childAgentId: "general-purpose",
        },
      },
    ],
  })
  const intents: WebIntent[] = []
  const Harness = (): ReactElement => {
    const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(() => makeSnapshot({ interactive }))
    const dispatch = (intent: WebIntent): void => {
      intents.push(intent)
      if (intent.type === "tool-toggle") {
        setSnapshot(prev => ({
          ...prev,
          expandedTools: new Set(prev.expandedTools).add(toolKey(intent.runId, intent.toolId)),
        }))
      }
    }
    return <Timeline snapshot={snapshot} dispatch={dispatch} />
  }
  const handle = render(<Harness />)
  try {
    act(() => { handle.container.querySelector<HTMLButtonElement>(".tool-row-header")?.click() })
    const btn = handle.container.querySelector<HTMLButtonElement>(".child-timeline-open-btn")
    expect(btn).not.toBeNull()
    expect(btn?.textContent).toContain("进入子时间线")
    act(() => { btn?.click() })
    expect(intents).toContainEqual({ type: "child-timeline-open", executionId: "child-abc" })
  } finally {
    handle.unmount()
  }
})
