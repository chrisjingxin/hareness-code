/** harness logs 的双上限与 snapshot cursor 回归测试。 */

import { expect, test } from "bun:test"
import { appendFile, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { assertDiagnosticQueryResult } from "@za38/protocol/diagnostic-log"

import { queryLogs, renderLogsHuman } from "../../../src/diagnostic-log/query"

const MAX_STDOUT_BYTES = 256 * 1024

function record(
  fingerprint: string,
  runId: string,
  sequence: number,
  overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: 1,
    timestamp_ms: 1_787_800_000_000 + sequence,
    level: "info",
    event: "run.started",
    component: "agent",
    process: {
      pid: 4321,
      started_at_ms: 1_787_800_000_000,
      record_sequence: sequence,
    },
    project_fingerprint: fingerprint,
    thread_id: "thread-pagination",
    run_id: runId,
    fields: {
      mode: "build",
      resumed: false,
      approval_mode: "default",
      model_profile_id: "default",
    },
    ...overrides,
  }
}

async function setup(): Promise<{ root: string; cwd: string; fingerprint: string; file: string }> {
  const root = await mkdtemp(join(tmpdir(), "harness-pagination-root-"))
  const cwd = await mkdtemp(join(tmpdir(), "harness-pagination-cwd-"))
  const fingerprint = createHash("sha256").update(await realpath(cwd)).digest("hex")
  const directory = join(root, "2026-08-27")
  await mkdir(directory, { recursive: true })
  return {
    root,
    cwd,
    fingerprint,
    file: join(directory, "agent-1787800000000-4321-0001.jsonl"),
  }
}

async function cleanup(value: { root: string; cwd: string }): Promise<void> {
  await rm(value.root, { recursive: true, force: true })
  await rm(value.cwd, { recursive: true, force: true })
}

test("条数上限覆盖 199/200/201/1000/1001，且只减少完整 events", async () => {
  const seeded = await setup()
  try {
    const counts = [199, 200, 201, 1000, 1001]
    const lines: string[] = []
    let sequence = 1
    for (const count of counts) {
      for (let index = 0; index < count; index += 1) {
        lines.push(JSON.stringify(record(seeded.fingerprint, `run-${count}`, sequence)))
        sequence += 1
      }
    }
    await writeFile(seeded.file, `${lines.join("\n")}\n`)

    for (const count of counts) {
      const limit = count > 1000 ? 1000 : count > 200 ? 1000 : 200
      const result = await queryLogs(
        { cwd: seeded.cwd, json: false, flat: true, limit, run: `run-${count}` },
        seeded.root,
      )
      expect(result.matched_count).toBe(count)
      expect(result.returned_count).toBe(Math.min(count, limit))
      expect(result.events).toHaveLength(result.returned_count)
      expect(result.truncated).toBe(count > limit)
      expect(result.next_cursor === null).toBe(count <= limit)
      expect(() => assertDiagnosticQueryResult(result)).not.toThrow()
    }
  } finally {
    await cleanup(seeded)
  }
})

