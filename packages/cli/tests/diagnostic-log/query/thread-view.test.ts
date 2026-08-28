/** harness logs 的 Thread 聚合、过程关联与中文投影测试。 */

import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises"
import { realpath } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { assertDiagnosticQueryResult } from "@za38/protocol/diagnostic-log"

import {
  LogsQueryError,
  queryLogs,
  renderLogsHuman,
} from "../../../src/diagnostic-log/query"

type EventInput = {
  timestamp: number
  sequence: number
  event: string
  level?: "debug" | "info" | "warn" | "error"
  threadId?: string
  runId?: string
  executionId?: string
  parentExecutionId?: string
  agentId?: string
  fields: Record<string, unknown>
}

function terminalWidth(value: string): number {
  let width = 0
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0
    width += (
      code >= 0x1100 && (
        code <= 0x115f
        || code === 0x2329
        || code === 0x232a
        || (code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f)
        || (code >= 0xac00 && code <= 0xd7a3)
        || (code >= 0xf900 && code <= 0xfaff)
        || (code >= 0xfe10 && code <= 0xfe19)
        || (code >= 0xfe30 && code <= 0xfe6f)
        || (code >= 0xff00 && code <= 0xff60)
        || (code >= 0xffe0 && code <= 0xffe6)
        || (code >= 0x20000 && code <= 0x3fffd)
      )
    ) ? 2 : 1
  }
  return width
}

function columnOf(line: string, value: string): number {
  const index = line.indexOf(value)
  if (index < 0) return -1
  return terminalWidth(line.slice(0, index))
}

