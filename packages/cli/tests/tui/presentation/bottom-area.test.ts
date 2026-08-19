import { expect, test } from "bun:test"

import { bottomAreaKind } from "../../../src/tui/presentation/bottom-area"

test("无 Interaction 时底部是输入栏", () => {
  expect(bottomAreaKind(null)).toBe("input")
})

test("审批 pending 时底部是 ApprovalDock", () => {
  expect(bottomAreaKind({
    type: "approval",
    requestId: "a1",
    description: "执行命令",
    requests: {},
    presentation: null,
    decisions: ["approve_once", "reject"],
    deadlineAtMs: 1,
  })).toBe("approval")
})

test("单选且无其他项的问答走 QuestionDock", () => {
  expect(bottomAreaKind({
    type: "question",
    requestId: "q1",
    questions: [{
      id: "fmt",
      question: "格式？",
      header: "",
      body: "",
      options: [{ label: "JSON", value: "json", description: "" }],
      multiSelect: false,
      allowOther: false,
    }],
    deadlineAtMs: 1,
  })).toBe("question")
})

test("开放式问答也走 QuestionDock，不再只改输入栏占位", () => {
  expect(bottomAreaKind({
    type: "question",
    requestId: "q2",
    questions: [{
      id: "open",
      question: "要分析哪个本地 .txt 文件？",
      header: "",
      body: "",
      options: [],
      multiSelect: false,
      allowOther: true,
    }],
    deadlineAtMs: 1,
  })).toBe("question")
})

test("多选问答也走 QuestionDock", () => {
  expect(bottomAreaKind({
    type: "question",
    requestId: "q3",
    questions: [{
      id: "scope",
      question: "选目录",
      header: "",
      body: "",
      options: [{ label: "src", value: "src", description: "" }],
      multiSelect: true,
      allowOther: false,
    }],
    deadlineAtMs: 1,
  })).toBe("question")
})

test("多题相关问答走 QuestionDock，即使第一题是文本", () => {
  expect(bottomAreaKind({
    type: "question",
    requestId: "q-multi",
    questions: [
      {
        id: "question-1",
        question: "输入路径怎么给？",
        header: "",
        body: "",
        options: [],
        multiSelect: false,
        allowOther: true,
      },
      {
        id: "question-2",
        question: "语言？",
        header: "",
        body: "",
        options: [{ label: "Python 3", value: "Python 3", description: "" }],
        multiSelect: false,
        allowOther: true,
      },
    ],
    deadlineAtMs: 1,
  })).toBe("question")
})

test("ask_user 式单选即使允许其他项也走 QuestionDock", () => {
  expect(bottomAreaKind({
    type: "question",
    requestId: "q4",
    questions: [{
      id: "question-1",
      question: "你想要什么类型的 Java 示例？",
      header: "",
      body: "",
      options: [{ label: "基础语法示例", value: "基础语法示例", description: "" }],
      multiSelect: false,
      allowOther: true,
    }],
    deadlineAtMs: 1,
  })).toBe("question")
})
