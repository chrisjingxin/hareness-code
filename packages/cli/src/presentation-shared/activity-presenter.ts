import type { InteractiveActivity } from "../interactive/types"

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
    case "restoring":
      return "正在恢复"
    default:
      return "就绪"
  }
}
