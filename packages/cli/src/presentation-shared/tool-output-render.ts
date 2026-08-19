/**
 * Tool 展开详情的渲染模型（纯函数，Web/TUI 可复用）。
 *
 * 按工具名分派结构化解析（read_file → file-content、ls/glob → path-list、
 * grep → grep-matches/path-list），其余走 JSON 美化或纯文本回退。
 * 所有解析失败一律安全回退，不向渲染端抛异常。
 */

/** 输出超过该行数时展示「展开全部/收起」切换，钳制高度由渲染端样式表达。 */
export const TOOL_OUTPUT_COLLAPSE_LINES = 24

/** file-content / grep 的一行：number 为 null 表示该行无行号（容错）。 */
export type ToolNumberedLine = {
  number: number | null
  text: string
}

export type ToolFileContent = {
  path: string
  shownStart: number
  shownEnd: number
  totalLines: number
  truncated: boolean
  lines: ToolNumberedLine[]
}

export type ToolPathList = {
  entries: string[]
}

export type ToolGrepMatch = {
  line: number | null
  text: string
  /** count 模式的命中数；content 模式缺省。 */
  count?: number
}

export type ToolGrepMatches = {
  mode: "content" | "count"
  groups: Array<{ path: string; matches: ToolGrepMatch[] }>
}

/** diff 一行：context 公共行、add 新增、remove 删除。 */
export type ToolDiffRow = {
  type: "context" | "add" | "remove"
  text: string
}

export type ToolDiff = {
  path: string | null
  added: number
  removed: number
  rows: ToolDiffRow[]
}

export type ToolTerminal = {
  command: string | null
  exitCode: number | null
  truncated: boolean
  lines: string[]
}

export type ToolOutputView = {
  kind: "text" | "json" | "file-content" | "path-list" | "grep-matches" | "diff" | "terminal"
  /** 复制文本：text/json 为展示文本；结构化种类为原始 output。 */
  text: string
  /** 渲染行数（结构化种类按渲染行计），驱动折叠阈值与行数统计。 */
  lineCount: number
  /** 头部条统计文案（「N 行」/「N 项」）。 */
  metaLabel: string
  collapsible: boolean
  fileContent?: ToolFileContent
  pathList?: ToolPathList
  grepMatches?: ToolGrepMatches
  diff?: ToolDiff
  terminal?: ToolTerminal
}

/** 仅美化 JSON 对象/数组；标量与非法 JSON 返回 null 让调用方回退原文。 */
export function prettifyJson(text: string): string | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== "object") return null
    return JSON.stringify(parsed, null, 2)
  } catch {
    return null
  }
}

/** 统计展示行数；空文本为 0 行。 */
export function outputLineCount(text: string): number {
  if (text.length === 0) return 0
  return text.split("\n").length
}

/** 构建 Tool 输出区的渲染模型；按工具名分派，解析失败回退 JSON/纯文本。 */
export function toolOutputView(toolName: string, output: string, argumentsText?: string): ToolOutputView {
  const structured = parseStructured(toolName, output, argumentsText)
  if (structured) return structured
  const pretty = prettifyJson(output)
  const text = pretty ?? output
  const lineCount = outputLineCount(text)
  return {
    kind: pretty !== null ? "json" : "text",
    text,
    lineCount,
    metaLabel: `${lineCount} 行`,
    collapsible: lineCount > TOOL_OUTPUT_COLLAPSE_LINES,
  }
}

/** 工具名 → 结构化解析；未登记或解析失败返回 null。 */
function parseStructured(toolName: string, output: string, argumentsText?: string): ToolOutputView | null {
  if (!output.trim()) return null
  if (toolName === "read_file") return parseReadFile(output)
  if (toolName === "edit_file" || toolName === "write_file") {
    return parseMutationDiff(toolName, output, argumentsText) ?? parseReadFile(output)
  }
  if (toolName === "ls" || toolName === "glob") return parsePathList(output)
  if (toolName === "grep") return parseGrep(output)
  if (toolName === "execute") return parseTerminal(output, argumentsText)
  return null
}

