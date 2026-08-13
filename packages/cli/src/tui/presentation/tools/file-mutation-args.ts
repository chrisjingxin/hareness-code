/** 从 write/edit 工具参数里抽出路径和正文，避免把整段 JSON 当 UI。 */

export type FileMutationArgs = {
  path: string | null
  content: string | null
  oldString: string | null
  newString: string | null
}

function asNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null
}

/** 只认 file_path/path 与 content；完整 JSON 优先，流式残片用字段扫描。 */
export function parseFileMutationArgs(raw: string): FileMutationArgs {
  if (!raw.trim()) return { path: null, content: null, oldString: null, newString: null }
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>
    return {
      path: asNonEmptyString(parsed.file_path) ?? asNonEmptyString(parsed.path),
      content: typeof parsed.content === "string" ? parsed.content : null,
      oldString: typeof parsed.old_string === "string" ? parsed.old_string : null,
      newString: typeof parsed.new_string === "string" ? parsed.new_string : null,
    }
  } catch {
    return {
      path: extractJsonStringField(raw, "file_path") ?? extractJsonStringField(raw, "path"),
      content: extractJsonStringField(raw, "content"),
      oldString: extractJsonStringField(raw, "old_string"),
      newString: extractJsonStringField(raw, "new_string"),
    }
  }
}

/** 从可能未闭合的 JSON 里抽出一个字符串字段；未结束的 content 返回已到达部分。 */
function extractJsonStringField(raw: string, field: string): string | null {
  const header = new RegExp(`"${field}"\\s*:\\s*"`)
  const match = header.exec(raw)
  if (!match) return null
  let index = match.index + match[0].length
  let value = ""
  while (index < raw.length) {
    const char = raw[index]
    if (char === "\\") {
      if (index + 1 >= raw.length) return value || null
      const next = raw[index + 1]
      if (next === "n") value += "\n"
      else if (next === "t") value += "\t"
      else if (next === "r") value += "\r"
      else if (next === '"') value += '"'
      else if (next === "\\") value += "\\"
      else if (next === "/") value += "/"
      else if (next === "u" && index + 5 < raw.length) {
        value += String.fromCharCode(Number.parseInt(raw.slice(index + 2, index + 6), 16))
        index += 6
        continue
      } else value += next
      index += 2
      continue
    }
    if (char === '"') return value
    value += char
    index += 1
  }
  return value || null
}

/** 从工具结果 JSON 抽出恢复展示用的路径和正文窗口。 */
export function parseToolResultPreview(raw: string): FileMutationArgs {
  if (!raw.trim()) return { path: null, content: null, oldString: null, newString: null }
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>
    return {
      path: asNonEmptyString(parsed.path) ?? asNonEmptyString(parsed.file_path),
      content: typeof parsed.content === "string" ? parsed.content : null,
      oldString: typeof parsed.old_string === "string" ? parsed.old_string : null,
      newString: typeof parsed.new_string === "string" ? parsed.new_string : null,
    }
  } catch {
    return { path: null, content: null, oldString: null, newString: null }
  }
}

/** 展示用短路径：取最后两段，避免占满终端。 */
export function shortMutationPath(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean)
  if (parts.length <= 2) return parts.join("/") || path
  return parts.slice(-2).join("/")
}

/** 用 old/new 替换片段生成可被 Diff renderer 消费的 unified diff。 */
export function unifiedDiffFromReplacement(path: string, oldText: string, newText: string): string {
  const oldLines = oldText.split("\n")
  const newLines = newText.split("\n")
  let start = 0
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) start += 1
  let oldEnd = oldLines.length
  let newEnd = newLines.length
  while (oldEnd > start && newEnd > start && oldLines[oldEnd - 1] === newLines[newEnd - 1]) {
    oldEnd -= 1
    newEnd -= 1
  }
  const removed = oldLines.slice(start, oldEnd)
  const added = newLines.slice(start, newEnd)
  const file = path.replace(/^\//, "") || "file"
  const hunk = [
    `@@ -${start + 1},${removed.length} +${start + 1},${added.length} @@`,
    ...removed.map(line => `-${line}`),
    ...added.map(line => `+${line}`),
  ]
  return [`--- a/${file}`, `+++ b/${file}`, ...hunk].join("\n")
}