async function fixture(): Promise<{ root: string; cwd: string; fingerprint: string }> {
  const root = await mkdtemp(join(tmpdir(), "harness-thread-query-"))
  const cwd = await mkdtemp(join(tmpdir(), "harness-thread-cwd-"))
  const fingerprint = createHash("sha256").update(await realpath(cwd)).digest("hex")
  const events: EventInput[] = [
    { timestamp: 1000, sequence: 1, event: "process.started", fields: { command_kind: "run", runtime_version: "python", platform: "darwin", arch: "arm64" } },
    { timestamp: 1010, sequence: 2, event: "mcp.connection.completed", fields: { server_name: "github", server_fingerprint: "b".repeat(64), transport: "http", duration_ms: 3800, tool_count: 3 } },
    { timestamp: 1020, sequence: 3, event: "ipc.initialize.completed", fields: { side: "server", duration_ms: 50, protocol_minor: 3 } },
    { timestamp: 1100, sequence: 4, event: "run.started", threadId: "thread-alpha", runId: "run-alpha-1", fields: { mode: "build", resumed: false, approval_mode: "default", model_profile_id: "default" } },
    { timestamp: 1200, sequence: 5, event: "run.completed", threadId: "thread-alpha", runId: "run-alpha-1", fields: { outcome: "completed", duration_ms: 4200, active_ms: 4000, interaction_wait_ms: 0, retry_wait_ms: 0, first_visible_activity_ms: 30, usage: { input_tokens: 10, output_tokens: 20, cached_input_tokens: 0 } } },
    { timestamp: 1250, sequence: 6, event: "run.started", threadId: "thread-beta", runId: "run-beta-1", fields: { mode: "compose", resumed: false, approval_mode: "default", model_profile_id: "default" } },
    { timestamp: 1300, sequence: 7, event: "run.completed", threadId: "thread-beta", runId: "run-beta-1", fields: { outcome: "completed", duration_ms: 2000, active_ms: 1800, interaction_wait_ms: 0, retry_wait_ms: 0, first_visible_activity_ms: 20, usage: { input_tokens: 8, output_tokens: 8, cached_input_tokens: null } } },
    { timestamp: 1400, sequence: 8, event: "run.started", threadId: "thread-alpha", runId: "run-alpha-2", fields: { mode: "build", resumed: true, approval_mode: "default", model_profile_id: "default" } },
    { timestamp: 1410, sequence: 9, event: "runtime.acquire.completed", threadId: "thread-alpha", runId: "run-alpha-2", fields: { source: "new", queue_ms: 0, build_ms: 326, duration_ms: 326 } },
    { timestamp: 1450, sequence: 10, event: "context.compaction.completed", threadId: "thread-alpha", runId: "run-alpha-2", fields: { trigger: "auto", action: "auto_micro", duration_ms: 50, before_estimated_tokens: 12000, after_estimated_tokens: 7000, artifact_count: 2 } },
    { timestamp: 1475, sequence: 11, event: "model.completed", threadId: "thread-alpha", runId: "run-alpha-2", fields: { model_round: 1, provider_attempt: 1, duration_ms: 8610, provider_first_chunk_ms: 100, usage: { input_tokens: 10, output_tokens: 20, cached_input_tokens: 0 }, finish_reason: "stop" } },
    { timestamp: 1480, sequence: 12, event: "catalog.bound", threadId: "thread-alpha", runId: "run-alpha-2", fields: { skill_count: 1, mcp_count: 1, plugin_count: 1, skill_ids: ["project/review"], mcp_ids: ["github"], plugin_ids: ["guard"] } },
    { timestamp: 1482, sequence: 13, event: "skill.read", threadId: "thread-alpha", runId: "run-alpha-2", fields: { skill_id: "project/review", kind: "body" } },
    { timestamp: 1484, sequence: 14, event: "hook.started", threadId: "thread-alpha", runId: "run-alpha-2", fields: { plugin_id: "guard", hook_event: "PreToolUse", tool_name: "execute" } },
    { timestamp: 1486, sequence: 15, event: "hook.completed", threadId: "thread-alpha", runId: "run-alpha-2", fields: { plugin_id: "guard", hook_event: "PreToolUse", tool_name: "execute", outcome: "allow", duration_ms: 6 } },
    { timestamp: 1490, sequence: 16, event: "execution.started", threadId: "thread-alpha", runId: "run-alpha-2", executionId: "child-explore", parentExecutionId: "root-run-alpha-2", agentId: "explore", fields: { kind: "child", agent_id: "explore" } },
    { timestamp: 1492, sequence: 17, event: "skill.read", threadId: "thread-alpha", runId: "run-alpha-2", executionId: "child-explore", parentExecutionId: "root-run-alpha-2", agentId: "explore", fields: { skill_id: "project/review", kind: "resource" } },
    { timestamp: 1495, sequence: 18, event: "execution.completed", threadId: "thread-alpha", runId: "run-alpha-2", executionId: "child-explore", parentExecutionId: "root-run-alpha-2", agentId: "explore", fields: { kind: "child", agent_id: "explore", outcome: "completed", duration_ms: 1200 } },
    { timestamp: 1496, sequence: 19, event: "execution.started", threadId: "thread-alpha", runId: "run-alpha-2", executionId: "child-plan", parentExecutionId: "root-run-alpha-2", agentId: "work-item-plan", fields: { kind: "compose_stage", agent_id: "work-item-plan" } },
    { timestamp: 1498, sequence: 20, event: "execution.completed", threadId: "thread-alpha", runId: "run-alpha-2", executionId: "child-plan", parentExecutionId: "root-run-alpha-2", agentId: "work-item-plan", fields: { kind: "compose_stage", agent_id: "work-item-plan", outcome: "completed", duration_ms: 800 } },
    { timestamp: 1500, sequence: 21, event: "tool.failed", level: "error", threadId: "thread-alpha", runId: "run-alpha-2", fields: { tool_name: "github.list_issues", tool_kind: "mcp", server_name: "github", duration_ms: 5000, failure_stage: "tool_handler", error_code: "TIMEOUT", error_type: "TimeoutError", retryable: true, summary_code: "TOOL_TIMEOUT" } },
    { timestamp: 1540, sequence: 22, event: "runtime.released", threadId: "thread-alpha", runId: "run-alpha-2", fields: { outcome: "released", duration_ms: 0 } },
    { timestamp: 1550, sequence: 23, event: "run.failed", level: "error", threadId: "thread-alpha", runId: "run-alpha-2", fields: { duration_ms: 15800, active_ms: 9000, interaction_wait_ms: 0, retry_wait_ms: 1000, failure_stage: "run", error_code: "TIMEOUT", error_type: "TimeoutError", retryable: false, summary_code: "RUN_TIMEOUT" } },
  ]
  const records = events.map(input => ({
    schema_version: 1,
    timestamp_ms: input.timestamp,
    level: input.level ?? "info",
    event: input.event,
    component: "agent",
    process: { pid: 42, started_at_ms: 1000, record_sequence: input.sequence },
    project_fingerprint: fingerprint,
    ...(input.threadId ? { thread_id: input.threadId } : {}),
    ...(input.runId ? { run_id: input.runId } : {}),
    ...(input.executionId ? { execution_id: input.executionId } : {}),
    ...(input.parentExecutionId ? { parent_execution_id: input.parentExecutionId } : {}),
    ...(input.agentId ? { agent_id: input.agentId } : {}),
    fields: input.fields,
  }))
  const dir = join(root, "2026-08-27")
  await mkdir(dir, { recursive: true })
  await writeFile(join(dir, "agent-1000-42-0001.jsonl"), `${records.map(record => JSON.stringify(record)).join("\n")}\n`)
  return { root, cwd, fingerprint }
}

