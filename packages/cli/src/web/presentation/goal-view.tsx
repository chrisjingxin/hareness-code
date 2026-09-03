/** 当前 Goal 的紧凑 Web 投影。 */
/** @jsxImportSource react */

import type { InteractiveSnapshot } from "../../interactive/types"

export function GoalBanner(props: { interactive: InteractiveSnapshot }): React.ReactNode {
  const { goal, goalPending, goalActivities } = props.interactive
  if (!goal && !goalPending) return null
  const recentActivities = goalActivities.slice(-3)
  return (
    <section className="goal-banner" aria-label="当前目标">
      <strong>目标</strong>
      <span>{goal ? `${goal.status} · ${goal.objective}` : `准备中 · ${goalPending?.input_text ?? ""}`}</span>
      {goal ? <small>r{goal.revision} · {goal.criteria.length} 条验收标准</small> : null}
      {goal?.note ? <small>备注：{goal.note}</small> : null}
      {goal?.prior_blocker ? <small>上一阻塞：{goal.prior_blocker}</small> : null}
      {goalPending ? <small>待处理：{goalPending.status} · {goalPending.input_text}</small> : null}
      {recentActivities.length ? (
        <ul aria-label="目标最近活动">
          {recentActivities.map(activity => <li key={activity.activity_id}>{activity.summary}</li>)}
        </ul>
      ) : null}
    </section>
  )
}
