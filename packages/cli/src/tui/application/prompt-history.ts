/** 提示词历史纯逻辑与 JSONL 持久化：不依赖 React，便于 CLI 与测试复用。 */

import { appendFile, mkdir, readFile, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

export const PROMPT_HISTORY_LIMIT = 50

export type PromptHistoryCursor = {
  /** 指向当前回填项；history.length 表示位于最新记录之后的空草稿。 */
  index: number
}

export type PromptHistoryMove = {
  value: string
  cursor: PromptHistoryCursor
}

/**
 * 使用与 OpenCode 相同的 JSONL 组织思路，但只保存本项目真正支持的纯文本提示词。
 * 保留连续去重能避免因连按 Enter 写入相邻重复项，同时不重排较早的历史记录。
 */
export function rememberPrompt(history: readonly string[], prompt: string): string[] {
  const normalized = prompt.trim()
  if (!normalized) return [...history]
  if (history.at(-1) === normalized) return [...history]
  return [...history, normalized].slice(-PROMPT_HISTORY_LIMIT)
}

/** 允许测试或嵌入式运行时注入根目录，正常 CLI 固定使用 ~/.harness。 */
export function promptHistoryPath(home = homedir()): string {
  return join(home, ".harness", "prompt-history.jsonl")
}

/**
 * 读取时跳过损坏行、兼容早期字符串格式，并仅保留最近 50 条。
 * 随后写回规范 JSONL，保证一次意外中断不会让后续启动无法读取历史。
 */
export function parsePromptHistory(text: string): string[] {
  const history: string[] = []
  for (const line of text.split("\n")) {
    if (!line.trim()) continue
    const value = parsePromptHistoryLine(line)
    if (!value || history.at(-1) === value) continue
    history.push(value)
  }
  return history.slice(-PROMPT_HISTORY_LIMIT)
}

/** 加载并自愈本地 JSONL 历史；不可读时安全降级为空历史。 */
export async function loadPromptHistory(path = promptHistoryPath()): Promise<string[]> {
  let text: string
  try {
    text = await readFile(path, "utf8")
  } catch {
    return []
  }
  const history = parsePromptHistory(text)
  const normalized = serializePromptHistory(history)
  if (text !== normalized) {
    try {
      await mkdir(dirname(path), { recursive: true })
      await writeFile(path, normalized, "utf8")
    } catch {
      // 历史记录是便利能力；只读家目录或磁盘失败不能阻塞 Agent thread。
    }
  }
  return history
}

/**
 * 未触发裁剪时只追加最后一行，达到上限后再整体重写，减少普通交互的磁盘写入。
 * 调用方先更新内存状态，持久化异常可以安全忽略，不会影响本次 thread。
 */
export async function persistPromptHistory(
  previous: readonly string[],
  next: readonly string[],
  path = promptHistoryPath(),
): Promise<void> {
  if (sameHistory(previous, next)) return
  try {
    await mkdir(dirname(path), { recursive: true })
    const appended = next.length === previous.length + 1
      && previous.every((value, index) => next[index] === value)
    if (appended) {
      const latest = next.at(-1)
      if (latest) await appendFile(path, `${JSON.stringify({ input: latest })}\n`, "utf8")
      return
    }
    await writeFile(path, serializePromptHistory(next), "utf8")
  } catch {
    // 不把本地历史写入失败显示为 Agent 错误，避免泄漏用户路径和系统细节。
  }
}

/**
 * 仅在空 composer 或当前内容正好来自历史记录时接管方向键。
 * 用户手动修改后的多行提示词仍交给 textarea 移动光标，避免破坏正常编辑。
 */
export function canNavigatePromptHistory(history: readonly string[], draft: string): boolean {
  return history.length > 0 && (draft.length === 0 || history.lastIndexOf(draft) >= 0)
}

/** 返回应回填的提示词；向下越过最新条目时返回空字符串。 */
export function selectPromptHistory(
  history: readonly string[],
  draft: string,
  direction: "previous" | "next",
): string | undefined {
  if (!canNavigatePromptHistory(history, draft)) return undefined
  const current = draft.length === 0 ? history.length : history.lastIndexOf(draft)
  const target = direction === "previous"
    ? Math.max(0, current - 1)
    : Math.min(history.length, current + 1)
  return target === history.length ? "" : history[target]
}

/**
 * 为实际 textarea 维护独立游标。不能只由 draft 反推位置：当用户从最新提示词
 * 向下回到空草稿后，仍需知道下一次 ↓ 已经越界，才能把按键交还 thread 滚动。
 */
export function movePromptHistory(
  history: readonly string[],
  draft: string,
  cursor: PromptHistoryCursor | undefined,
  direction: "previous" | "next",
): PromptHistoryMove | undefined {
  if (!history.length) return undefined
  const current = cursor?.index ?? (draft.length === 0 ? history.length : history.lastIndexOf(draft))
  if (current < 0) return undefined
  const target = direction === "previous"
    ? Math.max(0, current - 1)
    : Math.min(history.length, current + 1)
  if (target === current) return undefined
  return {
    value: target === history.length ? "" : history[target] ?? "",
    cursor: { index: target },
  }
}

/** 兼容字符串和对象两种历史行格式，并忽略损坏 JSON。 */
function parsePromptHistoryLine(line: string): string | undefined {
  try {
    const parsed: unknown = JSON.parse(line)
    const input = typeof parsed === "string"
      ? parsed
      : parsed && typeof parsed === "object" && typeof (parsed as { input?: unknown }).input === "string"
        ? (parsed as { input: string }).input
        : undefined
    const normalized = input?.trim()
    return normalized || undefined
  } catch {
    return undefined
  }
}

/** 将历史集合编码为每行一个对象的规范 JSONL 文本。 */
function serializePromptHistory(history: readonly string[]): string {
  return history.length ? `${history.map(input => JSON.stringify({ input })).join("\n")}\n` : ""
}

/** 比较两个历史集合是否完全相同，避免重复磁盘写入。 */
function sameHistory(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}
