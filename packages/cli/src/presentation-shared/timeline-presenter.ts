/** 跨端共享 Timeline 展示语义：activity/tool/interaction 状态的中文文案。 */

import type { InteractiveActivity } from "../interactive/types"
import type { InteractionCard } from "../interactive/state"

/** 将内部领域 Activity Kind 转换为展示用中文状态标签。 */
export function activityLabel(kind: InteractiveActivity["kind"]): string {
  switch (kind) {
    case "home":
      return "就绪"
    case "idle":
      return "就绪"
    case "compacting":
      return "正在压缩上下文"
    case "starting":
      return "正在思考"
    case "running":
      return "正在运行"
    case "waiting-interaction":
      return "等待交互"
    case "cancelling":
      return "正在取消"
    case "completed":
      return "已完成"
    case "cancelled":
      return "已取消"
    case "failed":
      return "运行失败"
    default:
      return "就绪"
  }
}

/** 将 Host 已观测的运行阶段转换为不虚构内部步骤的文案。 */
export function progressPhaseLabel(phase: "preparing" | "model"): string {
  return phase === "preparing" ? "准备运行" : "等待模型响应"
}

/** Tool 卡状态的中文标签。 */
export function toolStatusLabel(status: "running" | "completed" | "failed"): string {
  switch (status) {
    case "running":
      return "运行中"
    case "completed":
      return "已完成"
    case "failed":
      return "失败"
  }
}

/** Goal 独立验收结果的中文标签；未知值不回显英文枚举。 */
export function goalEvaluationResultLabel(result?: string): string {
  switch (result) {
    case "satisfied":
      return "通过"
    case "needs_revision":
      return "未通过"
    case "max_iterations_reached":
      return "已达次数上限"
    case "grader_error":
      return "执行失败"
    case "failed":
      return "失败"
    default:
      return ""
  }
}

/** 验收过程标题：验收中 / 验收通过 · 第 N 轮。 */
export function goalEvaluationTitle(
  phase: "checking" | "result",
  iteration: number,
  result?: string,
): string {
  if (phase === "checking") return `验收中 · 第 ${iteration} 轮`
  const label = goalEvaluationResultLabel(result)
  return label ? `验收${label} · 第 ${iteration} 轮` : `验收 · 第 ${iteration} 轮`
}

/** 将已落定的交互状态压缩为简短、可扫描的历史标签。 */
export function interactionStatusLabel(status: InteractionCard["status"]): string {
  switch (status) {
    case "approved":
      return "已允许"
    case "rejected":
      return "已拒绝"
    case "answered":
      return "已回答"
    case "cancelled":
      return "已超时"
    case "resolved":
      return "已解决"
    case "pending":
      return "等待中"
  }
}