// ---------- read_file → file-content ----------

/** content 行格式为「行号<TAB>内容」；缺前缀的行容错为 number: null。 */
const NUMBERED_LINE = /^(\d+)\t(.*)$/

function parseReadFile(output: string): ToolOutputView | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(output)
  } catch {
    return null
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null
  const record = parsed as Record<string, unknown>
  if (typeof record.content !== "string" || typeof record.path !== "string") return null
  const shown = (record.shown_lines ?? {}) as Record<string, unknown>
  const lines: ToolNumberedLine[] = record.content.split("\n").map(line => {
    const match = NUMBERED_LINE.exec(line)
    return match ? { number: Number(match[1]), text: match[2] ?? "" } : { number: null, text: line }
  })
  const fileContent: ToolFileContent = {
    path: record.path,
    shownStart: typeof shown.start_line === "number" ? shown.start_line : (lines[0]?.number ?? 1),
    shownEnd: typeof shown.end_line === "number" ? shown.end_line : (lines[lines.length - 1]?.number ?? 1),
    totalLines: typeof record.total_lines === "number" ? record.total_lines : lines.length,
    truncated: record.truncated === true,
    lines,
  }
  return {
    kind: "file-content",
    text: output,
    lineCount: lines.length,
    metaLabel: `${lines.length} 行`,
    collapsible: lines.length > TOOL_OUTPUT_COLLAPSE_LINES,
    fileContent,
  }
}

// ---------- ls / glob → path-list ----------

/**
 * 解析 Python repr 字符串列表（`['/a', '/b/']`，兼容双引号与常见转义）。
 * 任何形状异常返回 null，让调用方回退纯文本。
 */
function parseReprStringList(output: string): string[] | null {
  const text = output.trim()
  if (!text.startsWith("[") || !text.endsWith("]")) return null
  const entries: string[] = []
  let index = 1
  const end = text.length - 1
  const skipWhitespace = (): void => {
    while (index < end && /\s/.test(text[index] ?? "")) index += 1
  }
  skipWhitespace()
  if (index === end) return entries
  for (;;) {
    skipWhitespace()
    const quote = text[index]
    if (quote !== "'" && quote !== '"') return null
    index += 1
    let value = ""
    for (;;) {
      if (index >= end) return null
      const char = text[index] ?? ""
      if (char === "\\") {
        const escaped = text[index + 1]
        if (escaped === undefined) return null
        value += escaped === "n" ? "\n" : escaped === "t" ? "\t" : escaped
        index += 2
        continue
      }
      if (char === quote) {
        index += 1
        break
      }
      value += char
      index += 1
    }
    entries.push(value)
    skipWhitespace()
    if (index === end) return entries
    if (text[index] !== ",") return null
    index += 1
  }
}

function parsePathList(output: string): ToolOutputView | null {
  const entries = parseReprStringList(output)
  if (entries === null) return null
  return {
    kind: "path-list",
    text: output,
    lineCount: entries.length,
    metaLabel: `${entries.length} 项`,
    collapsible: entries.length > TOOL_OUTPUT_COLLAPSE_LINES,
    pathList: { entries },
  }
}

// ---------- grep → grep-matches / path-list ----------

const GREP_GROUP_HEADER = /^(\S[^]*?):\s*$/
const GREP_MATCH_LINE = /^ {2}(\d+): (.*)$/
const GREP_COUNT_LINE = /^(.+): (\d+)$/

