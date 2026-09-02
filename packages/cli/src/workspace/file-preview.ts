/**
 * 文件预览读取：文本识别、容量/行数/行宽截断、变化检测与 LRU 缓存。
 *
 * 二进制（前 8 KiB 含 NUL）与非 UTF-8 一律标记 unsupported，不渲染错误页；
 * 读取中文件变化（mtime/size）自动重试一次，仍变化收敛为 workspace-changed。
 */

import { open, stat } from "node:fs/promises"
import type { Stats } from "node:fs"
import path from "node:path"

import type { WorkspaceFilePreview } from "./types"
import { WorkspaceError, mapFsError, resolveWithinRoot, workspaceError } from "./path-policy"
import { fileLanguageId } from "./file-language"
import { decodeWorkspaceText, WorkspaceTextError } from "./text-file-policy"

export const MAX_PREVIEW_BYTES = 256 * 1024
export const MAX_PREVIEW_LINES = 2_000
export const MAX_LINE_CHARS = 20_000
export const PREVIEW_CACHE_LIMIT = 16
export const PREVIEW_CACHE_BYTES = 2 * 1024 * 1024

/**
 * 读取单个文件的预览。目录返回 not-file；二进制/非 UTF-8 返回 unsupported；
 * 读取期间文件变化自动重试一次，仍变化抛 workspace-changed。
 */
export async function readPreview(root: string, relativePath: string): Promise<WorkspaceFilePreview> {
  const target = await resolveWithinRoot(root, relativePath)
  let before: Stats
  try {
    before = await stat(target)
  } catch (error) {
    throw mapFsError(error)
  }
  if (before.isDirectory()) throw workspaceError("not-file", "目录不能预览")

  const preview = await readPreviewOnce(target, relativePath, before)
  const after = await statAfterRead(target)
  if (after.mtimeMs !== before.mtimeMs || after.size !== before.size) {
    // 读取期间文件变化：自动重试一次；重试后仍变化视为工作区正在写入。
    const retried = await readPreviewOnce(target, relativePath, after)
    const final = await statAfterRead(target)
    if (final.mtimeMs !== after.mtimeMs || final.size !== after.size) {
      throw workspaceError("workspace-changed", "文件已变化，请重新刷新")
    }
    return retried
  }
  return preview
}

async function statAfterRead(target: string): Promise<Stats> {
  try {
    return await stat(target)
  } catch (error) {
    throw mapFsError(error)
  }
}

/** 单次读取：限额读取 → 二进制识别 → UTF-8 解码 → 行/行宽截断。 */
async function readPreviewOnce(target: string, relativePath: string, fileStat: Stats): Promise<WorkspaceFilePreview> {
  const sizeBytes = fileStat.size
  const readBytes = Math.min(sizeBytes, MAX_PREVIEW_BYTES)
  const truncatedByBytes = readBytes < sizeBytes
  const handle = await open(target, "r")
  try {
    const buffer = Buffer.alloc(readBytes)
    const { bytesRead } = await handle.read(buffer, 0, readBytes, 0)
    // 只处理实际读到的字节：stat 与 read 之间文件缩小时尾部零填充不得进入探测窗口。
    const effective = buffer.subarray(0, bytesRead)

    let text: string
    try {
      text = decodeWorkspaceText(effective, truncatedByBytes)
    } catch (error) {
      if (error instanceof WorkspaceTextError && error.reason === "binary") {
        throw unsupportedFileError(sizeBytes)
      }
      throw workspaceError("unsupported-encoding", "非 UTF-8 文本暂不支持预览")
    }

    const { content, truncatedByLines } = truncateContent(text)
    return {
      path: relativePath,
      name: path.basename(relativePath),
      content,
      language: fileLanguageId(relativePath),
      sizeBytes,
      lineCount: countLines(content),
      modifiedAtMs: fileStat.mtimeMs,
      truncated: truncatedByBytes || truncatedByLines,
      version: `${fileStat.mtimeMs}:${fileStat.size}`,
    }
  } finally {
    await handle.close()
  }
}

