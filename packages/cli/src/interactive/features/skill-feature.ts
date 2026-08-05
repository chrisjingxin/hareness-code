/** Skill Feature：管理当前选中的 Skill（armedSkill）及启停（setSkillEnabled）。 */

import type { IntentOutcome, SkillSummary } from "../ports"
import { appendNotice } from "../state"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export class SkillFeature {
  armedSkill: SkillSummary | undefined

  armSkill(skillId: string, ctx: FeatureContext, options: { skills: readonly SkillSummary[] }): IntentOutcome {
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot arm skill while run is active" }
    }
    const skill = options.skills.find(item => item.id === skillId)
    if (!skill) {
      ctx.commit(current => appendNotice(current, "Skill 不存在。"))
      return { status: "rejected", code: "not-found", message: "Skill not found" }
    }
    this.armedSkill = skill
    ctx.publish()
    return { status: "accepted" }
  }

  clearArmedSkill(ctx: FeatureContext): void {
    this.armedSkill = undefined
    ctx.publish()
  }

  async setSkillEnabled(
    skillId: string,
    enabled: boolean,
    ctx: FeatureContext,
    options: {
      hasCapability: boolean
      hasSkill: boolean
      onSuccess: () => Promise<void>
    },
  ): Promise<IntentOutcome> {
    if (!options.hasCapability) {
      return { status: "rejected", code: "capability-missing", message: "Capability skills.manage missing" }
    }
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot change skill status while run is active" }
    }
    if (!options.hasSkill) {
      return { status: "rejected", code: "not-found", message: "Skill not found" }
    }

    try {
      await ctx.gateway.setSkillEnabled(skillId, enabled)
      if (this.armedSkill?.id === skillId && !enabled) {
        this.armedSkill = undefined
      }
      await options.onSuccess()
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `Skill 启停失败：${errorMessage(error)}` }
    }
  }
}
