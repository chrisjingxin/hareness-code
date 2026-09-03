/** `/goal` 的 Interactive Core 纵向行为测试。 */

import { Capability, type GoalInteractionRequest } from "@za38/protocol"
import { expect, test } from "bun:test"

import { commandRegistry, defaultCommandContext } from "../../src/interactive/commands"
import { dispatchSlashCommand } from "../../src/interactive/command-dispatcher"
import { flush, makeHarness, notices } from "./harness"

test("/goal 只在 Build 暴露，并把 show 后的额外文本当作目标", () => {
  const goal = commandRegistry.get("goal.manage")!
  expect(goal).toMatchObject({ name: "goal", requirements: { workModes: ["build"] } })
  expect(commandRegistry.availability(goal, defaultCommandContext({ workMode: "build" }))).toEqual({ state: "available" })
  expect(commandRegistry.availability(goal, defaultCommandContext({ workMode: "compose" }))).toEqual({
    state: "hidden",
    reason: "`/goal` 仅在 Build 工作模式可用。",
  })

  expect(dispatchSlashCommand(
    { id: "goal.manage", name: "goal", argument: "show JSON" },
    {
      commandContext: defaultCommandContext({ workMode: "build" }),
      threadId: "thread-1",
      runtimeStatus: "idle",
      idGenerator: { uuid: () => "unused" },
    },
  )).toEqual({ type: "goal", argument: "show JSON" })
})

test("明确目标创建空 Thread、启动内部 proposal，接受后 exactly once 自动续跑", async () => {
  const harness = makeHarness({ capabilities: [Capability.GOAL_READ, Capability.GOAL_MANAGE] })
  let requested: Parameters<typeof harness.port.requestGoal>[0] | undefined
  harness.port.requestGoal = async params => {
    requested = params
    return {
      disposition: "ready",
      pending: {
        request_id: params.request_id,
        kind: "create",
        status: "ready",
        base_goal_id: null,
        base_revision: null,
        input_text: params.input_text,
        proposed_objective: null,
        proposed_assumptions: [],
        proposed_criteria: [],
        created_at_ms: 1,
        updated_at_ms: 1,
        error_code: null,
      },
    }
  }

  const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/goal 完成登录功能" })
  expect(outcome.status).toBe("accepted")
  expect(requested?.input_text).toBe("完成登录功能")
  expect(requested?.thread_id).toBeTruthy()
  expect(harness.controller.getSnapshot().currentThreadId).toBe(requested?.thread_id)
  expect([...harness.runStates.values()].at(-1)?.input.input).toEqual({
    kind: "goal_proposal",
    request_id: requested?.request_id,
  })
  expect(harness.controller.getSnapshot().timeline).toEqual([
    {
      type: "message",
      message: expect.objectContaining({
        role: "user",
        content: "/goal 完成登录功能",
      }),
    },
  ])

  const proposal = harness.runHandles.at(-1)!
  const responsePromise = harness.port.sendInteraction(goalRequest(proposal.threadId, proposal.runId, requested!.request_id))
  await flush()
  const review = harness.controller.getSnapshot().interaction
  expect(review).toMatchObject({
    type: "goal",
    objective: "完成登录功能",
    criteria: ["登录成功后进入首页", "错误密码显示提示"],
  })
  await harness.controller.dispatch({
    type: "interaction.respond",
    requestId: review!.requestId,
    response: { kind: "goal", decision: "accepted" },
  })
  expect(await responsePromise).toEqual({ request_id: requested!.request_id, type: "goal", decision: "accepted" })

  harness.port.completeRunWithContext(proposal.threadId, proposal.runId, {
    goal_continuation: {
      continuation_id: "continue-1",
      goal_id: "goal-1",
      goal_revision: 1,
      reason: "accepted",
    },
  })
  await flush()
  expect(harness.runHandles).toHaveLength(2)
  expect(harness.runHandles.at(-1)?.runId).toBe("continue-1")
  expect([...harness.runStates.values()].at(-1)?.input.input).toEqual({
    kind: "goal_continuation",
    goal_id: "goal-1",
    goal_revision: 1,
    reason: "accepted",
  })
  await flush()
  expect(harness.runHandles).toHaveLength(2)
})

test("用户取消 goal proposal 后保持停止，不把恢复为 ready 的请求立即重新启动", async () => {
  const harness = makeHarness({ capabilities: [Capability.GOAL_READ, Capability.GOAL_MANAGE] })
  try {
    expect((await harness.controller.dispatch({
      type: "input.submit",
      value: "/goal 帮我完成一个目标",
    })).status).toBe("accepted")
    const proposal = harness.runHandles.at(-1)!
    harness.port.inspectGoal = async () => ({
      goal: null,
      pending: pendingProjection("ready"),
      latest_evaluation: null,
    })

    expect((await harness.controller.dispatch({ type: "run.cancel" })).status).toBe("accepted")
    harness.port.cancelRun(proposal.threadId, proposal.runId)
    await flush()

    expect(harness.runHandles).toHaveLength(1)
    expect(harness.controller.getSnapshot().activeRun).toBeNull()
    expect(harness.controller.getSnapshot().goalPending?.status).toBe("ready")
  } finally {
    await harness.controller.close()
  }
})

