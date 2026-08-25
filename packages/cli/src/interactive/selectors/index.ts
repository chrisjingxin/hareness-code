/** Interactive Selector：把领域 snapshot 收敛为可序列化展示视图与 FeatureAvailability。 */

export * from "./types"

import type { InteractiveSnapshot } from "../types"
import { CAPABILITY_GATE, type CommandView, type ConversationView, type FeatureAvailability, type InteractionView, type NavigationView, type RuntimeView, type WorkItemView } from "./types"

/** 由 snapshot 推导全部展示可用性；与 commands.ts 的 availability 计算共享同一输入。 */
export function selectFeatureAvailability(snapshot: InteractiveSnapshot): FeatureAvailability {
  const capabilities = new Set<string>(snapshot.runtime.capabilities ?? [])
  const hasRun = snapshot.activeRun !== null
  const hasInteraction = snapshot.interaction !== null
  const hasPendingOperation = snapshot.activity.kind === "compacting"
  const cancelling = snapshot.activity.kind === "cancelling"
  const isChildTimeline = Boolean(snapshot.childTimelineExecutionId)

  return {
    canSubmit: snapshot.connection.status === "open" && !hasPendingOperation && !isChildTimeline,
    canCancelRun: hasRun && !cancelling,
    canOpenThread: !hasRun && !hasInteraction && !hasPendingOperation,
    canToggleSkill: capabilities.has(CAPABILITY_GATE.toggleSkill) && !hasRun && !hasPendingOperation,
    canManageMcp: capabilities.has(CAPABILITY_GATE.manageMcp) && !hasRun && !hasPendingOperation,
    canChangeModel: capabilities.has(CAPABILITY_GATE.changeModel) && !hasPendingOperation,
    canOpenModelsPanel: capabilities.has(CAPABILITY_GATE.openModelsPanel),
    canOpenSkillsPanel: capabilities.has(CAPABILITY_GATE.openSkillsPanel),
    canOpenMcpPanel: capabilities.has(CAPABILITY_GATE.openMcpPanel),
    canOpenAgentsPanel: capabilities.has(CAPABILITY_GATE.openAgentsPanel),
    hasSkillManage: capabilities.has(CAPABILITY_GATE.toggleSkill),
    hasMcpManage: capabilities.has(CAPABILITY_GATE.manageMcp),
  }
}

/** 对话视图：时间线与当前运行状态。 */
export function selectConversationView(snapshot: InteractiveSnapshot): ConversationView {
  return {
    currentThreadId: snapshot.currentThreadId,
    activity: snapshot.activity,
    activeRun: snapshot.activeRun,
    timeline: snapshot.timeline,
    runProgress: snapshot.runProgress,
    lastRun: snapshot.lastRun,
    childTimelineExecutionId: snapshot.childTimelineExecutionId,
  }
}

/** 交互视图：挂起 Interaction 与破坏性确认。 */
export function selectInteractionView(snapshot: InteractiveSnapshot): InteractionView {
  return {
    interaction: snapshot.interaction,
    confirmation: snapshot.confirmation,
  }
}

/** 导航视图：四类 catalog 与打开/变更可用性。 */
export function selectNavigationView(snapshot: InteractiveSnapshot): NavigationView {
  const availability = selectFeatureAvailability(snapshot)
  return {
    catalogs: snapshot.catalogs,
    availability: {
      canOpenThread: availability.canOpenThread,
      canOpenModelsPanel: availability.canOpenModelsPanel,
      canOpenSkillsPanel: availability.canOpenSkillsPanel,
      canOpenMcpPanel: availability.canOpenMcpPanel,
      canOpenAgentsPanel: availability.canOpenAgentsPanel,
      hasSkillManage: availability.hasSkillManage,
      hasMcpManage: availability.hasMcpManage,
    },
  }
}

/** 命令视图：可用命令与提交可用性。 */
export function selectCommandView(snapshot: InteractiveSnapshot): CommandView {
  return {
    commands: snapshot.commands,
    availability: {
      canSubmit: selectFeatureAvailability(snapshot).canSubmit,
    },
  }
}

/** 运行时视图：runtime/connection/selection 与运行控制可用性。 */
export function selectRuntimeView(snapshot: InteractiveSnapshot): RuntimeView {
  const availability = selectFeatureAvailability(snapshot)
  return {
    runtime: snapshot.runtime,
    connection: snapshot.connection,
    selection: snapshot.selection,
    workMode: snapshot.workMode,
    composeState: snapshot.composeState,
    availability: {
      canCancelRun: availability.canCancelRun,
      canToggleSkill: availability.canToggleSkill,
      canManageMcp: availability.canManageMcp,
      canChangeModel: availability.canChangeModel,
    },
  }
}

/** Work Item 视图：持久投影与模式锁定；renderer 只消费此形状。 */
export function selectWorkItemView(snapshot: InteractiveSnapshot): WorkItemView {
  return {
    workItem: snapshot.workItem,
    threadMode: snapshot.threadMode,
    modeLocked: snapshot.threadMode != null,
  }
}
