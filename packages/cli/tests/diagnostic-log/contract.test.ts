/** Diagnostic Log v1 的 TypeScript/Python 共享契约测试。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { assertDiagnosticQueryResult, assertDiagnosticRecord } from "@za38/protocol/diagnostic-log"

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

test("MCP、压缩和过程章节使用可读名字与显式语义字段", () => {
  expect(() => assertDiagnosticRecord(eventRecord("mcp.connection.completed", {
    server_fingerprint: "b".repeat(64),
    transport: "http",
    duration_ms: 10,
    tool_count: 2,
  }))).toThrow()
  expect(() => assertDiagnosticRecord(eventRecord("context.compaction.completed", {
    duration_ms: 10,
    before_estimated_tokens: 1200,
    after_estimated_tokens: 600,
    artifact_count: 1,
  }))).toThrow()

  for (const [event, fields] of [
    ["context.compaction.failed", {
      trigger: "auto",
      action: "auto_failed",
      duration_ms: 10,
      failure_stage: "compress",
      error_type: "RuntimeError",
      summary_code: "COMPACTION_FAILED",
    }],
    ["catalog.bound", { skill_count: 1, mcp_count: 1, plugin_count: 1 }],
    ["skill.read", { skill_id: "tdd", kind: "body" }],
    ["hook.started", { plugin_id: "demo", hook_event: "PreToolUse", tool_name: "shell" }],
    ["execution.started", { kind: "child", agent_id: "explore" }],
  ] as const) {
    expect(() => assertDiagnosticRecord(eventRecord(event, fields))).not.toThrow()
  }
})

test("query v1 区分 Thread 选择器并限制 Run 摘要形状", () => {
  const query = {
    query_schema_version: 1,
    project_fingerprint: "a".repeat(64),
    thread_id: "thread-1",
    filters: { minimum_level: "info", event: null, component: null },
    summary: {
      run_count: 1,
      outcome: "completed",
      runs: [{ run_id: "run-1", outcome: "completed", duration_ms: 10 }],
    },
    events: [],
    matched_count: 0,
    returned_count: 0,
    truncated: false,
    next_cursor: null,
    diagnostics: {
      malformed_count: 0,
      invalid_count: 0,
      unsupported_schema_count: 0,
      partial_line_count: 0,
      orphan_active_count: 0,
      orphan_active_bytes: 0,
    },
  }

  expect(() => assertDiagnosticQueryResult(query)).not.toThrow()
  expect(() => assertDiagnosticQueryResult({
    ...query,
    run_id: "run-1",
  })).toThrow()
  expect(() => assertDiagnosticQueryResult({
    ...query,
    summary: { ...query.summary, runs: Array.from({ length: 1001 }, (_, index) => ({ run_id: `run-${index}` })) },
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

function eventRecord(event: string, fields: Record<string, unknown>): Record<string, unknown> {
  return {
    ...minimalRecord(),
    level: event.endsWith(".failed") ? "error" : "info",
    event,
    fields,
  }
}
