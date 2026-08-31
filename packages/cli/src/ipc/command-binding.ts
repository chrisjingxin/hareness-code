/** Plugin Command 的协议版本门禁：只在双方协商支持时登记 exact Registry binding。 */

import {
  OPERATION_MIN_MINOR,
  type CommandBinding,
  type CommandBindingsParams,
  type CommandBindingsResult,
} from "@za38/protocol"

export const COMMANDS_BIND_MIN_MINOR = OPERATION_MIN_MINOR["commands.bind"]
export const COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED = "COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED"

type CommandBindingClient = {
  bindCommandRegistry(params: CommandBindingsParams): Promise<CommandBindingsResult>
}

/**
 * 按 initialize 返回的协商结果执行一次 binding。
 *
 * 旧 Agent 没有 Plugin Command 时不需要绑定，保留普通 run 的兼容路径；
 * 一旦存在 Plugin Command，必须稳定报升级错误，不能尝试未知 RPC。
 */
export async function bindPluginCommands(
  client: CommandBindingClient,
  negotiatedMinor: number,
  snapshotId: string,
  bindings: readonly CommandBinding[],
): Promise<void> {
  if (bindings.length === 0) return
  if (negotiatedMinor < COMMANDS_BIND_MIN_MINOR) {
    throw new Error(COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED)
  }
  await client.bindCommandRegistry({
    snapshot_id: snapshotId,
    bindings: [...bindings],
  })
}
