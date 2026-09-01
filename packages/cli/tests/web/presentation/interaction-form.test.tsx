/** InteractionForm：动态 approval 决策、question 单/多选/other、完整答案集合提交。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act, useState } from "react"
import { createElement, type ReactElement } from "react"

import { InteractionForm } from "../../../src/web/presentation/interaction-form"
import { defaultDiffModeForWidth } from "../../../src/web/presentation/file-diff-approval"
import type { ApprovalDecision, InteractiveQuestion } from "../../../src/interactive/types"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { registerTestDom, render, setControlledValue, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function HarnessDraft(props: {
  initial: WebAdapterSnapshot
  intents: WebIntent[]
}): ReactElement {
  const [snapshot, setSnapshot] = useState<WebAdapterSnapshot>(props.initial)
  const dispatch = (intent: WebIntent): void => {
    props.intents.push(intent)
    if (intent.type === "interaction-draft-change" && intent.patch.kind === "answer") {
      setSnapshot(prev => {
        const previous = prev.interactionDraft
        const nextDraft = previous
          ? { ...previous, answers: { ...previous.answers, [intent.patch.questionId]: intent.patch.values.slice() }, touched: true }
          : { requestId: intent.requestId, feedback: "", answers: { [intent.patch.questionId]: intent.patch.values.slice() }, touched: true }
        return { ...prev, interactionDraft: nextDraft }
      })
    }
  }
  return <InteractionForm snapshot={snapshot} dispatch={dispatch} />
}

function mountForm(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <InteractionForm snapshot={snapshot} dispatch={intent => intents.push(intent)} />,
  )
}

const OTHER_QUESTION: InteractiveQuestion = {
  id: "q1",
  question: "选择颜色",
  header: "颜色",
  body: "可多选并补充",
  options: [
    { label: "红", value: "red", description: "" },
    { label: "蓝", value: "blue", description: "" },
  ],
  multiSelect: true,
  allowOther: true,
}

const SINGLE_QUESTION: InteractiveQuestion = {
  id: "q1",
  question: "选择颜色",
  header: "颜色",
  body: "",
  options: [
    { label: "红", value: "red", description: "" },
    { label: "蓝", value: "blue", description: "" },
  ],
  multiSelect: false,
  allowOther: false,
}

const MULTI_QUESTION: InteractiveQuestion = {
  id: "q1",
  question: "选择标签",
  header: "标签",
  body: "",
  options: [
    { label: "前端", value: "fe", description: "" },
    { label: "后端", value: "be", description: "" },
  ],
  multiSelect: true,
  allowOther: false,
}

describe("InteractionForm", () => {
  test("file_diff 显示路径、统计、行号和截断警告，并可切换左右/行内", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "diff-1",
        description: "文件变更需要审批",
        requests: [{ name: "edit_file", args: { file_path: "/src/a.ts" } }],
        presentation: {
          kind: "file_diff",
          operation: "edit",
          path: "/src/a.ts",
          added_lines: 1,
          removed_lines: 1,
          truncated: true,
          unified_diff: "--- /src/a.ts\n+++ /src/a.ts\n@@ -1 +1 @@\n-oldValue\n+newValue",
        },
        decisions: ["approve_once", "reject"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      expect(handle.container.querySelector(".file-diff-path")?.textContent).toBe("/src/a.ts")
      expect(handle.container.querySelector(".file-diff-stats")?.textContent).toContain("+1-1")
      expect(handle.container.querySelector(".file-diff-truncated")?.textContent).toContain("批准仍会应用完整变更")
      expect(handle.container.querySelector(".file-diff-request-details")?.hasAttribute("open")).toBe(false)
      const inline = [...handle.container.querySelectorAll<HTMLButtonElement>(".file-diff-mode button")]
        .find(button => button.textContent === "行内")
      act(() => { inline?.click() })
      expect(handle.container.querySelector(".file-diff-body")?.getAttribute("data-view")).toBe("unified")
      expect(handle.container.querySelector(".diff-remove")?.textContent).toContain("oldValue")
      expect(handle.container.querySelector(".diff-add")?.textContent).toContain("newValue")
    } finally {
      handle.unmount()
    }
  })

  test("child 审批标题使用子代理名称", () => {
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "child-1",
        description: "执行 shell 命令",
        requests: [],
        presentation: null,
        decisions: ["approve_once", "reject"],
        deadlineAtMs: Date.now() + 60_000,
        agentId: "general-purpose",
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), [])
    try {
      expect(handle.container.querySelector(".interaction-title")?.textContent).toBe("子代理 general-purpose 需要审批")
    } finally {
      handle.unmount()
    }
  })

  test("file_diff 切换展示模式后点击拒绝会立即提交", async () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "diff-reject",
        description: "文件变更需要审批",
        requests: [{ name: "edit_file", args: { file_path: "/src/a.ts" } }],
        presentation: {
          kind: "file_diff",
          operation: "edit",
          path: "/src/a.ts",
          added_lines: 1,
          removed_lines: 1,
          truncated: false,
          unified_diff: "--- /src/a.ts\n+++ /src/a.ts\n@@ -1 +1 @@\n-oldValue\n+newValue",
        },
        decisions: ["approve_once", "reject"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const inline = [...handle.container.querySelectorAll<HTMLButtonElement>(".file-diff-mode button")]
        .find(button => button.textContent === "行内")
      const reject = [...handle.container.querySelectorAll<HTMLButtonElement>(".approval-buttons button")]
        .find(button => button.textContent === "拒绝")
      await act(async () => {
        inline?.click()
        reject?.click()
        await Promise.resolve()
      })
      expect(handle.container.querySelector(".file-diff-body")?.getAttribute("data-view")).toBe("unified")
      const sent = intents.find(intent => intent.type === "interaction-submit")
      expect(sent).toEqual({
        type: "interaction-submit",
        requestId: "diff-reject",
        response: { kind: "approval", decision: "reject" },
      })
    } finally {
      handle.unmount()
    }
  })

  test("file_diff 畸形时显示有界原文，空文件显示明确提示", () => {
    const base = {
      type: "approval" as const,
      requestId: "diff-fallback",
      description: "文件变更需要审批",
      requests: [],
      decisions: ["approve_once" as const, "reject" as const],
      deadlineAtMs: Date.now() + 60_000,
    }
    const malformed = mountForm(makeSnapshot({ interactive: makeInteractive({ interaction: {
      ...base,
      presentation: { kind: "file_diff", operation: "edit", path: "/a.ts", added_lines: 1, removed_lines: 1, truncated: false, unified_diff: "not a diff" },
    } }) }), [])
    try {
      expect(malformed.container.querySelector(".file-diff-fallback")?.textContent).toContain("not a diff")
    } finally {
      malformed.unmount()
    }

    const empty = mountForm(makeSnapshot({ interactive: makeInteractive({ interaction: {
      ...base,
      requestId: "diff-empty",
      presentation: { kind: "file_diff", operation: "write", path: "/empty.txt", added_lines: 0, removed_lines: 0, truncated: false, unified_diff: "" },
    } }) }), [])
    try {
      expect(empty.container.querySelector(".file-diff-empty")?.textContent).toContain("创建空文件")
    } finally {
      empty.unmount()
    }
  })

  test("file_diff 对删除操作显示独立语义和逻辑路径", () => {
    const deletion = mountForm(makeSnapshot({ interactive: makeInteractive({ interaction: {
      type: "approval",
      requestId: "diff-delete",
      description: "文件变更需要审批",
      requests: [],
      presentation: {
        kind: "file_diff",
        operation: "delete",
        path: "/obsolete.ts",
        added_lines: 0,
        removed_lines: 1,
        truncated: false,
        unified_diff: "--- /obsolete.ts\n+++ /obsolete.ts\n@@ -1 +0,0 @@\n-export const obsolete = true",
      },
      decisions: ["approve_once", "reject"],
      deadlineAtMs: Date.now() + 60_000,
    } }) }), [])
    try {
      expect(deletion.container.querySelector(".file-diff-approval")?.getAttribute("aria-label")).toBe("删除文件 /obsolete.ts")
      expect(deletion.container.querySelector(".file-diff-stats")?.textContent).toContain("+0-1")
    } finally {
      deletion.unmount()
    }
  })

  test("file_diff 响应式默认断点为 760px", () => {
    expect(defaultDiffModeForWidth(759)).toBe("unified")
    expect(defaultDiffModeForWidth(760)).toBe("split")
  })

  test("approval 仅显示服务端允许的决策；含 reject_with_feedback 时显示反馈输入", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "r-1",
        description: "需要批准",
        requests: [{ tool: "write_file" }],
        presentation: null,
        decisions: ["approve_once", "reject_with_feedback"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const buttons = handle.container.querySelectorAll<HTMLButtonElement>(".approval-buttons button")
      const labels = [...buttons].map(button => button.textContent?.trim())
      expect(labels).toContain("允许一次")
      expect(labels).toContain("拒绝并反馈")
      expect(labels).not.toContain("允许此会话")
      expect(handle.container.querySelector(".interaction-feedback-input")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("approval 点击 allow 决策后再次点击提交 dispatch interaction-submit（kind=approval）", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "r-1",
        description: "写文件",
        requests: [{ tool: "write_file" }],
        presentation: null,
        decisions: ["approve_once", "reject"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const allow = [...handle.container.querySelectorAll<HTMLButtonElement>(".approval-buttons button")]
        .find(button => button.textContent?.includes("允许一次"))
      expect(allow).toBeDefined()
      act(() => { allow?.click() })
      const submit = handle.container.querySelector<HTMLButtonElement>(".interaction-submit")
      expect(submit?.disabled).toBe(false)
      act(() => { submit?.click() })
      const sent = intents.find(intent => intent.type === "interaction-submit")
      expect(sent).toBeDefined()
      if (sent && sent.type === "interaction-submit") {
        expect(sent.requestId).toBe("r-1")
        expect(sent.response.kind).toBe("approval")
        if (sent.response.kind === "approval") {
          expect(sent.response.decision).toBe("approve_once" satisfies ApprovalDecision)
        }
      }
    } finally {
      handle.unmount()
    }
  })

  test("approval 仅显示服务端 decisions；不存在 reject_with_feedback 时不显示反馈输入", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "r-1",
        description: "读文件",
        requests: [{ tool: "read_file" }],
        presentation: null,
        decisions: ["approve_once", "reject"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const buttons = handle.container.querySelectorAll<HTMLButtonElement>(".approval-buttons button")
      expect(buttons.length).toBe(2)
      expect(handle.container.querySelector(".interaction-feedback-input")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("question 一次性渲染多题；单选用 radio，多选用 checkbox", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "question",
        requestId: "r-2",
        questions: [
          { ...SINGLE_QUESTION, id: "q-single" },
          { ...MULTI_QUESTION, id: "q-multi" },
        ],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const fieldsets = handle.container.querySelectorAll("fieldset.question-group")
      expect(fieldsets.length).toBe(2)
      const singleGroup = fieldsets[0]
      expect(singleGroup?.querySelector("input[type=radio]")).not.toBeNull()
      const multiGroup = fieldsets[1]
      expect(multiGroup?.querySelector("input[type=checkbox]")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("question allowOther 渲染 free-text 输入；提交前按钮 disabled", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "question",
        requestId: "r-3",
        questions: [OTHER_QUESTION],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const text = handle.container.querySelector<HTMLInputElement>("input.question-option-other-input")
      expect(text).not.toBeNull()
      const submit = handle.container.querySelector<HTMLButtonElement>(".interaction-submit")
      expect(submit?.disabled).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("question 仅选择 Other 时填写文本后可提交，并把文本放入 answers map", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "question",
        requestId: "r-other-submit",
        questions: [OTHER_QUESTION],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = render(
      <HarnessDraft initial={makeSnapshot({ interactive })} intents={intents} />,
    )
    try {
      const other = handle.container.querySelector<HTMLInputElement>("input.question-option-other-input")
      const choice = handle.container.querySelector<HTMLInputElement>(`input[value="__other__"]`)
      act(() => { choice?.click() })
      expect(handle.container.querySelector<HTMLButtonElement>(".interaction-submit")?.disabled).toBe(true)
      act(() => { setControlledValue(other!, "自定义颜色") })
      expect(handle.container.querySelector<HTMLButtonElement>(".interaction-submit")?.disabled).toBe(false)
      act(() => { handle.container.querySelector<HTMLButtonElement>(".interaction-submit")?.click() })
      const submitted = intents.find(intent => intent.type === "interaction-submit")
      expect(submitted).toBeDefined()
      if (submitted?.type === "interaction-submit" && submitted.response.kind === "question") {
        expect(submitted.response.answers).toEqual({ "q1": ["自定义颜色"] })
      }
    } finally {
      handle.unmount()
    }
  })

  test("question 全部必答后按钮 enabled；提交时 dispatch answers map（Record<string,string[]>）", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "question",
        requestId: "r-4",
        questions: [
          { ...MULTI_QUESTION, id: "q-multi" },
          { ...SINGLE_QUESTION, id: "q-single" },
        ],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const initialDraft: WebAdapterSnapshot = makeSnapshot({ interactive })
    const handle = render(
      <HarnessDraft initial={initialDraft} intents={intents} />,
    )
    try {
      const fieldsets = handle.container.querySelectorAll("fieldset.question-group")
      const multiGroup = fieldsets[0]
      const fe = multiGroup?.querySelector<HTMLInputElement>("input[type=checkbox][value=fe]")
      act(() => { fe?.click() })
      const singleGroup = fieldsets[1]
      const red = singleGroup?.querySelector<HTMLInputElement>("input[type=radio][value=red]")
      act(() => { red?.click() })
      const submit = handle.container.querySelector<HTMLButtonElement>(".interaction-submit")
      expect(submit?.disabled).toBe(false)
      act(() => { submit?.click() })
      const sent = intents.find(intent => intent.type === "interaction-submit")
      expect(sent).toBeDefined()
      if (sent && sent.type === "interaction-submit") {
        expect(sent.requestId).toBe("r-4")
        expect(sent.response.kind).toBe("question")
        if (sent.response.kind === "question") {
          expect(sent.response.answers["q-multi"]).toEqual(["fe"])
          expect(sent.response.answers["q-single"]).toEqual(["red"])
        }
      }
    } finally {
      handle.unmount()
    }
  })

  test("interaction-draft-change 在 question 选项切换时被 dispatch", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "question",
        requestId: "r-5",
        questions: [SINGLE_QUESTION],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const red = handle.container.querySelector<HTMLInputElement>("input[type=radio][value=red]")
      act(() => { red?.click() })
      const draftChange = intents.find(intent => intent.type === "interaction-draft-change")
      expect(draftChange).toBeDefined()
      if (draftChange && draftChange.type === "interaction-draft-change") {
        expect(draftChange.requestId).toBe("r-5")
        expect(draftChange.patch.kind).toBe("answer")
      }
    } finally {
      handle.unmount()
    }
  })

  test("plan 有正文时按 markdown 渲染标题，而不是原样展示 #", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "plan",
        requestId: "plan-md",
        revision: 1,
        hasPlan: true,
        planMarkdown: "# 简化版 wc\n\n**目标**是统计行数。",
        planVirtualPath: "/.harness/plan.md",
        planDisplayPath: "~/.harness/plans/thread-1.md",
        decisions: ["approved", "revise", "abandoned"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const preview = handle.container.querySelector(".plan-preview")
      expect(preview?.querySelector(".markdown")).not.toBeNull()
      expect(preview?.querySelector("h2")?.textContent).toBe("简化版 wc")
      expect(preview?.querySelector("strong")?.textContent).toBe("目标")
      expect(preview?.textContent).not.toContain("# 简化版 wc")
    } finally {
      handle.unmount()
    }
  })

  test("plan 可选原始 Markdown 行范围写批注，打回时编入 feedback", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "plan",
        requestId: "plan-annotation",
        revision: 1,
        hasPlan: true,
        planMarkdown: "# 方案\n保留协议\n替换界面\n补充测试",
        planVirtualPath: "/.harness/plan.md",
        planDisplayPath: "~/.harness/plans/thread-1.md",
        decisions: ["approved", "revise", "abandoned"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      const annotate = [...handle.container.querySelectorAll<HTMLButtonElement>("button")]
        .find(button => button.textContent?.includes("添加批注"))
      act(() => { annotate?.click() })
      const lines = handle.container.querySelectorAll<HTMLButtonElement>(".plan-annotation-line")
      act(() => {
        lines[1]?.click()
        lines[2]?.click()
      })
      const comment = handle.container.querySelector<HTMLTextAreaElement>("textarea[aria-label='行批注意见']")
      act(() => { setControlledValue(comment!, "这两步保持原子交付") })
      const save = [...handle.container.querySelectorAll<HTMLButtonElement>("button")]
        .find(button => button.textContent?.includes("保存批注"))
      act(() => { save?.click() })
      expect(handle.container.textContent).toContain("1 条批注")

      const revise = [...handle.container.querySelectorAll<HTMLButtonElement>(".approval-button")]
        .find(button => button.textContent?.includes("继续打磨"))
      act(() => { revise?.click() })
      const submitted = intents.find(intent => intent.type === "interaction-submit")
      expect(submitted).toMatchObject({
        type: "interaction-submit",
        requestId: "plan-annotation",
        response: {
          kind: "plan",
          decision: "revise",
        },
      })
      if (submitted?.type === "interaction-submit" && submitted.response.kind === "plan") {
        expect(submitted.response.feedback).toContain("Proposed plan lines 2-3:")
        expect(submitted.response.feedback).toContain("> 保留协议\n> 替换界面")
        expect(submitted.response.feedback).toContain("Comment: 这两步保持原子交付")
      }
    } finally {
      handle.unmount()
    }
  })

  test("plan 交互卡显示三个中文动作和空计划占位，批准会提交", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "plan",
        requestId: "plan-1",
        revision: 1,
        hasPlan: false,
        planMarkdown: "",
        planVirtualPath: "/.harness/plan.md",
        planDisplayPath: "~/.harness/plans/thread-1.md",
        decisions: ["approved", "revise", "abandoned"],
        deadlineAtMs: Date.now() + 60_000,
      },
    })
    const handle = mountForm(makeSnapshot({ interactive }), intents)
    try {
      expect(handle.container.textContent).toContain("还没有写出计划")
      const labels = [...handle.container.querySelectorAll(".approval-button span")].map(node => node.textContent)
      expect(labels).toContain("批准并开始实现")
      expect(labels).toContain("继续打磨")
      expect(labels).toContain("放弃计划")
      expect(labels.some(label => label?.includes("auto-edit"))).toBe(false)
      const approve = [...handle.container.querySelectorAll<HTMLButtonElement>(".approval-button")]
        .find(button => button.textContent?.includes("批准并开始实现"))
      act(() => { approve?.click() })
      expect(intents).toContainEqual({
        type: "interaction-submit",
        requestId: "plan-1",
        response: { kind: "plan", decision: "approved", feedback: undefined },
      })
    } finally {
      handle.unmount()
    }
  })

  test("无 interaction 时 InteractionForm 渲染空占位", () => {
    const intents: WebIntent[] = []
    const handle = mountForm(makeSnapshot(), intents)
    try {
      expect(handle.container.querySelector(".interaction-card")).toBeNull()
    } finally {
      handle.unmount()
    }
  })
})
