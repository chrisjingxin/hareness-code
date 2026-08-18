/** 跨端共享 Tool 显示策略：工具名 → 动词标签、图标语义、副作用基调与主参数提取的唯一实现。 */

import { toolArgumentSummary } from "./tool-output-policy"

/**
 * 工具图标的语义名。
 * 本模块不依赖 UI 框架；Web 端映射到 lucide 组件、TUI 端映射到字符，各自维护映射表。
 */
export type ToolIconName =
  | "file-read"
  | "file-write"
  | "file-delete"
  | "folder"
  | "search"
  | "terminal"
  | "globe"
  | "brain"
  | "code"
  | "plan"
  | "agents"
  | "question"
  | "wrench"

/** 副作用基调：write 表示调用会改动文件或环境，渲染端用暖色图标提示；read/neutral 保持中性。 */
export type ToolDisplayTone = "read" | "write" | "neutral"

/** 单个工具的展示语义：动词标签、图标、基调与是否命中内置目录。 */
export type ToolDisplay = {
  /** 动词化中文标签；未知工具回退为原始工具名。 */
  label: string
  icon: ToolIconName
  tone: ToolDisplayTone
  /** false 表示 MCP/插件等未登记工具，此时 label 即原始工具名。 */
  known: boolean
}

type ToolDisplaySpec = {
  label: string
  icon: ToolIconName
  tone: ToolDisplayTone
  /** 主参数候选键，按优先级取第一个在 arguments 中出现且为标量的键。 */
  primaryArguments: readonly string[]
}

/**
 * 内置工具展示目录：名称与 Agent 端 file_tool_catalog、harness_tools、deferred_tools 对齐。
 * 新增内置工具时在此登记一次，Web/TUI 两端同时获得一致展示。
 */
const TOOL_DISPLAY_SPECS: Record<string, ToolDisplaySpec> = {
  read_file: { label: "读取文件", icon: "file-read", tone: "read", primaryArguments: ["file_path"] },
  write_file: { label: "写入文件", icon: "file-write", tone: "write", primaryArguments: ["file_path"] },
  edit_file: { label: "编辑文件", icon: "file-write", tone: "write", primaryArguments: ["file_path"] },
  delete_file: { label: "删除文件", icon: "file-delete", tone: "write", primaryArguments: ["file_path"] },
  ls: { label: "列出目录", icon: "folder", tone: "read", primaryArguments: ["path"] },
  glob: { label: "搜索文件", icon: "search", tone: "read", primaryArguments: ["pattern", "path"] },
  grep: { label: "查找内容", icon: "search", tone: "read", primaryArguments: ["pattern", "path"] },
  lsp: { label: "代码洞察", icon: "code", tone: "read", primaryArguments: ["action", "file_path"] },
  execute: { label: "执行命令", icon: "terminal", tone: "write", primaryArguments: ["command"] },
  monitor: { label: "后台监控", icon: "terminal", tone: "write", primaryArguments: ["command"] },
  web_search: { label: "联网搜索", icon: "globe", tone: "read", primaryArguments: ["query"] },
  web_fetch: { label: "抓取网页", icon: "globe", tone: "read", primaryArguments: ["url"] },
  memory_save: { label: "保存记忆", icon: "brain", tone: "write", primaryArguments: ["key"] },
  memory_search: { label: "检索记忆", icon: "brain", tone: "read", primaryArguments: ["query"] },
  write_todos: { label: "更新待办", icon: "plan", tone: "neutral", primaryArguments: [] },
  task: { label: "子代理任务", icon: "agents", tone: "neutral", primaryArguments: ["description", "prompt"] },
  enter_plan_mode: { label: "进入计划模式", icon: "plan", tone: "neutral", primaryArguments: [] },
  exit_plan_mode: { label: "退出计划模式", icon: "plan", tone: "neutral", primaryArguments: [] },
  ask_user: { label: "询问用户", icon: "question", tone: "neutral", primaryArguments: ["question"] },
}

/** 主参数单行最大字符数：比参数摘要更短，保证工具行一眼读完。 */
const PRIMARY_ARGUMENT_MAX = 72

/**
 * 查询工具的展示语义；未登记工具（MCP、插件）回退为 wrench 图标 + 原始工具名。
 */
export function toolDisplay(name: string): ToolDisplay {
  const spec = TOOL_DISPLAY_SPECS[name]
  if (!spec) {
    return { label: name, icon: "wrench", tone: "neutral", known: false }
  }
  return { label: spec.label, icon: spec.icon, tone: spec.tone, known: true }
}

/**
 * 提取工具行展示的主参数：已知工具按目录优先级取第一个标量值；
 * 取不到（缺键、嵌套值、非 JSON arguments）时回退到 key 级参数摘要，保证任何调用都有可读单行。
 */
export function toolPrimaryArgument(
  name: string,
  argumentsText: string | undefined,
  maxChars: number = PRIMARY_ARGUMENT_MAX,
): string | null {
  const spec = TOOL_DISPLAY_SPECS[name]
  if (spec && spec.primaryArguments.length > 0 && argumentsText) {
    const parsed = parseArgumentsObject(argumentsText)
    if (parsed) {
      for (const key of spec.primaryArguments) {
        const scalar = scalarArgumentText(parsed[key])
        if (scalar) return truncateSingleLine(scalar, maxChars)
      }
    }
  }
  return toolArgumentSummary(argumentsText, maxChars)
}

/** arguments 是 JSON 对象字符串时解析为记录；其余情况返回 null。 */
function parseArgumentsObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text)
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return null
  } catch {
    return null
  }
}

/** 标量参数值收敛为展示文本；嵌套对象/数组/空值返回 null，让位给下一个候选键。 */
function scalarArgumentText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim()
    return trimmed.length > 0 ? trimmed : null
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return null
}

/** 空白归一化为单行并截断；超长追加省略号。 */
function truncateSingleLine(text: string, maxChars: number): string {
  const singleLine = text.replace(/\s+/g, " ").trim()
  if (singleLine.length <= maxChars) return singleLine
  return `${singleLine.slice(0, maxChars - 1)}…`
}
