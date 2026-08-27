/** TypeScript DiagnosticLog 的异步、有界与文件生命周期测试。 */

import { expect, test } from "bun:test"
import { access, mkdir, mkdtemp, readFile, readdir, rm, stat, utimes, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { createDiagnosticLog } from "../../../src/diagnostic-log/runtime"

test("log.* 同步入队，child context 不可变，close 后得到 closed JSONL", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  try {
    const { log, lifecycle } = createDiagnosticLog(options(root))
    const runLog = log.child({ run_id: "run-1" })
    expect(log.info("process.started", processFields())).toBeUndefined()
    expect(runLog.info("ipc.initialize.completed", { side: "client", duration_ms: 2, protocol_minor: 6 })).toBeUndefined()
    await lifecycle.close()

    const dateDirs = await readdir(root)
    const files = await readdir(join(root, dateDirs[0]!))
    expect(files.some(name => name.endsWith(".active.jsonl"))).toBe(false)
    const text = await readFile(join(root, dateDirs[0]!, files[0]!), "utf8")
    const records = text.trim().split("\n").map(line => JSON.parse(line))
    expect(records.map(record => record.event)).toContain("logging.started")
    expect(records.map(record => record.event)).toContain("logging.stopped")
    expect(records.find(record => record.event === "ipc.initialize.completed")?.run_id).toBe("run-1")
    expect(records.find(record => record.event === "logging.started")?.fields).toMatchObject({
      max_queue_records: 4096,
      max_queue_bytes: 8 * 1024 * 1024,
      reserved_queue_records: 128,
      reserved_queue_bytes: 256 * 1024,
      max_file_bytes: 1024 * 1024,
    })
    if (process.platform !== "win32") {
      expect((await stat(join(root, dateDirs[0]!))).mode & 0o777).toBe(0o700)
      expect((await stat(join(root, dateDirs[0]!, files[0]!))).mode & 0o777).toBe(0o600)
    }
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("小文件上限会先 close 再 rotation，最终不留下 active", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  try {
    const { log, lifecycle } = createDiagnosticLog({ ...options(root), maxFileBytes: 420 })
    log.info("process.started", processFields())
    log.info("process.started", processFields())
    await lifecycle.close()
    const dateDirs = await readdir(root)
    const files = await readdir(join(root, dateDirs[0]!))
    expect(files.length).toBeGreaterThan(1)
    expect(files.every(name => name.endsWith(".jsonl") && !name.includes(".active."))).toBe(true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("retention 只删除过期 closed segment，绝不触碰 active/orphan", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  const oldDate = join(root, "2020-01-01")
  await mkdir(oldDate)
  const closed = join(oldDate, "cli-1-1-0000.jsonl")
  const active = join(oldDate, "agent-1-2-0000.active.jsonl")
  await writeFile(closed, "{}\n")
  await writeFile(active, "{}\n")
  await utimes(closed, 1, 1)
  await utimes(active, 1, 1)
  try {
    const { lifecycle } = createDiagnosticLog({ ...options(root), retentionDays: 1 })
    await lifecycle.close()
    await expect(access(closed)).rejects.toBeDefined()
    await expect(access(active)).resolves.toBeNull()
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("日志 root 不可建立时 writer disabled 且不影响业务调用", async () => {
  const parent = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  const root = join(parent, "root-file")
  await writeFile(root, "occupied")
  try {
    const { log, lifecycle } = createDiagnosticLog(options(root))
    expect(() => log.info("process.started", processFields())).not.toThrow()
    await new Promise(resolve => setTimeout(resolve, 5))
    expect(lifecycle.snapshot().disabled).toBe(true)
    await expect(lifecycle.close()).resolves.toMatchObject({ outcome: "disabled" })
  } finally {
    await rm(parent, { recursive: true, force: true })
  }
})

test("slow writer 的 close 有界返回，不同步阻塞 log.*", async () => {
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  const writer = {
    append: () => blocked,
    close: async () => undefined,
    abort: async () => undefined,
  }
  const { log, lifecycle } = createDiagnosticLog({
    ...options("unused"),
    writer,
    closeTimeoutMs: 20,
  })
  expect(log.info("process.started", processFields())).toBeUndefined()
  const result = await lifecycle.close()
  expect(result.outcome).toBe("disabled")
  release()
})

test("队列满时 DEBUG 先被淘汰且 sequence 不复用", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  try {
    const { log, lifecycle } = createDiagnosticLog({
      ...options(root),
      queue: { maxRecords: 6, maxBytes: 64 * 1024, reservedRecords: 2, reservedBytes: 8 * 1024 },
      startWorker: false,
    })
    for (let index = 0; index < 8; index += 1) {
      log.debug("runtime.pool_snapshot", { active_count: index, idle_count: 0, waiter_count: 0, eviction_count: 0 })
    }
    log.info("process.started", processFields())
    const snapshot = lifecycle.snapshot()
    expect(snapshot.dropped.debug).toBeGreaterThan(0)
    expect(snapshot.nextRecordSequence).toBe(11)
    lifecycle.start()
    await lifecycle.close()
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("WARN/ERROR 使用保留 lane 并在普通容量满时淘汰 DEBUG/INFO", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  try {
    const { log, lifecycle } = createDiagnosticLog({
      ...options(root),
      queue: { maxRecords: 6, maxBytes: 8 * 1024, reservedRecords: 2, reservedBytes: 2 * 1024 },
      startWorker: false,
    })
    for (let index = 0; index < 8; index += 1) {
      log.debug("runtime.pool_snapshot", { active_count: index, idle_count: 0, waiter_count: 0, eviction_count: 0 })
    }
    log.warn("logging.dropped", {
      debug_count: 1, info_count: 0, warn_count: 0, error_count: 0,
      invalid_count: 0, oversize_count: 0, reason: "queue_full",
    })
    log.error("logging.writer_failed", {
      failure_stage: "append", error_type: "OSError", summary_code: "WRITE_FAILED",
    })
    lifecycle.start()
    await lifecycle.close()
    const date = (await readdir(root))[0]!
    const file = (await readdir(join(root, date)))[0]!
    const events = (await readFile(join(root, date, file), "utf8")).trim().split("\n").map(line => JSON.parse(line).event)
    expect(events).toContain("logging.dropped")
    expect(events).toContain("logging.writer_failed")
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("契约错误不抛给业务路径，只累计 contract violation", async () => {
  const root = await mkdtemp(join(tmpdir(), "harness-diagnostic-ts-"))
  try {
    const { log, lifecycle } = createDiagnosticLog(options(root))
    expect(() => (log.info as Function)("process.started", { token: "secret" })).not.toThrow()
    expect(lifecycle.snapshot().contractViolations).toBe(1)
    expect(() => (log.info as Function)("process.started", {
      command_kind: "x".repeat(9_000), runtime_version: "bun", platform: "darwin", arch: "arm64",
    })).not.toThrow()
    expect(lifecycle.snapshot().oversize).toBe(1)
    await lifecycle.close()
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

function options(root: string) {
  return {
    component: "cli" as const,
    projectFingerprint: "a".repeat(64),
    root,
    level: "debug" as const,
    maxFileBytes: 1024 * 1024,
    retentionDays: 14,
    maxTotalBytes: 8 * 1024 * 1024,
    processId: 42,
    startedAtMs: 1_787_800_000_123,
  }
}

function processFields() {
  return { command_kind: "run", runtime_version: "bun", platform: "darwin", arch: "arm64" }
}
