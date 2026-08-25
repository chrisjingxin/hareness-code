/** Interactive Core 的脱敏运行环境：握手摘要、终端降级与 /status 语义。 */

import type { AgentCommand, InitializeResult } from "@za38/protocol"

export const CLI_VERSION = "0.1.0"

/** 规范审批模式；与协议 approvalMode 枚举保持一致。 */
export type InteractiveApprovalMode = "plan" | "default" | "auto-edit" | "auto" | "yolo"

/** Shift+Tab 循环切换顺序，与 Python approval_mode.MODE_CYCLE 对齐。 */
export const APPROVAL_MODE_CYCLE: readonly InteractiveApprovalMode[] = ["plan", "default", "auto-edit", "auto", "yolo"]

/** Git 工作区状态：探测结果只有这四种稳定形态，未知/失败统一为 unavailable。 */
export type GitWorkspaceState =
  | { kind: "branch"; branch: string; root: string }
  | { kind: "detached"; shortSha: string; root: string }
  | { kind: "not-repository" }
  | { kind: "unavailable"; message: string }

/** Git porcelain 变更的稳定展示形状；不把 XY 细节泄漏到表现层。 */
export type GitChangedFile = {
  readonly path: string
  readonly status: "modified" | "added" | "deleted" | "renamed" | "copied" | "untracked" | "conflicted"
  /** 相对 HEAD 的新增/删除行数；null 表示二进制文件或统计不可用。 */
  readonly addedLines: number | null
  readonly removedLines: number | null
}

export type InteractiveRuntime = {
  workspace: string
  gitWorkspace?: GitWorkspaceState
  cliVersion: string
  modelName?: string
  /** 当前实际使用或待绑定的脱敏模型 Profile ID。 */
  modelProfileId?: string
  modelConfigured: boolean
  startupError?: string
  executionMode: "local" | "remote-sandbox"
  sandboxProvider?: string
  approvalMode: InteractiveApprovalMode
  approvalModeWarning?: string
  /** initialize 协商后的能力；缺省仅用于兼容未更新的测试运行时。 */
  capabilities?: readonly string[]
  /** Host 启动快照中的 Plugin Command；只含展示信息和 requested Skill ID。 */
  agentCommands?: readonly AgentCommand[]
  mcpSummary?: string
  /** 是否在侧栏展示 Prefix Cache 命中率（由 config.toml [ui] 驱动）。 */
  showCacheHitRate?: boolean
}

/** 将握手结果收敛为界面可安全显示的运行摘要，避免把配置原样暴露给组件。 */
export function createInteractiveRuntime(
  result: InitializeResult,
  cwd: string,
  options: { gitWorkspace?: GitWorkspaceState; cliVersion?: string } = {},
): InteractiveRuntime {
  const config = isRecord(result.config_summary) ? result.config_summary : undefined
  const model = config && isRecord(config.model) ? config.model : undefined
  const security = config && isRecord(config.security) ? config.security : undefined
  const mcpServers = config && Array.isArray(config.mcp_servers) ? config.mcp_servers : undefined
  const ui = config && isRecord(config.ui) ? config.ui : undefined
  return {
    workspace: stringValue(config?.workspace, cwd),
    gitWorkspace: options.gitWorkspace,
    cliVersion: options.cliVersion ?? CLI_VERSION,
    modelName: optionalString(model?.name),
    modelConfigured: model?.api_key_configured === true,
    startupError: isRecord(result.startup_error) ? optionalString(result.startup_error.message) : undefined,
    executionMode: security?.mode === "remote-sandbox" ? "remote-sandbox" : "local",
    sandboxProvider: optionalString(security?.provider),
    approvalMode: approvalMode(security?.approval_mode),
    approvalModeWarning: optionalString(security?.approval_mode_warning),
    capabilities: [...new Set(result.capabilities.enabled)],
    agentCommands: [...result.agent_commands],
    mcpSummary: mcpServers && mcpServers.length > 0 ? `${mcpServers.length} 个服务器` : undefined,
    showCacheHitRate: ui?.show_cache_hit_rate === true,
  }
}

/** 将绝对工作区路径压缩成窄终端可显示的最后一级目录名。 */
export function workspaceLabel(workspace?: string): string {
  if (!workspace) return "~"
  const normalized = workspace.replace(/\\/g, "/").replace(/\/+$/, "")
  const parts = normalized.split("/").filter(Boolean)
  return parts.at(-1) ?? workspace
}

/** 小尺寸终端优先保证输入和输出可读，不渲染装饰性背景。 */
export function supportsHomeDecoration(width: number, height: number): boolean {
  return width >= 88 && height >= 28
}

/** 返回执行安全状态，明确本机默认模式不是隔离环境。 */
export function executionStatusLabel(runtime: InteractiveRuntime): string {
  if (runtime.executionMode === "remote-sandbox") {
    return runtime.sandboxProvider ? `远端沙箱 · ${runtime.sandboxProvider}` : "远端沙箱"
  }
  return "本机执行 · 未隔离"
}

/** 返回与配置和协议一致的英文审批模式名，便于终端快速扫描。 */
export function approvalModeLabel(runtime: InteractiveRuntime): string {
  return runtime.approvalMode
}

/** 生成 /status 使用的本地只读运行摘要，不依赖额外 Agent 或 RPC 调用。 */
export function runtimeStatusSummary(runtime: InteractiveRuntime): string {
  const lines = [
    `工作区  ${runtime.workspace}`,
    `模型    ${modelLabel(runtime)}`,
    `执行    ${executionStatusLabel(runtime)}`,
    `审批    ${approvalModeLabel(runtime)}`,
  ]
  if (runtime.approvalModeWarning) lines.push(`提示    ${runtime.approvalModeWarning}`)
  if (runtime.mcpSummary) lines.push(`MCP     ${runtime.mcpSummary}`)
  else lines.push("MCP     未配置")
  if (runtime.startupError) lines.push(`错误    ${runtime.startupError}`)
  return lines.join("\n")
}

/** 将运行时模型配置转换为状态摘要使用的简短文案。 */
function modelLabel(runtime: InteractiveRuntime): string {
  if (!runtime.modelConfigured) return "模型未配置"
  return modelReference(runtime.modelProfileId, runtime.modelName) ?? "已配置模型"
}

/** 统一格式化脱敏 Profile ID 与模型名，供 TUI 展示当前模型。 */
function modelReference(profileId: string | undefined, modelName: string | undefined): string | undefined {
  if (!profileId && !modelName) return undefined
  return `${profileId ? `${profileId} · ` : ""}${modelName ?? "已配置模型"}`
}

/** 判断握手字段是否为普通对象，拒绝 null 和数组。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/** 读取非空字符串，否则使用安全回退值。 */
function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback
}

/** 将可选展示字段规范化为空或非空字符串。 */
function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}

/** 对来自协议的审批模式做白名单解析，未知值回退到保守的默认确认。 */
function approvalMode(value: unknown): InteractiveApprovalMode {
  return (APPROVAL_MODE_CYCLE as readonly unknown[]).includes(value) ? value as InteractiveApprovalMode : "default"
}

/** 按循环顺序返回下一个审批模式。 */
export function nextApprovalMode(current: InteractiveApprovalMode): InteractiveApprovalMode {
  const index = APPROVAL_MODE_CYCLE.indexOf(current)
  return APPROVAL_MODE_CYCLE[(index + 1) % APPROVAL_MODE_CYCLE.length] ?? "default"
}
