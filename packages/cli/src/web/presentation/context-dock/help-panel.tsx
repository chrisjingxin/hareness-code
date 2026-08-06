/** Help 面板：列出共享 Registry 中的命令与可调用 Skill（迁移自 panels.tsx，全部只读）。 */
/** @jsxImportSource react */

import { Info, Settings } from "lucide-react"

import type { CommandMenuItem } from "../../../interactive/commands"

import type { WebAdapterSnapshot } from "../../application/adapter"

/** Help 面板：只读展示命令与 Skill 清单。 */
export function HelpPanel({
  snapshot,
}: {
  snapshot: WebAdapterSnapshot
}): React.ReactElement {
  const items: readonly CommandMenuItem[] = snapshot.interactive.commands
  return (
    <div className="panel help-view">
      <ul className="panel-list help-list" role="list">
        {items.map((item, index) => {
          if (item.kind === "skill") {
            return (
              <li key={`skill-${item.skill.id}-${index}`} className="panel-item help-item">
                <span className="panel-item-title">
                  <Settings aria-hidden="true" />
                  {`/skill:${item.skill.id}`}
                </span>
                <span className="panel-item-sub">{item.skill.description}</span>
              </li>
            )
          }
          const disabled = item.availability.state === "disabled"
          const reason = item.availability.state === "disabled"
            ? item.availability.reason
            : item.availability.state === "hidden"
              ? item.availability.reason
              : null
          return (
            <li key={`cmd-${item.command.id}-${index}`} className="panel-item help-item">
              <span className="panel-item-title">
                <Info aria-hidden="true" />
                {`/${item.command.name}`}
              </span>
              <span className="panel-item-sub">{item.command.description}</span>
              {reason ? <span className="panel-item-note">{reason}</span> : null}
              {!disabled && !reason ? null : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
