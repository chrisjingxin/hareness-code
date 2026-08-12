/** Command Feature：管理命令注册表查询、/ 解析、availability 能力计算及 semantic operation 执行。 */

import type { AgentCommand } from "@za38/protocol"
import { dispatchSlashCommand, type CommandDispatchContext, type CommandResult } from "../command-dispatcher"
import {
  builtinCommandCapabilities,
  commandRegistry,
  createCommandRegistry,
  findCommandMenuItems,
  resolveSlashCommand,
  unknownCommandNotice,
  type CommandContext,
  type CommandMenuItem,
  type CommandRegistry,
  type SkillMenuItem,
} from "../commands"
import type { IntentOutcome } from "../ports"
import { appendNotice } from "../state"
import { runtimeStatusSummary } from "../runtime"
import type { FeatureContext } from "./types"

export class CommandFeature {
  /** Host 已校验的 Plugin Command 与内置命令合一的不可变 Registry。 */
  private readonly registry: CommandRegistry

  constructor(agentCommands: readonly AgentCommand[] = []) {
    this.registry = createCommandRegistry(agentCommands)
  }

  commandDispatchContext(ctx: FeatureContext, hasPendingInteraction: boolean): CommandDispatchContext {
    return {
      commandContext: {
        capabilities: new Set(ctx.baseRuntime.capabilities ?? builtinCommandCapabilities),
        hasThread: Boolean(ctx.getState().currentThreadId),
        activeRun: Boolean(ctx.getState().activeRun),
        pendingOperation: Boolean(ctx.getState().pendingOperation),
        hasPendingInteraction,
      },
      threadId: ctx.getState().currentThreadId,
      runtimeStatus: runtimeStatusSummary(ctx.baseRuntime),
      versionSummary: `za38-cli ${ctx.baseRuntime.cliVersion ?? "0.1.0"} · JSON-RPC v3`,
      idGenerator: ctx.idGenerator,
    }
  }

  buildCommandItems(
    skillItems: readonly SkillMenuItem[],
    ctx: FeatureContext,
    hasPendingInteraction: boolean,
  ): readonly CommandMenuItem[] {
    return findCommandMenuItems(
      "/",
      skillItems,
      this.commandDispatchContext(ctx, hasPendingInteraction).commandContext,
      this.registry,
    )
  }

  /** 按稳定 command ID 交给 Dispatcher；未知 ID 只输出本地提示。 */
  async executeSlashCommand(
    command: { id: string; name: string; argument?: string },
    ctx: FeatureContext,
    options: {
      hasPendingInteraction: boolean
      applyResult: (result: CommandResult) => Promise<IntentOutcome>
    },
  ): Promise<IntentOutcome> {
    const definition = this.registry.get(command.id)
    if (!definition) {
      ctx.commit(current => appendNotice(current, `未知命令：/${command.name}。输入 /help 查看可用命令。`))
      return { status: "accepted" }
    }
    const result = dispatchSlashCommand(
      { id: command.id, name: definition.name, argument: command.argument },
      this.commandDispatchContext(ctx, options.hasPendingInteraction),
      this.registry,
    )
    return options.applyResult(result)
  }

  /** 解析输入为 command/unknown/escaped；unknown 由调用方转本地提示。 */
  resolveInputSlashCommand(rawValue: string): ReturnType<typeof resolveSlashCommand> {
    return resolveSlashCommand(rawValue, this.registry)
  }

  unknownNotice(resolution: Extract<ReturnType<typeof resolveSlashCommand>, { kind: "unknown" }>): string {
    return unknownCommandNotice(resolution)
  }
}
