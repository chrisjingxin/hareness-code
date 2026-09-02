/**
 * 共享提及候选搜索策略：基于工作区文件列表做过滤、排序并保留完整匹配统计。
 *
 * 纯函数，零 node/react/opentui import。
 */

import { resolveLanguageForPath } from "./language-catalog"

export type MentionCandidateItem = {
  readonly path: string
  readonly name: string
  readonly kind: "directory" | "file" | "symlink"
}

export type MentionOption = {
  readonly path: string
  readonly name: string
  readonly kind: "directory" | "file" | "symlink"
  readonly language: string | null
  /** 在 path 上的半开匹配区间，供 TUI 与 Web 做一致高亮。 */
  readonly matchRanges: readonly { readonly start: number; readonly end: number }[]
}

export type MentionSearchResult = {
  readonly items: readonly MentionOption[]
  readonly totalMatches: number
  readonly truncated: boolean
}

/** 菜单关闭时复用的空候选，避免无意义扫描完整工作区。 */
export const EMPTY_MENTION_SEARCH_RESULT: MentionSearchResult = {
  items: [],
  totalMatches: 0,
  truncated: false,
}

/** 候选池只限制传给表现层的项目数，totalMatches 始终反映完整匹配数。 */
export const MENTION_CANDIDATE_LIMIT = 1_000

/** 过滤并排序工作区文件候选，只返回可直接回填的文件或 symlink。 */
export function searchMentionOptions(
  items: readonly MentionCandidateItem[],
  query: string,
): MentionSearchResult {
  const normalizedQuery = query.trim().toLowerCase()

  // 1. 排除目录，仅保留可引用的文件或符号链接
  const fileItems = items.filter(item => item.kind !== "directory")

  if (!normalizedQuery) {
    return toSearchResult(fileItems, normalizedQuery)
  }

  // 2. 评分与匹配
  type ScoredItem = { item: MentionCandidateItem; score: number }
  const matched: ScoredItem[] = []

  for (const item of fileItems) {
    const lowerPath = item.path.toLowerCase()
    const lowerName = item.name.toLowerCase()

    let score = 0
    if (lowerName === normalizedQuery) {
      score = 100 // 文件名完全匹配
    } else if (lowerName.startsWith(normalizedQuery)) {
      score = 80 // 文件名前缀匹配
    } else if (lowerPath.startsWith(normalizedQuery)) {
      score = 60 // 路径前缀匹配
    } else if (lowerName.includes(normalizedQuery)) {
      score = 40 // 文件名包含
    } else if (lowerPath.includes(normalizedQuery)) {
      score = 20 // 路径包含
    } else {
      continue // 不匹配
    }

    // 短路径与浅层级优先微调
    const depthPenalty = Math.min(10, item.path.split("/").length)
    matched.push({ item, score: score - depthPenalty })
  }

  // 3. 按相关度降序、再按路径字母升序排序
  matched.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score
    return a.item.path.localeCompare(b.item.path)
  })

  return toSearchResult(matched.map(item => item.item), normalizedQuery)
}

/** 空查询浏览当前目录，非空查询恢复全工作区文件搜索。 */
export function mentionOptionsForQuery(
  items: readonly MentionCandidateItem[],
  query: string,
  currentDirectory: string,
): MentionSearchResult {
  if (query.trim()) return searchMentionOptions(items, query)
  const children = items
    .filter(item => parentMentionDirectory(item.path) === currentDirectory)
    .sort(compareMentionSiblings)
  return toSearchResult(children, "")
}

/** 返回相对路径的父目录；根目录及根的直接子目录都返回空串。 */
export function parentMentionDirectory(relativePath: string): string {
  const index = relativePath.lastIndexOf("/")
  return index < 0 ? "" : relativePath.slice(0, index)
}

function compareMentionSiblings(a: MentionCandidateItem, b: MentionCandidateItem): number {
  const rank = (item: MentionCandidateItem): number => item.kind === "directory" ? 0 : item.kind === "file" ? 1 : 2
  const rankDiff = rank(a) - rank(b)
  if (rankDiff !== 0) return rankDiff
  return a.name.localeCompare(b.name, "zh", { numeric: true, sensitivity: "base" })
}

function toSearchResult(items: readonly MentionCandidateItem[], normalizedQuery: string): MentionSearchResult {
  const totalMatches = items.length
  return {
    items: items.slice(0, MENTION_CANDIDATE_LIMIT).map(item => toMentionOption(item, normalizedQuery)),
    totalMatches,
    truncated: totalMatches > MENTION_CANDIDATE_LIMIT,
  }
}

function toMentionOption(item: MentionCandidateItem, normalizedQuery: string): MentionOption {
  const langEntry = resolveLanguageForPath(item.path)
  const language = langEntry.canonical !== "plaintext" ? langEntry.canonical : null
  const matchStart = normalizedQuery ? item.path.toLowerCase().indexOf(normalizedQuery) : -1
  return {
    path: item.path,
    name: item.name,
    kind: item.kind,
    language,
    matchRanges: matchStart >= 0
      ? [{ start: matchStart, end: matchStart + normalizedQuery.length }]
      : [],
  }
}
