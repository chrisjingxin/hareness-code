/** 子代理时间线无过程事件时的双端统一空态文案。 */

type ActiveRunIdentity = {
  readonly threadId: string
  readonly runId: string
}

/** 在子路由的可见时间线为空时返回与运行状态对应的短提示。 */
export function childTimelineEmptyMessage(
  executionId: string | null | undefined,
  hasVisibleItems: boolean,
  activeRun: ActiveRunIdentity | null | undefined,
): string | null {
  if (!executionId || hasVisibleItems) return null
  return activeRun ? "子代理刚开始，暂无过程" : "未收到该子代理的过程事件"
}
