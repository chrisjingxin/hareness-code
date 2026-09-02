/**
 * 共享提及词法触发策略：根据输入框文本与光标位置，解析是否触发 @ 文件补全。
 *
 * 纯函数，零 node/react/opentui import，支持普通路径 @query 与带引号路径 @"quoted query"。
 */

export type MentionQueryMatch =
  | {
      readonly active: true
      readonly query: string
      readonly start: number
      readonly end: number
      readonly isQuoted: boolean
    }
  | {
      readonly active: false
    }

/**
 * 从文本与光标 offset 提取当前光标所在的 @ 补全项。
 * 仅当 @ 位于行首或空白字符之后才触发，避免邮箱等单词中间的 @ 误触。
 */
export function extractMentionQuery(text: string, cursorOffset: number): MentionQueryMatch {
  if (cursorOffset < 0 || cursorOffset > text.length) {
    return { active: false }
  }

  const prefix = text.slice(0, cursorOffset)

  // 1. 优先检测带引号的 @"... 未闭合形式
  const quotedMatch = prefix.match(/(?:^|\s)@"([^"]*)$/)
  if (quotedMatch && quotedMatch.index !== undefined) {
    const fullMatch = quotedMatch[0]
    const atIndexInMatch = fullMatch.indexOf('@"')
    const start = quotedMatch.index + atIndexInMatch
    const query = quotedMatch[1] ?? ""
    return {
      active: true,
      query,
      start,
      end: cursorOffset,
      isQuoted: true,
    }
  }

  // 2. 检测未带引号的普通 @query 形式（query 中不包含空白字符与引号）
  const unquotedMatch = prefix.match(/(?:^|\s)@([^\s"']*)$/)
  if (unquotedMatch && unquotedMatch.index !== undefined) {
    const fullMatch = unquotedMatch[0]
    const atIndexInMatch = fullMatch.indexOf("@")
    const start = unquotedMatch.index + atIndexInMatch
    const query = unquotedMatch[1] ?? ""
    return {
      active: true,
      query,
      start,
      end: cursorOffset,
      isQuoted: false,
    }
  }

  return { active: false }
}
