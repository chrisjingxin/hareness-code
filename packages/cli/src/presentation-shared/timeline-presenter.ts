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
