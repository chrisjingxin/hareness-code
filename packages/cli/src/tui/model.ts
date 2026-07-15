import type { InitializeResult } from "@za38/protocol"

export const CLI_VERSION = "0.1.0"

export type TuiRuntime = {
  workspace: string
  gitBranch?: string
  cliVersion: string
  modelName?: string
  modelConfigured: boolean
  startupError?: string
}

/** 将握手结果收敛为界面可安全显示的运行摘要，避免把配置原样暴露给组件。 */
export function createTuiRuntime(
  result: InitializeResult,
  cwd: string,
  options: { gitBranch?: string; cliVersion?: string } = {},
): TuiRuntime {
  const config = isRecord(result.config) ? result.config : undefined
  const model = config && isRecord(config.model) ? config.model : undefined
  return {
    workspace: stringValue(config?.workspace, cwd),
    gitBranch: optionalString(options.gitBranch),
    cliVersion: options.cliVersion ?? CLI_VERSION,
    modelName: optionalString(model?.name),
    modelConfigured: model?.api_key_configured === true,
    startupError: optionalString(result.startup_error),
  }
}

export function workspaceLabel(workspace: string): string {
  const normalized = workspace.replace(/\\/g, "/").replace(/\/+$/, "")
  const parts = normalized.split("/").filter(Boolean)
  return parts.at(-1) ?? workspace
}

/** 小尺寸终端优先保证输入和输出可读，不渲染装饰性背景。 */
export function supportsHomeDecoration(width: number, height: number): boolean {
  return width >= 88 && height >= 28
}

export function runtimeStatusLabel(runtime: TuiRuntime): string {
  if (runtime.startupError) return "配置需要处理"
  if (!runtime.modelConfigured) return "模型未配置"
  return "Agent 已连接"
}

export function formatDuration(durationMs: number | undefined): string | undefined {
  if (!durationMs || durationMs < 1) return undefined
  if (durationMs < 1000) return `${durationMs}ms`
  return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`
}

export function formatUsage(usage: { inputTokens: number; outputTokens: number } | undefined): string | undefined {
  if (!usage) return undefined
  return `${compactNumber(usage.inputTokens)} in · ${compactNumber(usage.outputTokens)} out`
}

function compactNumber(value: number): string {
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}
