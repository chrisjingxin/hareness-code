/**
 * 提及语法解析策略：从文本中提取所有 @ 文件提及声明，并解析行号切片（支持 GitHub #L 与 IDE : 风格）。
 *
 * 纯函数，零 node/react/opentui import。
 */

export type ParsedMention = {
  /** 原始匹配文本，如 '@src/app.tsx#L20-50' 或 '@"my doc.md"' */
  readonly raw: string
  /** 纯净相对路径，如 'src/app.tsx' 或 'my doc.md' */
  readonly path: string
  /** 索引起始行（1-based），若未指定则为 undefined */
  readonly lineStart?: number
  /** 索引结束行（1-based），若未指定则为 undefined */
  readonly lineEnd?: number
}

/**
 * 从用户消息文本中解析所有有效的 @ 提及。
 * 仅当 @ 位于行首或空白字符之后时才匹配，避免邮箱等误触。
 */
export function parseMentionsFromText(text: string): readonly ParsedMention[] {
  if (!text.includes("@")) return []

  // 匹配形式：
  // 1. 带引号：@"path with spaces"(?:(#L|:)(\d+)(?:-(\d+))?)?
  // 2. 无引号：@path/to/file(?:(#L|:)(\d+)(?:-(\d+))?)?
  const regex = /(?:^|\s)(?:@"([^"]+)"|@([^\s"'#:]+))(?:(?:#L|:)(\d+)(?:-(\d+))?)?/g

  const results: ParsedMention[] = []
  let match: RegExpExecArray | null

  while ((match = regex.exec(text)) !== null) {
    const fullMatch = match[0]
    const leadingWhitespace = fullMatch.match(/^\s+/)?.[0] ?? ""
    const raw = fullMatch.slice(leadingWhitespace.length)

    const rawPath = match[1] ?? match[2]
    if (!rawPath) continue

    const lineStartRaw = match[3]
    const lineEndRaw = match[4]

    let lineStart: number | undefined
    let lineEnd: number | undefined

    if (lineStartRaw !== undefined) {
      const parsedStart = parseInt(lineStartRaw, 10)
      if (lineEndRaw !== undefined) {
        const parsedEnd = parseInt(lineEndRaw, 10)
        lineStart = Math.min(parsedStart, parsedEnd)
        lineEnd = Math.max(parsedStart, parsedEnd)
      } else {
        lineStart = parsedStart
        lineEnd = parsedStart
      }
    }

    results.push({
      raw,
      path: rawPath,
      lineStart,
      lineEnd,
    })
  }

  return results
}
