/** Compose Work Item 展示组件：只消费共享 WorkItemView 投影，不复制 reducer 或命令可见性规则。 */

import { TextAttributes } from "@opentui/core"

import type { WorkItemView } from "../../interactive/selectors"
import type { WorkItemProjection, WorkItemStatus, WorkMode } from "../../interactive/state"
import { tuiTheme } from "./theme"

/** Work Item 状态中文文案；TUI 与 Web 两端保持一致。 */
const WORK_ITEM_STATUS_LABELS: Record<WorkItemStatus, string> = {
  active: "进行中",
  waiting_user: "等待你的决定",
  blocked: "需要你处理",
  completed: "已完成",
  abandoned: "已放弃",
}

/** 状态语义色：进行中品牌蓝、等待决定警示黄、阻塞危险红、终态成功绿/弱化灰。 */
const WORK_ITEM_STATUS_COLORS: Record<WorkItemStatus, string> = {
  active: tuiTheme.primary,
  waiting_user: tuiTheme.warning,
  blocked: tuiTheme.danger,
  completed: tuiTheme.success,
  abandoned: tuiTheme.muted,
}

/** 窄终端截断：超长文案折为省略号，避免撑破行宽。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

/** 模式指示器：threadMode 锁定时不给「可切换」提示；未锁定时提示 Tab 切换。 */
function ModeIndicator(props: { threadMode: WorkMode | null; modeLocked: boolean }) {
  if (props.modeLocked && props.threadMode !== null) {
    return (
      <box flexDirection="row" gap={1}>
        <text fg={tuiTheme.primary} attributes={TextAttributes.BOLD}>{props.threadMode === "compose" ? "Compose" : "Build"}</text>
        <text fg={tuiTheme.muted}>已锁定</text>
      </box>
    )
  }
  return (
    <box flexDirection="row" gap={1}>
      <text fg={tuiTheme.subtle}>Tab 切换模式</text>
    </box>
  )
}

/** Work Item 详情：title/slug/revision/status 及 pending/blocked 补充。 */
function WorkItemDetails(props: { workItem: WorkItemProjection }) {
  const item = props.workItem
  return (
    <box flexDirection="column" paddingBottom={1}>
      <box flexDirection="row" gap={1}>
        <text fg={WORK_ITEM_STATUS_COLORS[item.status]} attributes={TextAttributes.BOLD}>
          {WORK_ITEM_STATUS_LABELS[item.status]}
        </text>
        <text fg={tuiTheme.text}>{shorten(item.title, 60)}</text>
        <text fg={tuiTheme.muted}>· {shorten(item.slug, 40)} · rev {item.revision}</text>
      </box>
      {item.currentActivity ? (
        <text fg={tuiTheme.muted}>活动：{shorten(item.currentActivity, 80)}</text>
      ) : null}
      {item.pendingDecision ? (
        <text fg={tuiTheme.warning}>待处理：{shorten(item.pendingDecision, 80)}</text>
      ) : null}
      {item.blockedReason ? (
        <text fg={tuiTheme.danger}>阻塞：{shorten(item.blockedReason, 80)}</text>
      ) : null}
    </box>
  )
}

/** Work Item 主视图：模式指示器 + 持久投影；无投影时显示占位空态。 */
export function WorkItemView({ view }: { view: WorkItemView }) {
  return (
    <box flexDirection="column" flexShrink={0} paddingLeft={2} paddingRight={2} paddingTop={1}>
      <ModeIndicator threadMode={view.threadMode} modeLocked={view.modeLocked} />
      {view.workItem ? (
        <WorkItemDetails workItem={view.workItem} />
      ) : (
        <text fg={tuiTheme.subtle}>暂无 Work Item</text>
      )}
    </box>
  )
}
