/** 跨端共享命令菜单策略：Slash 查询过滤与可见项排序的唯一实现。 */

import type { CommandMenuItem } from "../interactive/commands"

/**
 * 按 draft 过滤命令菜单：只有 `/name` 形态的输入展示菜单；
 * 命令按名称/别名前缀匹配，Skill 按 id/name/描述包含匹配，命令恒排在 Skill 前。
 */
export function filterCommandMenuItems(
  items: readonly CommandMenuItem[],
  draft: string,
): readonly CommandMenuItem[] {
  const query = draft.trimStart()
  if (!query.startsWith("/") || query.startsWith("//") || query.slice(1).match(/\s/)) return []
  const needle = query.slice(1).toLowerCase()
  const commands = items
    .filter((item): item is Extract<CommandMenuItem, { kind: "command" }> => item.kind === "command")
    .filter(({ command }) => shouldShowInMenu(command, needle))
    .filter(({ command }) => [command.name, ...(command.aliases ?? [])]
      .some(candidate => candidate.startsWith(needle)))
  const skills = items
    .filter((item): item is Extract<CommandMenuItem, { kind: "skill" }> => item.kind === "skill")
    .filter(skill => {
      const label = `skill:${skill.skill.id}`.toLowerCase()
      const shortNeedle = needle.startsWith("skill:") ? needle.slice("skill:".length) : needle
      return [label, skill.skill.id.toLowerCase(), skill.skill.name.toLowerCase(), skill.skill.description.toLowerCase()]
        .some(candidate => candidate.includes(needle) || candidate.includes(shortNeedle))
    })
  return [...commands, ...skills]
}

/** 空 Slash 菜单不展示已废弃命令；用户继续输入其名称时仍可看到迁移说明。 */
function shouldShowInMenu(definition: { deprecated?: unknown }, needle: string): boolean {
  return Boolean(needle) || !definition.deprecated
}
