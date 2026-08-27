/** Diagnostic Log v1 的 TypeScript/Python 共享契约测试。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { assertDiagnosticRecord } from "@za38/protocol/diagnostic-log"

type Fixture = { valid: boolean; value: unknown }

const fixtures = JSON.parse(
  await readFile(resolve(import.meta.dir, "../../../protocol/diagnostic-log/fixtures/v1-contract.json"), "utf8"),
) as Fixture[]

test("TypeScript 与 Python 共享 Diagnostic Log v1 正反 fixture", () => {
  for (const fixture of fixtures) {
    if (fixture.valid) expect(() => assertDiagnosticRecord(fixture.value)).not.toThrow()
    else expect(() => assertDiagnosticRecord(fixture.value)).toThrow()
  }
})

test("拒绝 level/event 不匹配、非有限数值、敏感 key 和超过 8 KiB 的记录", () => {
  const record = minimalRecord()
  expect(() => assertDiagnosticRecord({ ...record, level: "debug" })).toThrow()
  expect(() => assertDiagnosticRecord({ ...record, fields: { duration_ms: Number.NaN } })).toThrow()
  expect(() => assertDiagnosticRecord({ ...record, fields: { token: "secret" } })).toThrow()
  expect(() => assertDiagnosticRecord({
    ...record,
    fields: { command_kind: "x".repeat(9_000), runtime_version: "bun", platform: "darwin", arch: "arm64" },
  })).toThrow()
})

function minimalRecord(): Record<string, unknown> {
  return {
    schema_version: 1,
    timestamp_ms: 1,
    level: "info",
    event: "process.started",
    component: "cli",
    process: { pid: 1, started_at_ms: 1, record_sequence: 1 },
    project_fingerprint: "a".repeat(64),
    fields: { command_kind: "run", runtime_version: "bun", platform: "darwin", arch: "arm64" },
  }
}
