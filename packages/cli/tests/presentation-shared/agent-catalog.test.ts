import { expect, test } from "bun:test"
import type { AgentSummary } from "@za38/protocol"
import { agentBrowsePurpose, agentKindLabel, filterAgents } from "../../src/presentation-shared/agent-catalog"

function agent(overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    id: "explore",
    description: "只读探索子代理",
    purpose: "explore",
    model_profile_id: "inherit",
    execution_policy_id: "inherit",
    requested_skills: [],
    requested_mcp_servers: [],
    max_turns: null,
    source: "builtin",
    fingerprint: "explore-fingerprint",
    kind: "builtin",
    tools: ["ls", "read_file"],
    ...overrides,
  }
}

test("浏览文案只说用途，不暴露工具名单或能力求交", () => {
  expect(agentBrowsePurpose(agent({ id: "general-purpose", tools: [] }))).toBe("研究、搜索和把事情做完")
  expect(agentBrowsePurpose(agent({ id: "explore" }))).toBe("只读搜索代码，找出文件和结构")
  expect(agentBrowsePurpose(agent({ id: "reviewer", kind: "plugin", description: "检查 diff 质量" }))).toBe("检查 diff 质量")
  expect(agentKindLabel("plugin")).toBe("Plugin")
  expect(agentBrowsePurpose(agent({ id: "general-purpose", tools: [] }))).not.toContain("委派")
  expect(agentBrowsePurpose(agent({ id: "explore" }))).not.toMatch(/glob|grep|read_file/)
})

test("filterAgents 按 id、来源和用途匹配", () => {
  const items = [
    agent({ id: "general-purpose", purpose: "general-purpose", tools: [], description: "通用子代理" }),
    agent({ id: "explore" }),
  ]
  expect(filterAgents(items, "explore").map(item => item.id)).toEqual(["explore"])
  expect(filterAgents(items, "内置")).toHaveLength(2)
  expect(filterAgents(items, "只读搜索").map(item => item.id)).toEqual(["explore"])
})
