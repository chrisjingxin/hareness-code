/** harness logs flat 逐事件视图与树形 cursor 门禁测试。 */

import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { LogsQueryError, queryLogs, renderLogsHuman } from "../../../src/diagnostic-log/query"

function displayWidth(value: string): number {
  let width = 0
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0
    width += code >= 0x1100 && (
      code <= 0x115f
      || (code >= 0x2e80 && code <= 0xa4cf)
      || (code >= 0xac00 && code <= 0xd7a3)
      || (code >= 0xf900 && code <= 0xfaff)
      || (code >= 0xff00 && code <= 0xff60)
    ) ? 2 : 1
  }
  return width
}

async function setup() {
  const root = await mkdtemp(join(tmpdir(), "harness-flat-root-"))
  const cwd = await mkdtemp(join(tmpdir(), "harness-flat-cwd-"))
  const fingerprint = createHash("sha256").update(await realpath(cwd)).digest("hex")
  const process = { pid: 4321, started_at_ms: 1_787_800_000_000, record_sequence: 1 }
  const envelope = {
    schema_version: 1,
    timestamp_ms: 1_787_800_000_001,
    level: "info",
    component: "agent",
    process,
    project_fingerprint: fingerprint,
    thread_id: "thread-flat-view",
    run_id: "run-flat-view",
  }
  const records = [
    { ...envelope, event: "run.started", fields: { mode: "build", resumed: false, approval_mode: "default", model_profile_id: "default" } },
    {
      ...envelope,
      timestamp_ms: 1_787_800_000_002,
      process: { ...process, record_sequence: 2 },
      event: "model.completed",
      fields: {
        model_round: 1,
        provider_attempt: 1,
        duration_ms: 8610,
        provider_first_chunk_ms: 320,
        usage: { input_tokens: 100, output_tokens: 20, cached_input_tokens: 0 },
        finish_reason: "tool_calls",
      },
    },
    {
      ...envelope,
      timestamp_ms: 1_787_800_000_003,
      level: "error",
      process: { ...process, record_sequence: 3 },
      event: "tool.failed",
      fields: {
        tool_name: "github.list_issues",
        tool_kind: "mcp",
        server_name: "github",
        duration_ms: 5000,
        failure_stage: "invoke",
        error_type: "TimeoutError",
        retryable: true,
        summary_code: "TIMEOUT",
      },
    },
  ]
  const directory = join(root, "2026-08-27")
  await mkdir(directory, { recursive: true })
  await writeFile(
    join(directory, "agent-1787800000000-4321-0001.jsonl"),
    `${records.map(record => JSON.stringify(record)).join("\n")}\n`,
  )
  return { root, cwd, fingerprint }
}

test("flat 宽终端逐行显示时间、级别、来源、原始 EVENT、安全详情和耗时", async () => {
  const seeded = await setup()
  try {
    const query = { cwd: seeded.cwd, json: false, flat: true, limit: 200, run: "run-flat" } as const
    const result = await queryLogs(query, seeded.root)
    const output = renderLogsHuman(result, query, 100)

    expect(output).toContain("HARNESS LOGS · FLAT")
    expect(output).toContain("时间")
    expect(output).toContain("级别")
    expect(output).toContain("来源")
    expect(output).toContain("EVENT")
    expect(output).toContain("详情")
    expect(output).toContain("耗时")
    expect(output).toContain("model.completed")
    expect(output).toContain("回合 #1 · 尝试 #1 · 完成")
    expect(output).toContain("tool.failed")
    expect(output).toContain("MCP github.list_issues · 失败 · TIMEOUT")
    expect(output).not.toContain(seeded.fingerprint)
    expect(output.split("\n").every(line => displayWidth(line) <= 100)).toBeTrue()
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("flat 64 列降级为两行记录且不横向溢出", async () => {
  const seeded = await setup()
  try {
    const query = { cwd: seeded.cwd, json: false, flat: true, limit: 200, run: "run-flat" } as const
    const result = await queryLogs(query, seeded.root)
    const output = renderLogsHuman(result, query, 64)

    expect(output).not.toContain("时间          级别")
    expect(output).toContain("model.completed")
    expect(output).toContain("\n  回合 #1 · 尝试 #1 · 完成 · 8.61s")
    expect(output.split("\n").every(line => displayWidth(line) <= 64)).toBeTrue()
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("默认树形截断不生成 cursor，而是给出同选择器的 flat 命令", async () => {
  const seeded = await setup()
  try {
    const query = { cwd: seeded.cwd, json: false, flat: false, limit: 2, run: "run-flat" } as const
    const result = await queryLogs(query, seeded.root)
    const output = renderLogsHuman(result, query, 100)

    expect(result.truncated).toBeTrue()
    expect(result.next_cursor).toBeNull()
    expect(output).not.toContain("下一页")
    expect(output).toContain("harness logs --run run-flat-view --flat --limit 2")
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("query 层拒绝 tree cursor 与 flat/json 冲突", async () => {
  const seeded = await setup()
  try {
    await expect(queryLogs({
      cwd: seeded.cwd,
      json: false,
      flat: false,
      limit: 2,
      run: "run-flat",
      cursor: "opaque",
    }, seeded.root)).rejects.toEqual(expect.objectContaining({
      name: LogsQueryError.name,
      code: "CURSOR_REQUIRES_FLAT_OR_JSON",
    }))
    await expect(queryLogs({
      cwd: seeded.cwd,
      json: true,
      flat: true,
      limit: 2,
      run: "run-flat",
    }, seeded.root)).rejects.toMatchObject({ code: "FLAT_JSON_CONFLICT" })
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})
