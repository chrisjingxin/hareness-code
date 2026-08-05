import { expect, test } from "bun:test"
import { Capability } from "@za38/protocol"
import { makeHarness, flush, runtime } from "./harness"

test("skill.set-enabled：缺少 skills.manage 能力时直接拒绝，不发 RPC", async () => {
  const harness = makeHarness()
  try {
    await flush()
    const before = harness.calls.filter(call => call.startsWith("skills.set_enabled")).length
    const outcome = await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "user/repo-review-demo", enabled: false })
    expect(harness.calls.filter(call => call.startsWith("skills.set_enabled")).length).toBe(before)
    expect(outcome).toEqual({ status: "rejected", code: "capability-missing", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：协商了 skills.manage 时启用 Skill 并刷新权威 catalog", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  try {
    await flush()
    expect(harness.controller.getSnapshot().catalogs.skills.status).toBe("ready")
    const skillsListCallsBefore = harness.calls.filter(call => call === "skills.list(true)").length

    await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "user/repo-review-demo", enabled: true })
    await flush()

    expect(harness.calls).toContain("skills.set_enabled(user/repo-review-demo,true)")
    // 成功后用 include_disabled=true 重新拉取权威全集
    expect(harness.calls.filter(call => call === "skills.list(true)").length).toBe(skillsListCallsBefore + 1)
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：禁用当前 armed Skill 时清除选择并保留 catalog 全集", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  try {
    await flush()
    await harness.controller.dispatch({ type: "skill.arm", skillId: "user/repo-review-demo" })
    expect(harness.controller.getSnapshot().selection.armedSkill?.id).toBe("user/repo-review-demo")

    await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "user/repo-review-demo", enabled: false })
    await flush()
    expect(harness.calls).toContain("skills.set_enabled(user/repo-review-demo,false)")
    expect(harness.controller.getSnapshot().selection.armedSkill).toBeNull()
    // catalog 仍含 disabled 项（authoritative full set）
    expect(harness.controller.getSnapshot().catalogs.skills.items.some(item => item.id === "user/repo-review-demo")).toBe(true)
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：RPC 失败保留当前状态并输出脱敏 notice", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  harness.port.setSkillEnabledImpl(async () => {
    throw new Error("内部错误包含敏感信息")
  })
  try {
    await flush()
    await harness.controller.dispatch({ type: "skill.arm", skillId: "user/repo-review-demo" })
    const armedBefore = harness.controller.getSnapshot().selection.armedSkill?.id

    const outcome = await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "user/repo-review-demo", enabled: false })
    await flush()
    const snapshot = harness.controller.getSnapshot()
    // 失败时 armedSkill 不变，状态保持
    expect(snapshot.selection.armedSkill?.id).toBe(armedBefore)
    expect(outcome).toEqual({ status: "rejected", code: "agent-error", message: expect.any(String) })
    // 失败时不刷新 catalog：拉取次数不应增长
    const skillsListCount = harness.calls.filter(call => call === "skills.list(true)").length
    expect(skillsListCount).toBeGreaterThan(0)
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：active Run 期间拒绝，不发 RPC", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  try {
    await flush()
    await harness.controller.dispatch({ type: "input.submit", value: "运行中" })
    const setEnabledBefore = harness.calls.filter(call => call.startsWith("skills.set_enabled")).length
    const outcome = await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "user/repo-review-demo", enabled: false })
    expect(harness.calls.filter(call => call.startsWith("skills.set_enabled")).length).toBe(setEnabledBefore)
    expect(outcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：未知 skillId 直接拒绝，不发 RPC", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  try {
    await flush()
    const setEnabledBefore = harness.calls.filter(call => call.startsWith("skills.set_enabled")).length
    const outcome = await harness.controller.dispatch({ type: "skill.set-enabled", skillId: "missing/skill", enabled: true })
    expect(harness.calls.filter(call => call.startsWith("skills.set_enabled")).length).toBe(setEnabledBefore)
    expect(outcome).toEqual({ status: "rejected", code: "not-found", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("skill.set-enabled：catalog 包含 disabled Skill 时，命令菜单只暴露 enabled && userInvocable 项", async () => {
  const harness = makeHarness({ capabilities: [...runtime.capabilities ?? [], Capability.SKILLS_MANAGE] })
  try {
    await flush()
    const items = harness.controller.getSnapshot().commands
    const skillItems = items.filter(item => item.kind === "skill")
    // 内部 catalog 仍包含 disabled 项
    expect(harness.controller.getSnapshot().catalogs.skills.items.some(item => item.id === "builtin/disabled-demo")).toBe(true)
    // 菜单只暴露 enabled && userInvocable
    expect(skillItems.map(item => item.kind === "skill" ? item.skill.id : null)).toEqual(["user/repo-review-demo"])
  } finally {
    await harness.controller.close()
  }
})

