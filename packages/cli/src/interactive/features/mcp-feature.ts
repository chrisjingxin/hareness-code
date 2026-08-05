/** MCP Feature：管理 MCP 服务器添加、删除与异常映射。 */

import type { IntentOutcome, InteractiveMcpInput } from "../ports"
import type { FeatureContext } from "./types"

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