function parseGrep(output: string): ToolOutputView | null {
  const lines = output.split("\n")
  // content 模式：路径头行（以冒号结尾、不缩进）+ 两空格缩进的「行号: 内容」。
  const contentGroups: Array<{ path: string; matches: ToolGrepMatch[] }> = []
  let current: { path: string; matches: ToolGrepMatch[] } | null = null
  let contentOk = lines.length > 0
  for (const line of lines) {
    const matchLine = GREP_MATCH_LINE.exec(line)
    if (matchLine && current) {
      current.matches.push({ line: Number(matchLine[1]), text: matchLine[2] ?? "" })
      continue
    }
    const header = !line.startsWith(" ") ? GREP_GROUP_HEADER.exec(line) : null
    if (header) {
      current = { path: header[1] ?? "", matches: [] }
      contentGroups.push(current)
      continue
    }
    contentOk = false
    break
  }
  if (contentOk && current !== null && contentGroups.some(group => group.matches.length > 0)) {
    const lineCount = contentGroups.reduce((sum, group) => sum + 1 + group.matches.length, 0)
    return {
      kind: "grep-matches",
      text: output,
      lineCount,
      metaLabel: `${lineCount} 行`,
      collapsible: lineCount > TOOL_OUTPUT_COLLAPSE_LINES,
      grepMatches: { mode: "content", groups: contentGroups },
    }
  }
  // count 模式：每行「路径: 命中数」。
  const countGroups: Array<{ path: string; matches: ToolGrepMatch[] }> = []
  let countOk = lines.length > 0
  for (const line of lines) {
    const countLine = GREP_COUNT_LINE.exec(line)
    if (!countLine) {
      countOk = false
      break
    }
    countGroups.push({ path: countLine[1] ?? "", matches: [{ line: null, text: "", count: Number(countLine[2]) }] })
  }
  if (countOk && countGroups.length > 0) {
    return {
      kind: "grep-matches",
      text: output,
      lineCount: countGroups.length,
      metaLabel: `${countGroups.length} 个文件`,
      collapsible: countGroups.length > TOOL_OUTPUT_COLLAPSE_LINES,
      grepMatches: { mode: "count", groups: countGroups },
    }
  }
  // files_with_matches 模式：每行一个绝对路径。
  if (lines.every(line => line.startsWith("/"))) {
    return {
      kind: "path-list",
      text: output,
      lineCount: lines.length,
      metaLabel: `${lines.length} 项`,
      collapsible: lines.length > TOOL_OUTPUT_COLLAPSE_LINES,
      pathList: { entries: lines },
    }
  }
  return null
}

// ---------- edit_file / write_file → diff（红绿行来自 arguments 的 old/new） ----------

/** LCS 规模上限：超过后退化为「旧全删 + 新全增」，避免大文件 O(n·m) 卡顿。 */
const DIFF_LCS_CELL_LIMIT = 250_000

/**
 * 编辑/写入的红绿 diff 来自 arguments（old_string → new_string / content），
 * 因为只有成功结果（output JSON `ok: true`）才能证明变更已落盘；
 * 其余情况返回 null，让调用方回退 file-content/json。
 */
function parseMutationDiff(toolName: string, output: string, argumentsText?: string): ToolOutputView | null {
  if (!argumentsText) return null
  let args: unknown
  let result: unknown
  try {
    args = JSON.parse(argumentsText)
    result = JSON.parse(output)
  } catch {
    return null
  }
  if (args === null || typeof args !== "object" || Array.isArray(args)) return null
  if (result === null || typeof result !== "object" || Array.isArray(result)) return null
  if ((result as Record<string, unknown>).ok !== true) return null
  const record = args as Record<string, unknown>
  const path = typeof record.file_path === "string" ? record.file_path : null
  const oldText = toolName === "edit_file"
    ? (typeof record.old_string === "string" ? record.old_string : null)
    : ""
  const newText = toolName === "edit_file"
    ? (typeof record.new_string === "string" ? record.new_string : null)
    : (typeof record.content === "string" ? record.content : null)
  if (oldText === null || newText === null) return null
  const rows = diffLines(oldText, newText)
  const added = rows.filter(row => row.type === "add").length
  const removed = rows.filter(row => row.type === "remove").length
  return {
    kind: "diff",
    text: output,
    lineCount: rows.length,
    metaLabel: `+${added} −${removed}`,
    collapsible: rows.length > TOOL_OUTPUT_COLLAPSE_LINES,
    diff: { path, added, removed, rows },
  }
}