test("Thread 恢复 Goal projection 但不会自动启动 Run", async () => {
  const harness = makeHarness({
    initialThreadId: "thread-goal",
    openThreadImpl: async threadId => ({
      thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "", latest_message: "", message_count: 0 },
      messages: [],
      plan: { has_plan: false, plan_markdown: "", plan_virtual_path: "/.harness/plan.md", plan_display_path: `~/.harness/plans/${threadId}.md` },
      goal: goalProjection(),
      goal_pending: null,
      goal_activities: [{ activity_id: "activity-1", kind: "proposal", summary: "目标已接受", created_at_ms: 2 }],
    }),
  })
  await flush()
  expect(harness.controller.getSnapshot().goal).toEqual(goalProjection())
  expect(harness.controller.getSnapshot().goalActivities).toHaveLength(1)
  expect(harness.runHandles).toHaveLength(0)
})

test("Thread 恢复 ready/reviewing proposal 时续接审核 Run，不重复显示原命令", async () => {
  for (const status of ["ready", "reviewing"] as const) {
    const harness = makeHarness({
      initialThreadId: `thread-${status}`,
      openThreadImpl: async threadId => ({
        thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "", latest_message: "", message_count: 0 },
        messages: [],
        plan: { has_plan: false, plan_markdown: "", plan_virtual_path: "/.harness/plan.md", plan_display_path: `~/.harness/plans/${threadId}.md` },
        goal: null,
        goal_pending: pendingProjection(status),
        goal_activities: [],
      }),
    })

    await flush()

    expect(harness.runHandles).toHaveLength(1)
    expect([...harness.runStates.values()][0]?.input.input).toEqual({
      kind: "goal_proposal",
      request_id: "request-1",
    })
    expect(harness.controller.getSnapshot().timeline).toEqual([])
  }
})

test("/goal amend 发起同目标修订，pause/resume/clear 走 mutation 且恢复只续跑一次", async () => {
  const harness = makeHarness({
    capabilities: [Capability.GOAL_READ, Capability.GOAL_MANAGE],
    initialThreadId: "thread-goal",
    openThreadImpl: async threadId => ({
      thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "", latest_message: "", message_count: 0 },
      messages: [],
      plan: { has_plan: false, plan_markdown: "", plan_virtual_path: "/.harness/plan.md", plan_display_path: `~/.harness/plans/${threadId}.md` },
      goal: goalProjection(),
      goal_pending: null,
      goal_activities: [],
    }),
  })
  await flush()
  harness.port.inspectGoal = async () => ({ goal: goalProjection(), pending: null, latest_evaluation: null })
  let requested: Parameters<typeof harness.port.requestGoal>[0] | undefined
  harness.port.requestGoal = async params => {
    requested = params
    return { disposition: "ready", pending: pendingProjection("ready", "amend") }
  }

  expect((await harness.controller.dispatch({ type: "input.submit", value: "/goal amend 补充错误态验收" })).status).toBe("accepted")
  expect(requested).toMatchObject({
    kind: "amend",
    input_text: "补充错误态验收",
    expected_goal_id: "goal-1",
    expected_revision: 1,
  })

  harness.port.completeRun("thread-goal", harness.runHandles.at(-1)!.runId)
  await flush()
  const actions: string[] = []
  harness.port.mutateGoal = async params => {
    actions.push(params.action.kind)
    if (params.action.kind === "resume") {
      return {
        disposition: "applied",
        goal: { ...goalProjection(), revision: 3, status: "active" },
        pending: null,
        continuation: { continuation_id: "resume-1", goal_id: "goal-1", goal_revision: 3, reason: "resumed" },
      }
    }
    return {
      disposition: "applied",
      goal: params.action.kind === "clear" ? null : { ...goalProjection(), revision: 2, status: "paused" },
      pending: null,
      continuation: null,
    }
  }

  await harness.controller.dispatch({ type: "input.submit", value: "/goal pause" })
  expect(harness.controller.getSnapshot().goal?.status).toBe("paused")
  await harness.controller.dispatch({ type: "input.submit", value: "/goal resume" })
  await flush()
  expect(harness.runHandles.at(-1)?.runId).toBe("resume-1")
  await harness.controller.dispatch({ type: "input.submit", value: "/goal clear" })
  expect(harness.controller.getSnapshot().goal).toBeNull()
  expect(actions).toEqual(["pause", "resume", "clear"])
})

