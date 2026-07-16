/** TUI 运行时展示模型：集中处理握手摘要、终端降级和格式化。 */

import type { InitializeResult } from "@za38/protocol"

export const CLI_VERSION = "0.1.0"

export type TuiRuntime = {
  workspace: string
  gitBranch?: string
  cliVersion: string
  modelName?: string
  modelConfigured: boolean
  startupError?: string
  executionMode: "local" | "remote-sandbox"
  sandboxProvider?: string
  approvalMode: "plan" | "ask" | "auto-edit"
}

/** 将握手结果收敛为界面可安全显示的运行摘要，避免把配置原样暴露给组件。 */
export function createTuiRuntime(
  result: InitializeResult,
  cwd: string,
  options: { gitBranch?: string; cliVersion?: string } = {},
): TuiRuntime {
  const config = isRecord(result.config_summary) ? result.config_summary : undefined
  const model = config && isRecord(config.model) ? config.model : undefined
  const security = config && isRecord(config.security) ? config.security : undefined
  return {
    workspace: stringValue(config?.workspace, cwd),
    gitBranch: optionalString(options.gitBranch),
    cliVersion: options.cliVersion ?? CLI_VERSION,
    modelName: optionalString(model?.name),
    modelConfigured: model?.api_key_configured === true,
    startupError: isRecord(result.startup_error) ? optionalString(result.startup_error.message) : undefined,
    executionMode: security?.mode === "remote-sandbox" ? "remote-sandbox" : "local",
    sandboxProvider: optionalString(security?.provider),
    approvalMode: approvalMode(security?.approval_mode),
  }
}

/** 将绝对工作区路径压缩成窄终端可显示的最后一级目录名。 */
export function workspaceLabel(workspace: string): string {
  const normalized = workspace.replace(/\\/g, "/").replace(/\/+$/, "")
  const parts = normalized.split("/").filter(Boolean)
  return parts.at(-1) ?? workspace
}

/** 小尺寸终端优先保证输入和输出可读，不渲染装饰性背景。 */
export function supportsHomeDecoration(width: number, height: number): boolean {
  return width >= 88 && height >= 28
}

/** 根据启动错误和模型配置生成不泄露凭据的连接状态文案。 */
export function runtimeStatusLabel(runtime: TuiRuntime): string {
  if (runtime.startupError) return "配置需要处理"
  if (!runtime.modelConfigured) return "模型未配置"
  return "Agent 已连接"
}

/** 返回执行安全状态，明确本机默认模式不是隔离环境。 */
export function executionStatusLabel(runtime: TuiRuntime): string {
  if (runtime.executionMode === "remote-sandbox") {
    return runtime.sandboxProvider ? `远端沙箱 · ${runtime.sandboxProvider}` : "远端沙箱"
  }
  return "本机执行 · 未隔离"
}

/** 将毫秒耗时格式化为紧凑的毫秒或秒显示。 */
export function formatDuration(durationMs: number | undefined): string | undefined {
  if (!durationMs || durationMs < 1) return undefined
  if (durationMs < 1000) return `${durationMs}ms`
  return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`
}

/** 将 token 用量格式化为终端底栏可读的 in/out 摘要。 */
export function formatUsage(usage: { inputTokens: number; outputTokens: number } | undefined): string | undefined {
  if (!usage) return undefined
  return `${compactNumber(usage.inputTokens)} in · ${compactNumber(usage.outputTokens)} out`
}

/** 将大数字转换为 k 单位，避免底栏在窄终端换行。 */
function compactNumber(value: number): string {
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k`
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

/** 对来自协议的审批模式做白名单解析，未知值回退到保守的 ask。 */
function approvalMode(value: unknown): "plan" | "ask" | "auto-edit" {
  if (value === "plan" || value === "auto-edit" || value === "ask") return value
  return "ask"
}
