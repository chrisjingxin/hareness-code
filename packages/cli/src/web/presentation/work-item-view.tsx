/** Web Work Item 展示：持久 Work Item 投影卡片；只读、不持业务状态。 */
/** @jsxImportSource react */

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
 * Work Item 展示条：仅在存在进行中 Work Item 时渲染卡片；
 * 无 Work Item 时不渲染任何内容——模式指示与空态提示已并入 Composer rail，
 * 避免用「什么都没有」的横幅抢占对话列顶部。
 */
export function WorkItemBanner({ view }: { view: WorkItemView }): ReactElement | null {
  if (!view.workItem) {
    return null
  }
  return (
    <section className="work-item-banner" aria-label="工作项状态">
      <WorkItemCard item={view.workItem} />
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
