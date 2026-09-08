/** MCP Feature：管理 MCP 服务器添加、删除与异常映射。 */

import type { McpServerStatus } from "@za38/protocol"

import type { IntentOutcome, InteractiveMcpInput, LoadableCatalog } from "../ports"
import type { FeatureContext } from "./types"

/** 把 MCP catalog 收成 `/mcp` 可展示的本地通知，不带 URL 或凭据。 */
export function formatMcpStatusNotice(catalog: LoadableCatalog<McpServerStatus>): string {
  if (catalog.status === "error") return `MCP 状态查询失败：${catalog.message}`
  const servers = catalog.items
  if (servers.length === 0) {
    return [
      "未配置 MCP 服务器。",
      "添加：/mcp add <name> <command> [args...]",
      "      /mcp add <name> --url <url> [--sse]",
      "删除：/mcp remove <name>",
    ].join("\n")
  }
  const connected = servers.filter(server => server.status === "connected").length
  const tools = servers.reduce((count, server) => count + server.tool_names.length, 0)
  const lines = [
    `MCP 服务器 ${servers.length} 个 · ${connected} 已连接 · 共 ${tools} 个工具`,
    ...servers.map(formatMcpServerLine),
  ]
  return lines.join("\n")
}

function formatMcpServerLine(server: McpServerStatus): string {
  const status = server.status === "connected" ? "已连接"
    : server.status === "failed" ? "连接失败"
      : "已跳过"
  const tools = server.tool_names.length ? server.tool_names.join(", ") : "无工具"
  const error = server.error ? ` · 错误: ${server.error}` : ""
  const source = server.source ? ` · ${server.source}` : ""
  return `- ${server.name} · ${server.transport} · ${status} · 工具: ${tools}${source}${error}`
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export class McpFeature {
  async addMcpServer(
    input: InteractiveMcpInput,
    ctx: FeatureContext,
    options: {
      hasCapability: boolean
      onSuccess: () => Promise<void>
    },
  ): Promise<IntentOutcome> {
    if (!options.hasCapability) {
      return { status: "rejected", code: "capability-missing", message: "Capability mcp.manage missing" }
    }
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot add MCP server while run is active" }
    }

    try {
      if (input.transport === "stdio") {
        await ctx.gateway.mcpAdd({
          name: input.name,
          transport: "stdio",
          command: input.command ?? "",
          args: input.args,
        })
      } else {
        await ctx.gateway.mcpAdd({
          name: input.name,
          transport: input.transport,
          url: input.url ?? "",
        })
      }
      await options.onSuccess()
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `添加 MCP 服务器失败：${errorMessage(error)}` }
    }
  }

  async removeMcpServer(
    name: string,
    ctx: FeatureContext,
    options: {
      hasCapability: boolean
      onSuccess: () => Promise<void>
    },
  ): Promise<IntentOutcome> {
    if (!options.hasCapability) {
      return { status: "rejected", code: "capability-missing", message: "Capability mcp.manage missing" }
    }
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot remove MCP server while run is active" }
    }

    try {
      await ctx.gateway.mcpRemove(name)
      await options.onSuccess()
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `删除 MCP 服务器失败：${errorMessage(error)}` }
    }
  }
}
