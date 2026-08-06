/** Skills 面板：选择 armed Skill；具备 skills.manage 时显示启停控件（迁移自 panels.tsx）。 */
/** @jsxImportSource react */

import { Wrench } from "lucide-react"

import type { SkillSummary } from "../../../interactive/types"
import { selectNavigationView } from "../../../interactive/selectors"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { PanelEmpty, PanelError, PanelToolbar } from "./panel-common"

/** Skills 面板：arm Skill + 启停管理；capability 门禁由共享 Core 提供。 */
export function SkillsPanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.skills
  const query = snapshot.panelSearch.skills.query
  const items = filterSkills(catalog.items, query)
  const armedId = snapshot.interactive.selection.armedSkill?.id ?? null
  const manageAllowed = selectNavigationView(snapshot.interactive).availability.hasSkillManage
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  return (
    <div className="panel panel-skills">
      <PanelToolbar
        query={query}
        placeholder="搜索 Skill…"
        onSearch={value => dispatch({ type: "panel-search", panel: "skills", query: value })}
        onRefresh={() => dispatch({ type: "dock-panel-select", panel: "skills" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "dock-panel-select", panel: "skills" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Skill…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Skill" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(skill => {
            const isArmed = skill.id === armedId
            const canInvoke = skill.enabled && skill.userInvocable
            const reason = !skill.enabled
              ? "Skill 已停用"
              : !skill.userInvocable
                ? "该 Skill 不能手动调用"
                : null
            return (
              <li key={skill.id} className="panel-item-row">
                <button
                  type="button"
                  className="panel-item"
                  data-active={isArmed ? "true" : "false"}
                  data-disabled={!canInvoke ? "true" : "false"}
                  disabled={disabled || !canInvoke}
                  aria-pressed={isArmed}
                  title={reason ?? skill.description}
                  onClick={() => dispatch({ type: "skill-arm", skillId: skill.id })}
                >
                  <span className="panel-item-title">
                    <Wrench aria-hidden="true" />
                    {skill.name}
                  </span>
                  <span className="panel-item-sub">
                    {skill.source} · {skill.description}
                  </span>
                  {skill.argumentHint ? (
                    <span className="panel-item-note">参数：{skill.argumentHint}</span>
                  ) : null}
                  {reason ? <span className="panel-item-note">{reason}</span> : null}
                </button>
                {manageAllowed ? (
                  <label className="skill-toggle" title={skill.enabled ? "停用 Skill" : "启用 Skill"}>
                    <input
                      type="checkbox"
                      checked={skill.enabled}
                      disabled={disabled || busy}
                      onChange={event => dispatch({
                        type: "skill-set-enabled",
                        skillId: skill.id,
                        enabled: event.currentTarget.checked,
                      })}
                    />
                    <span>{skill.enabled ? "已启用" : "已停用"}</span>
                  </label>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function filterSkills(
  items: readonly SkillSummary[],
  query: string,
): readonly SkillSummary[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(skill => {
    const haystack = `${skill.id} ${skill.name} ${skill.description} ${skill.source}`.toLowerCase()
    return haystack.includes(needle)
  })
}
