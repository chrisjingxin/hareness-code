/** 可派发 Agent 的只读浏览展示：用途文案与检索过滤。 */

import type { AgentSummary } from "@za38/protocol"

/** 内置角色给用户看的短用途；不写工具名、排除项或能力求交。 */
const BUILTIN_BROWSE_PURPOSE: Readonly<Record<string, string>> = {
  "general-purpose": "研究、搜索和把事情做完",
  explore: "只读搜索代码，找出文件和结构",
}

/** 来源标签：内置 / Plugin。 */
export function agentKindLabel(kind: AgentSummary["kind"]): string {
  return kind === "builtin" ? "内置" : "Plugin"
}

/** 浏览列表右侧的一句话用途。 */
export function agentBrowsePurpose(agent: AgentSummary): string {
  const builtin = BUILTIN_BROWSE_PURPOSE[agent.id]
  if (builtin) return builtin
  const description = agent.description?.trim()
  if (description) return description
  return agent.purpose
}

/** 按 id、来源和用途过滤浏览列表。 */
export function filterAgents(agents: readonly AgentSummary[], query: string): readonly AgentSummary[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return agents
  return agents.filter(agent => {
    const haystack = [
      agent.id,
      agent.kind,
      agentKindLabel(agent.kind),
      agentBrowsePurpose(agent),
      agent.description ?? "",
      agent.purpose,
    ].join(" ").toLowerCase()
    return haystack.includes(needle)
  })
}
