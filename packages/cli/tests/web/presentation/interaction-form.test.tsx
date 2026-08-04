/** InteractionForm：动态 approval 决策、question 单/多选/other、完整答案集合提交。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act, useState } from "react"
import { createElement, type ReactElement } from "react"

import { InteractionForm } from "../../../src/web/presentation/interaction-form"
import type { ApprovalDecision, InteractiveQuestion } from "../../../src/interactive/types"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { render, setControlledValue, type RenderHandle } from "./render"

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
  test("approval 仅显示服务端允许的决策；含 reject_with_feedback 时显示反馈输入", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      interaction: {
        type: "approval",
        requestId: "r-1",
        description: "需要批准",
        requests: [{ tool: "write_file" }],
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
