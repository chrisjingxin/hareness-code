/**
 * 工作区文本判定策略：统一二进制探测与严格 UTF-8 解码，供预览和提及注入复用。
 */

/** 二进制探测窗口：前 8 KiB。 */
const BINARY_PROBE_BYTES = 8 * 1024
/** UTF-8 多字节序列最长 4 字节；截断读取最多需要丢 3 个尾字节。 */
const MAX_TRAILING_DROP = 3

const decoder = new TextDecoder("utf-8", { fatal: true })

/** 文本内容不适合读取或内联时使用的稳定内部错误。 */
export class WorkspaceTextError extends Error {
  readonly reason: "binary" | "unsupported-encoding"

  /** 保存机器可判定的拒绝原因，不携带文件内容或绝对路径。 */
  constructor(reason: "binary" | "unsupported-encoding") {
    super(reason)
    this.name = "WorkspaceTextError"
    this.reason = reason
  }
}

/**
 * 判定字节内容是否为可安全内联的 UTF-8 文本；截断读取允许丢弃不完整的尾部多字节序列。
 */
export function decodeWorkspaceText(buffer: Uint8Array, truncated = false): string {
  const probe = buffer.subarray(0, Math.min(BINARY_PROBE_BYTES, buffer.length))
  if (probe.includes(0)) throw new WorkspaceTextError("binary")

  if (!truncated) {
    try {
      return decoder.decode(buffer)
    } catch {
      throw new WorkspaceTextError("unsupported-encoding")
    }
  }

  for (let drop = 0; drop <= MAX_TRAILING_DROP; drop += 1) {
    try {
      return decoder.decode(buffer.subarray(0, buffer.length - drop))
    } catch {
      // 继续尝试更短的尾段。
    }
  }
  throw new WorkspaceTextError("unsupported-encoding")
}