test("默认列表按最近活动聚合 Thread，--thread/--run 合并同进程准备事件", async () => {
  const seeded = await fixture()
  try {
    const list = await queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, seeded.root)
    expect(list.events).toEqual([])
    expect(list.threads?.map(thread => thread.thread_id)).toEqual(["thread-alpha", "thread-beta"])
    expect(list.threads?.[0]).toMatchObject({ run_count: 2, latest_outcome: "failed", latest_duration_ms: 15800 })
    expect(renderLogsHuman(list, { cwd: seeded.cwd, json: false, limit: 20 })).toContain("最近 Thread")
    expect(() => assertDiagnosticQueryResult(list)).not.toThrow()

    const thread = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, thread: "thread-a" }, seeded.root)
    expect(thread.thread_id).toBe("thread-alpha")
    expect(thread.summary).toMatchObject({ run_count: 2, outcome: "failed" })
    expect(thread.events.some(event => event.event === "mcp.connection.completed")).toBeTrue()
    expect(thread.events.filter(event => event.event === "run.started")).toHaveLength(2)
    expect(() => assertDiagnosticQueryResult(thread)).not.toThrow()

    const run = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, seeded.root)
    expect(run.run_id).toBe("run-alpha-2")
    expect(run.thread_id).toBeUndefined()
    expect(run.events.some(event => event.event === "mcp.connection.completed")).toBeTrue()
    const runOutput = renderLogsHuman(run, { cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" })
    expect(runOutput).toContain("失败点")
    expect(runOutput).toContain("过程")
    expect(runOutput).toContain("慢项")
    expect(runOutput).toContain("harness logs --thread")

    const filtered = await queryLogs({
      cwd: seeded.cwd,
      json: false,
      limit: 200,
      thread: "thread-alpha",
      level: "error",
      event: "tool",
      component: "agent",
    }, seeded.root)
    expect(filtered.events.map(event => event.event)).toEqual(["tool.failed"])
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("Thread/Run 前缀无匹配或歧义返回稳定 code", async () => {
  const seeded = await fixture()
  try {
    await expect(queryLogs({ cwd: seeded.cwd, json: false, limit: 200, thread: "missing" }, seeded.root))
      .rejects.toMatchObject({ code: "THREAD_NOT_FOUND" } satisfies Partial<LogsQueryError>)
    await expect(queryLogs({ cwd: seeded.cwd, json: false, limit: 200, thread: "thread-" }, seeded.root))
      .rejects.toMatchObject({ code: "THREAD_PREFIX_AMBIGUOUS" } satisfies Partial<LogsQueryError>)
    await expect(queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha" }, seeded.root))
      .rejects.toMatchObject({ code: "RUN_PREFIX_AMBIGUOUS" } satisfies Partial<LogsQueryError>)
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("中文 Thread 主视图展示名字、折叠旧 Run、失败点与慢项", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, thread: "thread-alpha" }, seeded.root)
    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 200, thread: "thread-alpha" })
    expect(output).toContain("Thread    thread-alpha")
    expect(output).toContain("失败点")
    expect(output).toContain("过程")
    expect(output).toContain("MCP github")
    expect(output).toContain("已连接")
    expect(output).toContain("更早的运行已折叠")
    expect(output).toContain("自动压缩")
    expect(output).toContain("慢项")
    expect(output).not.toContain("mcp.connection.completed")
    expect(output).not.toContain("b".repeat(64))
    expect(output).not.toContain(seeded.fingerprint)
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("慢项排除 Run 汇总和零耗时控制事件，并为保留事件使用中文名字", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, seeded.root)
    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" })
    const slow = output.split("慢项\n")[1]?.split("\n\n")[0] ?? ""

    expect(slow).toContain("模型回合 #1")
    expect(slow).toContain("运行时准备")
    expect(slow).toContain("Agent 初始化")
    expect(slow).not.toMatch(/\n运行\s+15\.8s/)
    expect(slow).not.toMatch(/\n步骤\s+\d/)
    expect(slow).not.toMatch(/\s0ms$/m)
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("human 页脚提示非零的日志解析诊断", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, seeded.root)
    result.diagnostics.invalid_count = 16
    result.diagnostics.orphan_active_count = 1
    result.diagnostics.orphan_active_bytes = 512

    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" })
    expect(output).toContain("诊断      无效记录 16 · 未正常关闭文件 1（512 B）")

    const list = await queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, seeded.root)
    list.diagnostics.invalid_count = 16
    const listOutput = renderLogsHuman(list, { cwd: seeded.cwd, json: false, limit: 20 })
    expect(listOutput).toContain("日志      无效记录 16")
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("Thread 列表按终端显示宽度对齐中文列并建立清晰层级", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, seeded.root)
    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 20 }, 88)
    const lines = output.split("\n")
    const header = lines.find(line => line.includes("THREAD")) ?? ""
    const alpha = lines.find(line => line.includes("thread-alpha")) ?? ""

    expect(lines.slice(0, 3)).toEqual(["HARNESS LOGS", "最近 Thread · 2", ""])
    expect(columnOf(header, "THREAD")).toBe(columnOf(alpha, "thread-alpha"))
    expect(columnOf(header, "次数") + terminalWidth("次数")).toBe(columnOf(alpha, "2") + terminalWidth("2"))
    expect(output).toContain("提示  harness logs --thread <prefix> 查看详情")
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("Run 详情使用扫描友好的摘要、过程、慢项和记录区块", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, seeded.root)
    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, 88)

    expect(output).toContain("HARNESS LOGS\nRun       run-alpha-2")
    expect(output).toContain("模型回合 #1 / 尝试 #1")
    expect(output).toContain("目录")
    expect(output).toContain("Skill 读取 project/review")
    expect(output).toContain("Hook PreToolUse")
    expect(output).toContain("子代理 explore")
    expect(output).toContain("阶段 work-item-plan")
    expect(output).toContain("资源")
    expect(output).toContain("慢项\n耗时      项目")
    expect(output).toContain("共 19 条 · 已显示 19 条")
    expect(output).not.toContain("步骤                             耗时")
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("human 输出在窄终端内截断而不产生横向溢出", async () => {
  const seeded = await fixture()
  try {
    const result = await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, seeded.root)
    const tool = result.events.find(event => event.event === "tool.failed")
    if (tool) (tool.fields as Record<string, unknown>).tool_name = `github.${"very_long_tool_name_".repeat(5)}`
    const output = renderLogsHuman(result, { cwd: seeded.cwd, json: false, limit: 200, run: "run-alpha-2" }, 64)
    expect(output.split("\n").every(line => terminalWidth(line) <= 64)).toBeTrue()
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})