/** 行级 diff：LCS 对齐公共行；超规模退化为全删全增。空文本按 0 行计。 */
function diffLines(oldText: string, newText: string): ToolDiffRow[] {
  const oldLines = oldText.length > 0 ? oldText.split("\n") : []
  const newLines = newText.length > 0 ? newText.split("\n") : []
  if (oldLines.length * newLines.length > DIFF_LCS_CELL_LIMIT) {
    return [
      ...oldLines.map((text): ToolDiffRow => ({ type: "remove", text })),
      ...newLines.map((text): ToolDiffRow => ({ type: "add", text })),
    ]
  }
  const rows: ToolDiffRow[] = []
  // LCS 长度表（从右下角向左上填）。
  const width = newLines.length + 1
  const table = new Uint32Array((oldLines.length + 1) * width)
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      table[i * width + j] = oldLines[i] === newLines[j]
        ? (table[(i + 1) * width + j + 1] ?? 0) + 1
        : Math.max(table[(i + 1) * width + j] ?? 0, table[i * width + j + 1] ?? 0)
    }
  }
  let i = 0
  let j = 0
  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      rows.push({ type: "context", text: oldLines[i] ?? "" })
      i += 1
      j += 1
    } else if ((table[(i + 1) * width + j] ?? 0) >= (table[i * width + j + 1] ?? 0)) {
      rows.push({ type: "remove", text: oldLines[i] ?? "" })
      i += 1
    } else {
      rows.push({ type: "add", text: newLines[j] ?? "" })
      j += 1
    }
  }
  while (i < oldLines.length) {
    rows.push({ type: "remove", text: oldLines[i] ?? "" })
    i += 1
  }
  while (j < newLines.length) {
    rows.push({ type: "add", text: newLines[j] ?? "" })
    j += 1
  }
  return rows
}

// ---------- execute → terminal ----------

const EXECUTE_STATUS_MARKER = /^\[Command (succeeded|failed) with exit code (\d+)\]$/
const EXECUTE_TRUNCATED_MARKER = "[Output was truncated due to size limits]"

/** execute 输出 = 合并的 stdout/stderr 原文 + 尾部执行标记（deepagents 约定），剥离标记渲染终端块。 */
function parseTerminal(output: string, argumentsText?: string): ToolOutputView | null {
  let command: string | null = null
  if (argumentsText) {
    try {
      const parsed: unknown = JSON.parse(argumentsText)
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        const value = (parsed as Record<string, unknown>).command
        if (typeof value === "string") command = value
      }
    } catch {
      command = null
    }
  }
  const lines = output.split("\n")
  let exitCode: number | null = null
  let truncated = false
  // 尾部标记只出现在末尾（允许中间空行），剥离后余下为真实输出。
  while (lines.length > 0) {
    const last = lines[lines.length - 1] ?? ""
    const status = EXECUTE_STATUS_MARKER.exec(last)
    if (status) {
      exitCode = Number(status[2])
      lines.pop()
      continue
    }
    if (last === EXECUTE_TRUNCATED_MARKER) {
      truncated = true
      lines.pop()
      continue
    }
    if (last === "" && (exitCode !== null || truncated)) {
      lines.pop()
      continue
    }
    break
  }
  const renderedLines = command !== null ? lines.length + 1 : lines.length
  return {
    kind: "terminal",
    text: output,
    lineCount: renderedLines,
    metaLabel: exitCode !== null ? `exit ${exitCode}` : `${renderedLines} 行`,
    collapsible: renderedLines > TOOL_OUTPUT_COLLAPSE_LINES,
    terminal: { command, exitCode, truncated, lines },
  }
}
