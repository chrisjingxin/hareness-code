/** Timeline：用户/Assistant/Tool/Interaction 时间线 + Tool 折叠 dispatch + ARIA 属性。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act, useState } from "react"
import { createElement, type ReactElement } from "react"

import { Timeline } from "../../../src/web/presentation/timeline"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import type {
  ConversationMessage,
  InteractionCard,
  TimelineItem,
  ToolCard,
} from "../../../src/interactive/state"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { render, type RenderHandle } from "./render"

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
      expect(handle.container.querySelector(".tool-card")).not.toBeNull()
      expect(handle.container.querySelector(".interaction-card")).not.toBeNull()
      expect(handle.container.textContent).toContain("你好")
      expect(handle.container.textContent).toContain("我很好")
      expect(handle.container.textContent).toContain("read_file")
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
      const card = handle.container.querySelector<HTMLDivElement>(".tool-card")
      expect(card).not.toBeNull()
      expect(card?.querySelector(".tool-card-name")?.textContent).toBe("read_file")
      expect(card?.querySelector(".tool-details")).toBeNull()
      const header = card?.querySelector<HTMLButtonElement>(".tool-card-header")
      expect(header?.getAttribute("aria-expanded")).toBe("false")
      act(() => { header?.click() })
      expect(intents).toEqual([{ type: "tool-toggle", toolId: "t1" }])
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
            expandedTools: new Set(prev.expandedTools).add(intent.toolId),
          }))
        }
      }
      return <Timeline snapshot={snapshot} dispatch={dispatch} />
    }
    const handle = render(<Harness />)
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-card-header")
      act(() => { header?.click() })
      const details = handle.container.querySelector(".tool-details")
      expect(details).not.toBeNull()
      const pres = details?.querySelectorAll("pre.tool-details-pre")
      expect(pres?.length).toBe(2)
      expect(pres?.[0]?.textContent).toBe("{\"cmd\":\"ls\"}")
      expect(pres?.[1]?.textContent).toBe("a.txt")
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
      const cards = handle.container.querySelectorAll(".tool-card")
      expect(cards.length).toBe(2)
      expect(cards[0]?.getAttribute("data-tool-id")).toBe("t1")
      expect(cards[1]?.getAttribute("data-tool-id")).toBe("t2")
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
      expect(live?.textContent).toBe("正在思考")
    } finally {
      handle.unmount()
    }
  })
})
