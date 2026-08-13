/** 工具名到 TUI Renderer 的分流；未知一律 generic。 */

export type ToolRendererKind = "inline" | "block" | "diff" | "generic"

const INLINE = new Set([
  "read",
  "read_file",
  "grep",
  "glob",
  "ls",
  "list",
  "webfetch",
  "web_fetch",
  "websearch",
  "web_search",
  "codesearch",
  "view_image",
])

const BLOCK = new Set(["execute", "bash", "exec", "shell"])

const DIFF = new Set(["write_file", "write", "edit_file", "edit", "delete_file", "delete"])

/** 去掉首尾空白、转小写，并丢掉末尾非字母，便于 Read / execute. 等别名命中。 */
export function canonicalizeToolName(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z]+$/g, "")
}

/** 只暴露工具名 → 四种 kind；没有映射时必须是 generic。 */
export function resolveToolRenderer(name: string): ToolRendererKind {
  const key = canonicalizeToolName(name)
  if (INLINE.has(key)) return "inline"
  if (BLOCK.has(key)) return "block"
  if (DIFF.has(key)) return "diff"
  return "generic"
}
