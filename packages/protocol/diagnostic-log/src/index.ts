/** Diagnostic Log v1：生成类型、事件目录和跨语言运行时校验。 */

import { Buffer } from "node:buffer"
import { diagnosticValidators } from "./validators.generated"
import {
  DIAGNOSTIC_EVENT_LEVELS,
  MAX_DIAGNOSTIC_RECORD_BYTES,
  type DiagnosticEvent,
  type DiagnosticRecord,
} from "./generated"

export * from "./generated"

type Validator = { (value: unknown): boolean; errors?: Array<{ instancePath: string; message?: string }> | null }
const sensitiveKey = /(api[_-]?key|authorization|cookie|credential|password|secret|token)/i
const allowedTokenCountKeys = new Set(["input_tokens", "output_tokens", "cached_input_tokens", "estimated_tokens", "before_estimated_tokens", "after_estimated_tokens"])

/** 校验完整记录，包括 event/level/fields 对应、敏感键、有限数值和 8 KiB 上限。 */
export function assertDiagnosticRecord(value: unknown): asserts value is DiagnosticRecord {
  validate("record", value, "record")
  const record = value as { event: string; level: string; fields: unknown }
  const levels = DIAGNOSTIC_EVENT_LEVELS[record.event as DiagnosticEvent] as readonly string[] | undefined
  if (!levels) throw new Error(`未知 Diagnostic Event：${record.event}`)
  if (!levels.includes(record.level)) throw new Error(`${record.event} 不允许 level=${record.level}`)
  validate(`fields:${record.event}`, record.fields, `${record.event} fields`)
  assertSafeValue(record.fields)
  const bytes = Buffer.byteLength(`${JSON.stringify(value)}\n`, "utf8")
  if (bytes > MAX_DIAGNOSTIC_RECORD_BYTES) throw new Error(`Diagnostic record 超过 ${MAX_DIAGNOSTIC_RECORD_BYTES} bytes`)
}

function validate(key: string, value: unknown, label: string): void {
  const validator = diagnosticValidators[key as keyof typeof diagnosticValidators] as Validator | undefined
  if (!validator) throw new Error(`未知 Diagnostic validator：${key}`)
  if (!validator(value)) {
    const detail = (validator.errors ?? []).map(error => `${error.instancePath || "/"} ${error.message ?? "invalid"}`).join("; ")
    throw new Error(`${label} 不符合 Diagnostic Log v1：${detail}`)
  }
}

function assertSafeValue(value: unknown, key?: string): void {
  if (key && !allowedTokenCountKeys.has(key) && sensitiveKey.test(key)) throw new Error("Diagnostic fields 包含敏感 key")
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Diagnostic fields 包含非有限数值")
  if (typeof value === "string" && /[\u0000-\u001f\u007f]/.test(value)) throw new Error("Diagnostic fields 包含控制字符")
  if (Array.isArray(value)) for (const item of value) assertSafeValue(item)
  else if (value && typeof value === "object") for (const [childKey, child] of Object.entries(value)) assertSafeValue(child, childKey)
}
