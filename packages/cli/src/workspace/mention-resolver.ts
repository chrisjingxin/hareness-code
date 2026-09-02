/**
 * 工作区提及解析器：解析消息中的 @ 文件提及，实施 128k 保守门禁，读取内容/切片或降级占位。
 */

import { stat, readFile } from "node:fs/promises"
import { parseMentionsFromText, type ParsedMention } from "../presentation-shared/mention-parse-policy"
import { resolveLanguageForPath } from "../presentation-shared/language-catalog"
import { resolveWithinRoot, resolveWorkspaceRoot } from "./path-policy"
import { decodeWorkspaceText, WorkspaceTextError } from "./text-file-policy"

/** 单文件最大内联大小：10KB */
export const MAX_SINGLE_FILE_BYTES = 10 * 1024

/** 单文件最大内联行数：300 行 */
export const MAX_SINGLE_FILE_LINES = 300

/** 单轮全部提及累计最大内联预算：20KB */
export const MAX_TOTAL_MENTIONS_BYTES = 20 * 1024

/** 单轮全部提及累计最大内联行数：500 行 */
export const MAX_TOTAL_MENTIONS_LINES = 500

/** 显式行号切片允许扫描的最大源文件：防止为少量行整体读入无界大文件。 */
export const MAX_MENTION_SLICE_SOURCE_BYTES = 16 * 1024 * 1024

/** 自动降级为占位符的黑名单扩展名/文件名特征 */
const BLACKLIST_PATTERNS = [
  /\.lock$/i,
  /\.lockb$/i,
  /(?:^|[\\/])(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|cargo\.lock|go\.sum|bun\.lock|bun\.lockb)$/i,
  /\.min\.(js|cjs|mjs|css)$/i,
  /\.map$/i,
  /\.(png|jpg|jpeg|gif|webp|ico|svg)$/i,
  /\.(woff|woff2|ttf|eot)$/i,
  /\.(wasm|bin|tar|gz|zip|7z|pdf|exe|dll|so|dylib)$/i,
]

export type ResolvedMention =
  | {
      readonly kind: "inlined"
      readonly raw: string
      readonly path: string
      readonly content: string
      readonly lineStart?: number
      readonly lineEnd?: number
      readonly totalLines: number
      readonly language: string
      readonly bytes: number
    }
  | {
      readonly kind: "reference"
      readonly raw: string
      readonly path: string
      readonly reason: "too-large" | "blacklisted" | "binary" | "budget-exceeded"
      readonly sizeBytes: number
    }

export type MentionResolutionResult = {
  readonly resolved: readonly ResolvedMention[]
  readonly inlinedCount: number
  readonly referenceCount: number
  readonly totalBytes: number
  /** 生成的附加上下文 Prompt 片段；若无有效 mention 则为空字符串 */
  readonly contextBlock: string
  /** 拼装附加上下文后的最终提交 Prompt */
  readonly prompt: string
}

function isBlacklisted(path: string): boolean {
  const lower = path.toLowerCase()
  return BLACKLIST_PATTERNS.some(pattern => pattern.test(lower))
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  const kb = (bytes / 1024).toFixed(1)
  return `${kb}KB`
}

