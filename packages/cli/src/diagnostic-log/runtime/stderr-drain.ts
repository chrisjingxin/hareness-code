/** Sidecar stderr 有界 drain：只保留计数，不累积原文。 */

const MAX_STDERR_BYTES = 256 * 1024
const MAX_STDERR_LINES = 10_000

export type SidecarStderrSnapshot = {
  bytes: number
  lines: number
  truncated: boolean
}

/** 持续消费 sidecar stderr，内存占用不随输出增长。 */
export class SidecarStderrDrain {
  private bytes = 0
  private lines = 0
  private truncated = false

  push(chunk: Buffer | string): void {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    const nextBytes = this.bytes + value.byteLength
    const nextLines = this.lines + value.reduce((count, byte) => count + Number(byte === 10), 0)
    this.truncated ||= nextBytes > MAX_STDERR_BYTES || nextLines > MAX_STDERR_LINES
    this.bytes = Math.min(nextBytes, MAX_STDERR_BYTES)
    this.lines = Math.min(nextLines, MAX_STDERR_LINES)
  }

  snapshot(): SidecarStderrSnapshot {
    return { bytes: this.bytes, lines: this.lines, truncated: this.truncated }
  }
}
