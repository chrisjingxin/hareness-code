/** 本地诊断日志：仅写入调用方提供的白名单字段，不记录业务载荷与凭据。 */

import { chmodSync, closeSync, mkdirSync, openSync, writeSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

export type DiagnosticFields = Record<string, string | number | boolean | null | undefined>

export type DiagnosticLogger = {
  readonly filePath: string | null
  readonly isDebug: boolean
  info(event: string, fields?: DiagnosticFields): void
  debug(event: string, fields?: DiagnosticFields): void
  error(event: string, fields?: DiagnosticFields, debugFields?: DiagnosticFields): void
  close(): void
}

export type DiagnosticLoggerOptions = {
  directory?: string
  level?: string
  now?: () => Date
  pid?: number
}

const noopLogger: DiagnosticLogger = {
  filePath: null,
  isDebug: false,
  info: () => undefined,
  debug: () => undefined,
  error: () => undefined,
  close: () => undefined,
}

/**
 * 创建本次 CLI 进程的 JSONL 日志。目录或文件不可写时返回 no-op logger，
 * 诊断能力不能反向阻断 Agent 启动。
 */
export function createDiagnosticLogger(options: DiagnosticLoggerOptions = {}): DiagnosticLogger {
  const now = options.now ?? (() => new Date())
  const isDebug = (options.level ?? process.env.HARNESS_LOG_LEVEL)?.toLowerCase() === "debug"
  const directory = options.directory ?? join(homedir(), ".harness", "debug")
  const pid = options.pid ?? process.pid
  let descriptor: number
  let filePath: string
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    chmodSync(directory, 0o700)
    const timestamp = now().toISOString().replaceAll(":", "-")
    filePath = join(directory, `harness-${timestamp}-${pid}.jsonl`)
    descriptor = openSync(filePath, "a", 0o600)
    chmodSync(filePath, 0o600)
  } catch {
    return noopLogger
  }

  let closed = false
  const write = (level: "info" | "debug" | "error", event: string, fields: DiagnosticFields = {}) => {
    if (closed || (level === "debug" && !isDebug)) return
    const entry = {
      timestamp: now().toISOString(),
      level,
      event: sanitizeText(event, 80),
      ...sanitizeFields(fields),
    }
    try {
      writeSync(descriptor, `${JSON.stringify(entry)}\n`, undefined, "utf8")
    } catch {
      // 日志写失败不能影响 CLI 生命周期。
    }
  }

  return {
    filePath,
    isDebug,
    info: (event, fields) => write("info", event, fields),
    debug: (event, fields) => write("debug", event, fields),
    error: (event, fields, debugFields) => write(
      "error",
      event,
      isDebug ? { ...fields, ...debugFields } : fields,
    ),
    close: () => {
      if (closed) return
      closed = true
      try {
        closeSync(descriptor)
      } catch {
        // descriptor 可能已因外部原因失效。
      }
    },
  }
}

function sanitizeFields(fields: DiagnosticFields): DiagnosticFields {
  return Object.fromEntries(Object.entries(fields).flatMap(([key, value]) => {
    if (value === undefined) return []
    const safeKey = key.replaceAll(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 64)
    if (!safeKey) return []
    return [[safeKey, typeof value === "string" ? sanitizeText(value, 240) : value]]
  }))
}

function sanitizeText(value: string, maxLength: number): string {
  const singleLine = value.replaceAll(/[\r\n\t]/g, " ")
  return singleLine.length > maxLength ? `${singleLine.slice(0, maxLength)}…` : singleLine
}
