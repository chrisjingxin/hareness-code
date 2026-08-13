/** Web Work Item 展示：持久 Work Item 投影 + 模式锁定指示器；只读、不持业务状态。 */
/** @jsxImportSource react */

import { Lock } from "lucide-react"
import type { ReactElement } from "react"
import type { WorkItemView } from "../../interactive/selectors"
import type { WorkItemProjection, WorkItemStatus } from "../../interactive/state"

/** Work Item 状态中文文案；两端一致。 */
const WORK_ITEM_STATUS_LABELS: Record<WorkItemStatus, string> = {
  active: "进行中",
  waiting_user: "等待你的决定",
  blocked: "需要你处理",
  completed: "已完成",
  abandoned: "已放弃",
}

/**
 * Work Item 展示条：顶部为模式指示器（Thread 冻结时锁定、否则提示 Tab 切换），
 * 下方为当前 Work Item 卡片或无 Work Item 时的空态。纯展示，不派生任何业务状态。
 */
export function WorkItemBanner({ view }: { view: WorkItemView }): ReactElement {
  return (
    <section className="work-item-banner" aria-label="工作项状态">
      {view.modeLocked ? (
        <span
          className="work-item-mode work-item-mode-locked"
          role="status"
          title="Thread 工作模式已锁定，无法切换"
        >
          <Lock aria-hidden="true" className="work-item-mode-icon" />
          <span>{view.threadMode === "compose" ? "Compose" : "Build"}</span>
        </span>
      ) : (
        <span
          className="work-item-mode"
          role="status"
          title="空闲时按 Tab 切换工作模式"
        >
          Tab 切换工作模式
        </span>
      )}
      {view.workItem ? (
        <WorkItemCard item={view.workItem} />
      ) : (
        <div className="work-item-empty" role="status">当前无进行中的工作项</div>
      )}
    </section>
  )
}

/** 单个 Work Item 的只读卡片：标题、slug/revision/状态、当前活动与待处理/阻塞原因。 */
function WorkItemCard({ item }: { item: WorkItemProjection }): ReactElement {
  return (
    <div className="work-item-card">
      <div className="work-item-title">{item.title}</div>
      <div className="work-item-meta">
        <span className="work-item-status" data-status={item.status}>{WORK_ITEM_STATUS_LABELS[item.status]}</span>
        <span className="work-item-slug">{item.slug}</span>
        <span className="work-item-revision">rev {item.revision}</span>
      </div>
      {item.currentActivity ? <div className="work-item-activity">{item.currentActivity}</div> : null}
      {item.pendingDecision ? <div className="work-item-pending">待处理：{item.pendingDecision}</div> : null}
      {item.blockedReason ? <div className="work-item-blocked">阻塞：{item.blockedReason}</div> : null}
    </div>
  )
}
