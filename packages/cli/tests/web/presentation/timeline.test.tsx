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
      expect(intents).toEqual([{ type: "tool-toggle", runId: "run-1", toolId: "t1" }])
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
    const headers = (): NodeListOf<HTMLButtonElement> => handle.container.querySelectorAll<HTMLButtonElement>(".tool-card-header")
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
      expect(live?.textContent).toBe("正在运行")
    } finally {
      handle.unmount()
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

  test("思考段冻结后折叠为标题与展开按钮", () => {
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
      const reasoning = handle.container.querySelector(".reasoning")
      expect(reasoning?.getAttribute("data-active")).toBe("false")
      expect(reasoning?.textContent).toContain("思考")
      expect(reasoning?.querySelector(".reasoning-toggle")?.textContent).toBe("展开")
      expect(reasoning?.textContent).toContain("第一行思考")
      expect(reasoning?.textContent).not.toContain("后续细节")
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
    // 折叠时不渲染组内 Tool 卡
    expect(handle.container.textContent).not.toContain("read_file")
    act(() => {
      header.click()
    })
    expect(handle.container.querySelector(".timeline-activity-header")?.getAttribute("aria-expanded")).toBe("true")
    expect(handle.container.textContent).toContain("read_file")
    // 再次点击折叠
    act(() => {
      header.click()
    })
    expect(handle.container.querySelector(".timeline-activity-header")?.getAttribute("aria-expanded")).toBe("false")
    expect(handle.container.textContent).not.toContain("read_file")
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
      revision: 2,
      stage: "build",
      status: "running",
      stages: [
        { id: "understand", status: "passed", attempts: 1 },
        { id: "plan", status: "passed", attempts: 1 },
        { id: "build", status: "running", attempts: 1 },
        { id: "verify", status: "pending", attempts: 0 },
        { id: "review", status: "pending", attempts: 0 },
      ],
      tasks: [{ id: "task-1", title: "实现搜索", status: "running" }],
      evidence: [],
      blockedReason: null,
    },
  })
  const handle = render(
    <Timeline snapshot={makeSnapshot({ interactive })} dispatch={() => {}} />,
  )
  try {
    const live = handle.container.querySelector(".run-status-live")
    expect(live?.textContent).toContain("Compose")
    expect(live?.textContent).toContain("构建")
    expect(live?.textContent).toContain("实现搜索")
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
      revision: 5,
      stage: "verify",
      status: "running",
      stages: [
        { id: "understand", status: "passed", attempts: 1 },
        { id: "plan", status: "passed", attempts: 1 },
        { id: "build", status: "passed", attempts: 1 },
        { id: "verify", status: "running", attempts: 1 },
        { id: "review", status: "pending", attempts: 0 },
      ],
      tasks: [
        { id: "task-1", title: "实现搜索", status: "passed" },
        { id: "task-2", title: "补充文档", status: "pending" },
      ],
      evidence: [{ label: "pytest -q tests/test_search.py", status: "failed" }],
      blockedReason: null,
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const progress = handle.container.querySelector(".compose-progress")
    expect(progress).not.toBeNull()
    const text = progress?.textContent ?? ""
    expect(text).toContain("理解")
    expect(text).toContain("验证")
    expect(text).toContain("补充文档")
    expect(text).toContain("pytest -q")
    expect(text).toContain("rev 5")
  } finally {
    handle.unmount()
  }
})

test("Compose blocked 投影展示阻塞原因", async () => {
  const interactive = makeInteractive({
    timeline: [],
    composeState: {
      revision: 7,
      stage: "verify",
      status: "blocked",
      stages: [
        { id: "understand", status: "passed", attempts: 1 },
        { id: "plan", status: "passed", attempts: 1 },
        { id: "build", status: "passed", attempts: 1 },
        { id: "verify", status: "blocked", attempts: 3 },
        { id: "review", status: "pending", attempts: 0 },
      ],
      tasks: [],
      evidence: [],
      blockedReason: "verify fix budget exhausted",
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const text = handle.container.querySelector(".compose-progress")?.textContent ?? ""
    expect(text).toContain("阻塞")
    expect(text).toContain("verify fix budget exhausted")
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
        revision: 2,
        stage: "understand",
        status: "failed",
        stages: [
          { id: "understand", status: "failed", attempts: 2 },
          { id: "plan", status: "pending", attempts: 0 },
          { id: "build", status: "pending", attempts: 0 },
          { id: "verify", status: "pending", attempts: 0 },
          { id: "review", status: "pending", attempts: 0 },
        ],
        tasks: [],
        evidence: [],
        blockedReason: null,
      },
    },
  })
  const handle = render(createElement(Timeline, { snapshot: makeSnapshot({ interactive }), dispatch: () => {} }))
  try {
    const text = handle.container.querySelector(".compose-progress")?.textContent ?? ""
    expect(text).toContain("理解")
    expect(text).toContain("验证")
    expect(text).toContain("rev 2")
  } finally {
    handle.unmount()
  }
})