test("JSON 和 human stdout 均不超过 256 KiB，记录不被按字节切坏", async () => {
  const seeded = await setup()
  try {
    const ids = Array.from({ length: 32 }, (_, index) => `skill-${index}-${"x".repeat(48)}`)
    const lines = Array.from({ length: 80 }, (_, index) => JSON.stringify(record(
      seeded.fingerprint,
      "run-large",
      index + 1,
      {
        event: "catalog.bound",
        fields: {
          skill_count: 32,
          mcp_count: 32,
          plugin_count: 32,
          skill_ids: ids,
          mcp_ids: ids.map(value => `mcp-${value}`),
          plugin_ids: ids.map(value => `plugin-${value}`),
        },
      },
    )))
    await writeFile(seeded.file, `${lines.join("\n")}\n`)

    const jsonResult = await queryLogs(
      { cwd: seeded.cwd, json: true, limit: 200, run: "run-large" },
      seeded.root,
    )
    const jsonOutput = `${JSON.stringify(jsonResult, null, 2)}\n`
    expect(Buffer.byteLength(jsonOutput, "utf8")).toBeLessThanOrEqual(MAX_STDOUT_BYTES)
    expect(jsonResult.returned_count).toBeLessThan(80)
    expect(jsonResult.next_cursor).toBeString()
    expect(() => assertDiagnosticQueryResult(jsonResult)).not.toThrow()
    expect(jsonResult.events.every(event => event.event === "catalog.bound")).toBeTrue()

    const humanResult = await queryLogs(
      { cwd: seeded.cwd, json: false, flat: true, limit: 200, run: "run-large" },
      seeded.root,
    )
    const humanOutput = `${renderLogsHuman(
      humanResult,
      { cwd: seeded.cwd, json: false, flat: true, limit: 200, run: "run-large" },
      88,
    )}\n`
    expect(Buffer.byteLength(humanOutput, "utf8")).toBeLessThanOrEqual(MAX_STDOUT_BYTES)
    expect(humanOutput).not.toContain("�")
  } finally {
    await cleanup(seeded)
  }
})

test("cursor 三页无重复漏读，首页后追加和新 segment 不进入 snapshot", async () => {
  const seeded = await setup()
  try {
    const initial = Array.from({ length: 5 }, (_, index) => (
      JSON.stringify(record(seeded.fingerprint, "run-cursor", index + 1))
    ))
    await writeFile(seeded.file, `${initial.join("\n")}\n`)

    const first = await queryLogs(
      { cwd: seeded.cwd, json: false, flat: true, limit: 2, run: "run-cursor" },
      seeded.root,
    )
    expect(first.next_cursor).toBeString()

    await appendFile(
      seeded.file,
      `${JSON.stringify(record(seeded.fingerprint, "run-cursor", 6))}\n`,
    )
    const newSegment = join(seeded.root, "2026-08-27", "agent-1787800000100-4322-0002.jsonl")
    await writeFile(
      newSegment,
      `${JSON.stringify(record(seeded.fingerprint, "run-cursor", 7, {
        process: { pid: 4322, started_at_ms: 1_787_800_000_100, record_sequence: 1 },
      }))}\n`,
    )

    const second = await queryLogs(
      {
        cwd: seeded.cwd,
        json: false,
        flat: true,
        limit: 2,
        run: "run-cursor",
        cursor: first.next_cursor!,
      },
      seeded.root,
    )
    const third = await queryLogs(
      {
        cwd: seeded.cwd,
        json: false,
        flat: true,
        limit: 2,
        run: "run-cursor",
        cursor: second.next_cursor!,
      },
      seeded.root,
    )

    const sequences = [...first.events, ...second.events, ...third.events]
      .map(event => event.process.record_sequence)
    expect(sequences).toEqual([1, 2, 3, 4, 5])
    expect(new Set(sequences).size).toBe(5)
    expect(third.next_cursor).toBeNull()
    expect(third.truncated).toBeFalse()
  } finally {
    await cleanup(seeded)
  }
})

test("human 截断页显示总数、已显示数和可复制下一页命令", async () => {
  const seeded = await setup()
  try {
    const lines = Array.from({ length: 3 }, (_, index) => (
      JSON.stringify(record(seeded.fingerprint, "run-human-cursor", index + 1))
    ))
    await writeFile(seeded.file, `${lines.join("\n")}\n`)
    const query = {
      cwd: seeded.cwd,
      json: false,
      flat: true,
      limit: 2,
      run: "run-human-cursor",
    } as const
    const result = await queryLogs(query, seeded.root)
    const output = renderLogsHuman(result, query, 88)

    expect(output).toContain("共 3 条 · 已显示 2 条 · 已截断")
    expect(output).toContain("harness logs --run run-human-cursor --flat --limit 2 --cursor ")
    expect(output).toContain(result.next_cursor!)
  } finally {
    await cleanup(seeded)
  }
})
