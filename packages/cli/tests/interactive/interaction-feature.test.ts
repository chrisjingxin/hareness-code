import { expect, test } from "bun:test"
import { makeHarness, flush, notices, manualScheduler, approvalRequest, questionRequest } from "./harness"

test("approval 校验 decisions allowlist，reject_with_feedback 携带反馈", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "审批" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId, ["approve_once", "reject_with_feedback"]))
    expect(harness.controller.getSnapshot().interaction?.type).toBe("approval")

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "approval-1",
      response: { kind: "approval", decision: "approve_project" },
    })
    // 非法 decision 不发送响应，Interaction 保持等待。
    expect(notices(harness.controller.getSnapshot())).toContain("不支持的审批决定")
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "approval-1",
      response: { kind: "approval", decision: "reject_with_feedback", feedback: "理由不足" },
    })
    expect(await responsePromise).toMatchObject({ type: "approval", request_id: "approval-1", decision: "reject_with_feedback", feedback: "理由不足" })
  } finally {
    await harness.controller.close()
  }
})

test("approval 把 file_diff presentation 原样投影到共享 DTO", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "审批 diff" })
    const run = harness.runHandles.at(-1)!
    const request = approvalRequest(run.threadId, run.runId)
    if (request.type !== "approval") throw new Error("expected approval")
    request.payload.presentation = {
      kind: "file_diff",
      operation: "edit",
      path: "/src/a.ts",
      added_lines: 1,
      removed_lines: 1,
      truncated: false,
      unified_diff: "--- /src/a.ts\n+++ /src/a.ts\n@@ -1 +1 @@\n-old\n+new",
    }
    const responsePromise = harness.port.sendInteraction(request)

    const interaction = harness.controller.getSnapshot().interaction
    expect(interaction?.type).toBe("approval")
    if (interaction?.type !== "approval") throw new Error("expected approval dto")
    expect(interaction.presentation).toEqual(request.payload.presentation)

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: interaction.requestId,
      response: { kind: "approval", decision: "reject" },
    })
    await responsePromise
  } finally {
    await harness.controller.close()
  }
})

test("question 完整 schema：多题、多选、other answer 与非法响应", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "提问" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(questionRequest(run.threadId, run.runId))
    const interaction = harness.controller.getSnapshot().interaction
    expect(interaction?.type).toBe("question")
    if (interaction?.type !== "question") throw new Error("expected question")
    expect(interaction.questions).toHaveLength(2)
    expect(interaction.deadlineAtMs).toBeGreaterThan(0)

    // 缺少声明问题：不发送 RPC，Interaction 保持等待。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: [] } },
    })
    expect(notices(harness.controller.getSnapshot())).toContain("缺少问题")
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    // 非法选项：不发送 RPC。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow", "not-allowed"] } },
    })
    expect(notices(harness.controller.getSnapshot())).toContain("无效选项")

    // 合法多选 + other answer 后正常回写。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["自由文本"], level: ["shallow", "deep"] } },
    })
    expect(await responsePromise).toMatchObject({ answers: { scope: ["自由文本"], level: ["shallow", "deep"] } })

    // stale request ID 不产生 RPC：新的 Interaction 到达后仍保持 pending。
    const stalePromise = harness.port.sendInteraction(questionRequest(run.threadId, run.runId))
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "stale-request",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow"] } },
    })
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow"] } },
    })
    await expect(stalePromise).resolves.toMatchObject({ type: "question", answers: { scope: ["src"], level: ["shallow"] } })
  } finally {
    await harness.controller.close()
  }
})

test("Interaction timeout 按 scheduler 收敛为 fail-closed 响应", async () => {
  const manual = manualScheduler()
  const harness = makeHarness({ scheduler: manual.scheduler })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "超时审批" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    manual.runAll()
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.interaction).toBeNull()
    expect(await responsePromise).toMatchObject({ type: "approval", decision: "reject" })
  } finally {
    await harness.controller.close()
  }
})

test("timeout_ms=0 的 Interaction 立即收敛，不残留 pending 状态", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "零超时审批" })
    const run = harness.runHandles.at(-1)!
    const request = {
      ...approvalRequest(run.threadId, run.runId),
      timeout_ms: 0,
    }
    const responsePromise = harness.port.sendInteraction(request)
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.interaction).toBeNull()
    expect(await responsePromise).toMatchObject({ type: "approval", decision: "reject" })
  } finally {
    await harness.controller.close()
  }
})