/** 行数（2000）与单行宽度（20000 字符）截断；返回是否发生了行级截断。 */
function truncateContent(text: string): { content: string; truncatedByLines: boolean } {
  const lines = text.split("\n")
  let content = text
  let truncated = false
  if (lines.length > MAX_PREVIEW_LINES) {
    content = lines.slice(0, MAX_PREVIEW_LINES).join("\n")
    truncated = true
  }
  if (content.length > 0) {
    const linesAfter = content.split("\n")
    if (linesAfter.some(line => line.length > MAX_LINE_CHARS)) {
      content = linesAfter.map(line => (line.length > MAX_LINE_CHARS ? line.slice(0, MAX_LINE_CHARS) : line)).join("\n")
      truncated = true
    }
  }
  return { content, truncatedByLines: truncated }
}

/** 展示行数：空内容 0；结尾换行不产生额外行号（与 <pre> 渲染一致）。 */
function countLines(content: string): number {
  if (content === "") return 0
  const lines = content.split("\n")
  return content.endsWith("\n") ? lines.length - 1 : lines.length
}

/** unsupported-file 需要携带 sizeBytes 供界面展示元信息；挂在错误上透传给 explorer。 */
function unsupportedFileError(sizeBytes: number): WorkspaceError {
  const error = workspaceError("unsupported-file", "二进制文件暂不支持预览")
  ;(error as WorkspaceError & { sizeBytes: number }).sizeBytes = sizeBytes
  return error
}

/** 预览 LRU 缓存：Map 插入序即淘汰序，key = path + NUL + version。 */
export class PreviewCache {
  private readonly cache = new Map<string, WorkspaceFilePreview>()
  /** path → 最近一次插入版本的缓存 key；get(path) 直接返回该版本。 */
  private readonly latestByPath = new Map<string, string>()
  private bytes = 0

  /** 返回该路径最近缓存的预览；命中并把条目刷新到最末（LRU）。 */
  get(filePath: string): WorkspaceFilePreview | undefined {
    const key = this.latestByPath.get(filePath)
    if (key === undefined) return undefined
    const entry = this.cache.get(key)
    if (!entry) return undefined
    this.cache.delete(key)
    this.cache.set(key, entry)
    return entry
  }

  put(filePath: string, version: string, preview: WorkspaceFilePreview): void {
    const key = this.key(filePath, version)
    const existing = this.cache.get(key)
    if (existing) this.bytes -= contentBytes(existing)
    this.cache.set(key, preview)
    this.bytes += contentBytes(preview)
    this.latestByPath.set(filePath, key)
    this.evict()
  }

  /** 使某路径的全部版本失效（refresh-preview 前调用）。 */
  invalidate(filePath: string): void {
    for (const key of [...this.cache.keys()]) {
      if (key.startsWith(`${filePath}\u0000`)) {
        const entry = this.cache.get(key)
        if (entry) this.bytes -= contentBytes(entry)
        this.cache.delete(key)
      }
    }
    this.latestByPath.delete(filePath)
  }

  clear(): void {
    this.cache.clear()
    this.bytes = 0
  }

  get size(): number {
    return this.cache.size
  }

  private key(filePath: string, version: string): string {
    // NUL 分隔：POSIX 路径不允许 NUL，杜绝 path 含 `:` 时与 version 前缀混淆。
    return `${filePath}\u0000${version}`
  }

  /** 容量淘汰：条目数或内容总字节超限时逐出最旧条目；至少保留一条避免空转。 */
  private evict(): void {
    while (this.cache.size > PREVIEW_CACHE_LIMIT || (this.cache.size > 1 && this.bytes > PREVIEW_CACHE_BYTES)) {
      const oldestKey = this.cache.keys().next().value
      if (oldestKey === undefined) break
      const entry = this.cache.get(oldestKey)
      if (entry) this.bytes -= contentBytes(entry)
      this.cache.delete(oldestKey)
    }
  }
}

/** 内容字节近似（UTF-16 code unit × 2），用于缓存字节预算。 */
function contentBytes(preview: WorkspaceFilePreview): number {
  return preview.content.length * 2
}
