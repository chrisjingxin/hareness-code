/** 文件审批 unified diff 的纯解析与双栏对齐模型。 */

export type FileDiffLineKind = "context" | "add" | "remove" | "no-newline"

export type FileDiffLine = {
  readonly kind: FileDiffLineKind
  readonly text: string
  readonly oldLine: number | null
  readonly newLine: number | null
}

export type FileDiffHunk = {
  readonly header: string
  readonly oldStart: number
  readonly oldCount: number
  readonly newStart: number
  readonly newCount: number
  readonly lines: readonly FileDiffLine[]
}

export type ParsedFileDiff =
  | { readonly status: "parsed"; readonly hunks: readonly FileDiffHunk[] }
  | { readonly status: "invalid"; readonly reason: string }

export type FileDiffSplitRow = {
  readonly left: FileDiffLine | null
  readonly right: FileDiffLine | null
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$/
const TRUNCATION_MARKER = "[diff 因行数或字节上限截断]"

/** 解析标准 unified diff；允许尾部因审批上限截断而缺少完整 hunk 行数。 */
export function parseFileDiff(unifiedDiff: string): ParsedFileDiff {
  if (unifiedDiff === "") return { status: "parsed", hunks: [] }
  const rawLines = unifiedDiff.replaceAll("\r\n", "\n").split("\n")
  const hunks: FileDiffHunk[] = []
  let index = 0
  while (index < rawLines.length && (rawLines[index]!.startsWith("--- ") || rawLines[index]!.startsWith("+++ "))) {
    index++
  }

  while (index < rawLines.length) {
    const header = rawLines[index]!
    if (header === "" && index === rawLines.length - 1) break
    if (header === TRUNCATION_MARKER) break
    const match = HUNK_HEADER.exec(header)
    if (!match) return { status: "invalid", reason: "missing-hunk-header" }
    index++
    let oldLine = Number(match[1])
    let newLine = Number(match[3])
    const lines: FileDiffLine[] = []

    while (index < rawLines.length) {
      const raw = rawLines[index]!
      if (HUNK_HEADER.test(raw)) break
      if (raw === TRUNCATION_MARKER || (raw === "" && index === rawLines.length - 1)) {
        index++
        break
      }
      if (raw === "\\ No newline at end of file") {
        lines.push({ kind: "no-newline", text: raw, oldLine: null, newLine: null })
      } else if (raw.startsWith(" ")) {
        lines.push({ kind: "context", text: raw.slice(1), oldLine, newLine })
        oldLine++
        newLine++
      } else if (raw.startsWith("-")) {
        lines.push({ kind: "remove", text: raw.slice(1), oldLine, newLine: null })
        oldLine++
      } else if (raw.startsWith("+")) {
        lines.push({ kind: "add", text: raw.slice(1), oldLine: null, newLine })
        newLine++
      } else {
        return { status: "invalid", reason: "invalid-hunk-line" }
      }
      index++
    }
    hunks.push({
      header,
      oldStart: Number(match[1]),
      oldCount: match[2] === undefined ? 1 : Number(match[2]),
      newStart: Number(match[3]),
      newCount: match[4] === undefined ? 1 : Number(match[4]),
      lines,
    })
  }
  return { status: "parsed", hunks }
}

/** 把一个 hunk 的连续 remove/add block 对齐为左右单元格，不跨 hunk 猜测语义。 */
export function alignFileDiffHunk(hunk: FileDiffHunk): readonly FileDiffSplitRow[] {
  const rows: FileDiffSplitRow[] = []
  let index = 0
  while (index < hunk.lines.length) {
    const line = hunk.lines[index]!
    if (line.kind === "context") {
      rows.push({ left: line, right: line })
      index++
      continue
    }
    if (line.kind === "no-newline") {
      rows.push({ left: line, right: line })
      index++
      continue
    }
    const removed: FileDiffLine[] = []
    const added: FileDiffLine[] = []
    const markers: FileDiffLine[] = []
    while (index < hunk.lines.length && hunk.lines[index]!.kind !== "context") {
      const changed = hunk.lines[index]!
      if (changed.kind === "remove") removed.push(changed)
      if (changed.kind === "add") added.push(changed)
      if (changed.kind === "no-newline") markers.push(changed)
      index++
    }
    const count = Math.max(removed.length, added.length)
    for (let row = 0; row < count; row++) {
      rows.push({ left: removed[row] ?? null, right: added[row] ?? null })
    }
    for (const marker of markers) rows.push({ left: marker, right: marker })
  }
  return rows
}

/**
 * 把有界审批预览转成原生 Diff renderer 可严格解析的文本。
 *
 * 服务端可在 hunk 中途截断，原始 header 仍保留完整 mutation 的行数。
 * OpenTUI 会严格校验该计数，因此这里只针对展示副本把 header 收缩为实际
 * 可见行数；这不会改写 Protocol 统计或被批准的 prepared mutation。
 */
export function diffTextForRenderer(unifiedDiff: string): string {
  const normalized = unifiedDiff.replaceAll("\r\n", "\n")
  const parsed = parseFileDiff(normalized)
  if (parsed.status === "invalid") {
    const lines = normalized.split("\n")
    if (lines.at(-1) === TRUNCATION_MARKER) lines.pop()
    return lines.join("\n")
  }

  const rawLines = normalized.split("\n")
  const rendered: string[] = []
  let headerIndex = 0
  while (headerIndex < rawLines.length && (rawLines[headerIndex]!.startsWith("--- ") || rawLines[headerIndex]!.startsWith("+++ "))) {
    rendered.push(rawLines[headerIndex]!)
    headerIndex++
  }
  for (const hunk of parsed.hunks) {
    const oldCount = hunk.lines.filter(line => line.kind === "context" || line.kind === "remove").length
    const newCount = hunk.lines.filter(line => line.kind === "context" || line.kind === "add").length
    const closingMarker = hunk.header.indexOf("@@", 2)
    const suffix = closingMarker >= 0 ? hunk.header.slice(closingMarker + 2) : ""
    rendered.push(`@@ -${hunk.oldStart},${oldCount} +${hunk.newStart},${newCount} @@${suffix}`)
    rendered.push(...hunk.lines.map(line => {
      if (line.kind === "no-newline") return line.text
      const prefix = line.kind === "context" ? " " : line.kind === "add" ? "+" : "-"
      return `${prefix}${line.text}`
    }))
  }
  return rendered.join("\n")
}