/** 选择严格长于正文中最长反引号序列的围栏，避免 Markdown 文件提前闭合附件块。 */
function codeFenceForContent(content: string): string {
  const longestRun = Math.max(0, ...(content.match(/`+/g) ?? []).map(run => run.length))
  return "`".repeat(Math.max(3, longestRun + 1))
}

/**
 * 解析用户消息文本中的全部 @ 提及，安全读取工作区文件，应用 10KB/20KB 保守预算门禁。
 */
export async function resolveMentions(
  workspaceRoot: string,
  text: string,
): Promise<MentionResolutionResult> {
  const mentions = parseMentionsFromText(text)
  if (mentions.length === 0) {
    return {
      resolved: [],
      inlinedCount: 0,
      referenceCount: 0,
      totalBytes: 0,
      contextBlock: "",
      prompt: text,
    }
  }

  const resolved: ResolvedMention[] = []
  let cumulativeBytes = 0
  let cumulativeLines = 0

  // 记录已处理过的路径+切片，避免重复内联
  const processedKeys = new Set<string>()

  let realRoot = workspaceRoot
  try {
    realRoot = await resolveWorkspaceRoot(workspaceRoot)
  } catch {
    realRoot = workspaceRoot
  }

  for (const mention of mentions) {
    const key = `${mention.path}#${mention.lineStart ?? ""}-${mention.lineEnd ?? ""}`
    if (processedKeys.has(key)) continue
    processedKeys.add(key)

    // 1. 工作区路径安全校验
    let targetPath: string
    try {
      targetPath = await resolveWithinRoot(realRoot, mention.path)
    } catch {
      // 路径非法（如越界 ../）或不存在，静默跳过
      continue
    }

    // 2. 文件元数据检查
    let fileStat
    try {
      fileStat = await stat(targetPath)
    } catch {
      continue
    }

    if (!fileStat.isFile()) continue

    // 3. 黑名单过滤
    if (isBlacklisted(mention.path)) {
      resolved.push({
        kind: "reference",
        raw: mention.raw,
        path: mention.path,
        reason: "blacklisted",
        sizeBytes: fileStat.size,
      })
      continue
    }

    // 4. 未指定切片时可直接按 stat 执行 10KB 边界，无需读取大文件。
    if (fileStat.size >= MAX_SINGLE_FILE_BYTES && mention.lineStart === undefined) {
      resolved.push({
        kind: "reference",
        raw: mention.raw,
        path: mention.path,
        reason: "too-large",
        sizeBytes: fileStat.size,
      })
      continue
    }

    // 显式行号切片仍需读取源文件；先以固定上限阻断无界内存读取。
    if (fileStat.size > MAX_MENTION_SLICE_SOURCE_BYTES) {
      resolved.push({
        kind: "reference",
        raw: mention.raw,
        path: mention.path,
        reason: "too-large",
        sizeBytes: fileStat.size,
      })
      continue
    }

    // 5. 读取文件内容
    let fileContent: string
    try {
      const buffer = await readFile(targetPath)
      fileContent = decodeWorkspaceText(buffer)
    } catch (error) {
      const reason = error instanceof WorkspaceTextError ? "binary" : "too-large"
      resolved.push({
        kind: "reference",
        raw: mention.raw,
        path: mention.path,
        reason,
        sizeBytes: fileStat.size,
      })
      continue
    }

    const allLines = fileContent.split(/\r?\n/)
    const totalLines = allLines.length
    const langEntry = resolveLanguageForPath(mention.path)
    const language = langEntry.canonical

    // 6. 行号切片处理
    if (mention.lineStart !== undefined && mention.lineEnd !== undefined) {
      const clampedStart = Math.max(1, Math.min(mention.lineStart, totalLines))
      const clampedEnd = Math.max(clampedStart, Math.min(mention.lineEnd, totalLines))
      const sliceLines = allLines.slice(clampedStart - 1, clampedEnd)
      const sliceContent = sliceLines.join("\n")
      const sliceBytes = Buffer.byteLength(sliceContent, "utf8")
      const sliceLineCount = sliceLines.length

      if (sliceBytes > MAX_SINGLE_FILE_BYTES || sliceLineCount > MAX_SINGLE_FILE_LINES) {
        resolved.push({
          kind: "reference",
          raw: mention.raw,
          path: mention.path,
          reason: "too-large",
          sizeBytes: sliceBytes,
        })
        continue
      }

      if (
        cumulativeBytes + sliceBytes > MAX_TOTAL_MENTIONS_BYTES ||
        cumulativeLines + sliceLineCount > MAX_TOTAL_MENTIONS_LINES
      ) {
        resolved.push({
          kind: "reference",
          raw: mention.raw,
          path: mention.path,
          reason: "budget-exceeded",
          sizeBytes: sliceBytes,
        })
        continue
      }

      cumulativeBytes += sliceBytes
      cumulativeLines += sliceLineCount

      resolved.push({
        kind: "inlined",
        raw: mention.raw,
        path: mention.path,
        content: sliceContent,
        lineStart: clampedStart,
        lineEnd: clampedEnd,
        totalLines,
        language,
        bytes: sliceBytes,
      })
    } else {
      // 全文处理
      const fileBytes = Buffer.byteLength(fileContent, "utf8")

      if (fileBytes >= MAX_SINGLE_FILE_BYTES || totalLines >= MAX_SINGLE_FILE_LINES) {
        resolved.push({
          kind: "reference",
          raw: mention.raw,
          path: mention.path,
          reason: "too-large",
          sizeBytes: fileBytes,
        })
        continue
      }

      if (
        cumulativeBytes + fileBytes > MAX_TOTAL_MENTIONS_BYTES ||
        cumulativeLines + totalLines > MAX_TOTAL_MENTIONS_LINES
      ) {
        resolved.push({
          kind: "reference",
          raw: mention.raw,
          path: mention.path,
          reason: "budget-exceeded",
          sizeBytes: fileBytes,
        })
        continue
      }

      cumulativeBytes += fileBytes
      cumulativeLines += totalLines

      resolved.push({
        kind: "inlined",
        raw: mention.raw,
        path: mention.path,
        content: fileContent,
        totalLines,
        language,
        bytes: fileBytes,
      })
    }
  }

  if (resolved.length === 0) {
    return {
      resolved: [],
      inlinedCount: 0,
      referenceCount: 0,
      totalBytes: 0,
      contextBlock: "",
      prompt: text,
    }
  }

  // 7. 生成结构化附加上下文代码块
  const blocks: string[] = []
  let inlinedCount = 0
  let referenceCount = 0

  for (const item of resolved) {
    if (item.kind === "inlined") {
      inlinedCount++
      const header = item.lineStart !== undefined && item.lineEnd !== undefined
        ? `[Attached Context: ${item.path} (lines ${item.lineStart}-${item.lineEnd} of ${item.totalLines})]`
        : `[Attached Context: ${item.path} (${item.totalLines} lines, ${formatBytes(item.bytes)})]`
      const fence = codeFenceForContent(item.content)
      blocks.push(`${header}\n${fence}${item.language}\n${item.content}\n${fence}`)
    } else {
      referenceCount++
      blocks.push(
        `[Mentioned File: ${item.path} (Size: ${formatBytes(item.sizeBytes)}, too large to inline. Use read_file to inspect)]`,
      )
    }
  }

  const contextBlock = blocks.join("\n\n")
  const prompt = contextBlock ? `${contextBlock}\n\n${text}` : text

  return {
    resolved,
    inlinedCount,
    referenceCount,
    totalBytes: cumulativeBytes,
    contextBlock,
    prompt,
  }
}