test("普通 Run 终态可消费服务端对账出的 pending proposal", async () => {
  const harness = makeHarness({ capabilities: [Capability.GOAL_READ, Capability.GOAL_MANAGE] })
  const started = await harness.controller.dispatch({ type: "input.submit", value: "先做当前任务" })
  expect(started.status).toBe("accepted")
  const current = harness.runHandles.at(-1)!
  harness.port.completeRunWithContext(current.threadId, current.runId, {
    goal_proposal_ready: { request_id: "request-after-run" },
  })

  await flush()

  expect([...harness.runStates.values()].at(-1)?.input.input).toEqual({
    kind: "goal_proposal",
    request_id: "request-after-run",
  })
})

test("只读能力可打开 Goal viewer，但不能创建目标", async () => {
  const harness = makeHarness({
    capabilities: [Capability.GOAL_READ],
    initialThreadId: "thread-goal",
  })
  harness.port.inspectGoal = async () => ({
    goal: goalProjection(),
    pending: null,
    latest_evaluation: null,
  })
  await flush()

  expect((await harness.controller.dispatch({ type: "input.submit", value: "/goal show" })).status).toBe("accepted")
  expect(harness.controller.getSnapshot().interaction).toMatchObject({
    type: "goal",
    readOnly: true,
    status: "active",
    revision: 1,
    criteria: ["登录成功后进入首页"],
    graderLabel: "继承主模型",
    maxIterations: 3,
  })
  expect(harness.runHandles).toHaveLength(0)
  expect((await harness.controller.dispatch({ type: "goal-view.close" })).status).toBe("accepted")
  expect(harness.controller.getSnapshot().interaction).toBeNull()

  const create = await harness.controller.dispatch({ type: "input.submit", value: "/goal 新目标" })
  expect(create).toMatchObject({ status: "rejected", code: "capability-missing" })
  expect(harness.calls).not.toContain("goal.request")
})

test("Compose 手输 /goal 返回稳定本地提示", async () => {
  const harness = makeHarness()
  await harness.controller.dispatch({ type: "work-mode.cycle" })
  await harness.controller.dispatch({ type: "input.submit", value: "/goal 做完登录" })
  expect(notices(harness.controller.getSnapshot())).toContain("`/goal` 仅在 Build 工作模式可用。")
  expect(harness.calls).not.toContain("goal.request")
})

test("Goal 保留前缀非法参数不会退化成 objective，普通生命周期前缀仍可作为目标", async () => {
  const harness = makeHarness({ capabilities: [Capability.GOAL_READ, Capability.GOAL_MANAGE] })
  for (const value of ["/goal amend", "/goal model unknown", "/goal max-iterations nope"]) {
    const result = await harness.controller.dispatch({ type: "input.submit", value })
    expect(result).toMatchObject({ status: "rejected", code: "invalid-argument" })
  }
  expect(harness.calls).not.toContain("goal.request")

  let inputText = ""
  harness.port.requestGoal = async params => {
    inputText = params.input_text
    return { disposition: "queued", pending: pendingProjection("ready") }
  }
  expect((await harness.controller.dispatch({ type: "input.submit", value: "/goal pause later" })).status).toBe("accepted")
  expect(inputText).toBe("pause later")
})

function goalRequest(threadId: string, runId: string, requestId: string): GoalInteractionRequest & { type: "goal" } {
  return {
    type: "goal",
    request_id: requestId,
    thread_id: threadId,
    run_id: runId,
    timeout_ms: 0,
    payload: {
      interrupt_id: "interrupt-1",
      request_id: requestId,
      proposal_kind: "create",
      base_goal_id: null,
      base_revision: null,
      objective: "完成登录功能",
      assumptions: ["沿用现有会话"],
      criteria: ["登录成功后进入首页", "错误密码显示提示"],
      decisions: ["accepted", "edited", "cancelled"],
    },
  }
}

function goalProjection() {
  return {
    goal_id: "goal-1",
    revision: 1,
    status: "active" as const,
    objective: "完成登录功能",
    assumptions: [],
    criteria: [{ criterion_id: "criterion-1", text: "登录成功后进入首页" }],
    note: null,
    prior_blocker: null,
    grader: { selection: "inherit" as const, configured_profile_id: null, actual_profile_id: null },
    max_iterations: 3,
    created_at_ms: 1,
    updated_at_ms: 2,
    completed_at_ms: null,
  }
}

function pendingProjection(status: "ready" | "reviewing", kind: "create" | "replace" | "amend" = "create") {
  return {
    request_id: "request-1",
    kind,
    status,
    base_goal_id: kind === "create" ? null : "goal-1",
    base_revision: kind === "create" ? null : 1,
    input_text: "完成登录功能",
    proposed_objective: status === "reviewing" ? "完成登录功能" : null,
    proposed_assumptions: status === "reviewing" ? ["沿用现有会话"] : [],
    proposed_criteria: status === "reviewing" ? ["登录成功后进入首页"] : [],
    created_at_ms: 1,
    updated_at_ms: 2,
    error_code: null,
  }
}
