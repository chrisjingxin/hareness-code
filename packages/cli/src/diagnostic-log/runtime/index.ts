/** CLI 本地诊断日志：同步校验入队，单异步 writer 负责文件生命周期。 */

import { Buffer } from "node:buffer"
import { arch, homedir, platform, release } from "node:os"
import { chmod, lstat, mkdir, open, readdir, rename, rm, type FileHandle } from "node:fs/promises"
import { join } from "node:path"
import {
  DIAGNOSTIC_EVENT_LEVELS,
  MAX_DIAGNOSTIC_RECORD_BYTES,
  assertDiagnosticRecord,
  type DiagnosticComponent,
  type DiagnosticContext,
  type DiagnosticEvent,
  type DiagnosticFieldsMap,
  type DiagnosticLevel,
  type DiagnosticLevelMap,
  type DiagnosticRecord,
} from "@za38/protocol/diagnostic-log"

const LEVEL_ORDER: Record<DiagnosticLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 }
const DEFAULT_QUEUE = { maxRecords: 4096, maxBytes: 8 * 1024 * 1024, reservedRecords: 128, reservedBytes: 256 * 1024 }
const CLOSED_FILE = /^(cli|agent)-(\d+)-(\d+)-(\d{4})\.jsonl$/
const ACTIVE_FILE = /^(cli|agent)-(\d+)-(\d+)-(\d{4})\.active\.jsonl$/
const DATE_DIRECTORY = /^\d{4}-\d{2}-\d{2}$/

type EventsForLevel<L extends DiagnosticLevel> = {
  [E in DiagnosticEvent]: L extends DiagnosticLevelMap[E] ? E : never
}[DiagnosticEvent]

export type DiagnosticQueueLimits = typeof DEFAULT_QUEUE
export type DiagnosticRuntimeOptions = {
  component: DiagnosticComponent
  projectFingerprint: string
  root?: string
  level?: DiagnosticLevel
  maxFileBytes?: number
  retentionDays?: number
  maxTotalBytes?: number
  queue?: DiagnosticQueueLimits
  processId?: number
  startedAtMs?: number
  startWorker?: boolean
  now?: () => number
  closeTimeoutMs?: number
  writer?: DiagnosticWriter
}

export interface DiagnosticWriter {
  append(encoded: string, bytes: number): Promise<void>
  close(): Promise<void>
  abort(): Promise<void>
}

export interface DiagnosticLog {
  child(context: DiagnosticContext): DiagnosticLog
  debug<E extends EventsForLevel<"debug">>(event: E, fields: DiagnosticFieldsMap[E]): void
  info<E extends EventsForLevel<"info">>(event: E, fields: DiagnosticFieldsMap[E]): void
  warn<E extends EventsForLevel<"warn">>(event: E, fields: DiagnosticFieldsMap[E]): void
  error<E extends EventsForLevel<"error">>(event: E, fields: DiagnosticFieldsMap[E]): void
}

export type DiagnosticSnapshot = {
  queuedRecords: number
  queuedBytes: number
  writtenRecords: number
  nextRecordSequence: number
  contractViolations: number
  dropped: Record<DiagnosticLevel, number>
  oversize: number
  disabled: boolean
}

export interface DiagnosticLogLifecycle {
  start(): void
  reconfigure(settings: { level: DiagnosticLevel; maxFileBytes?: number; retentionDays?: number; maxTotalBytes?: number }, source?: "default" | "environment" | "config" | "initialize"): void
  close(): Promise<{ outcome: "completed" | "timeout" | "disabled"; writtenRecords: number; droppedRecords: number }>
  snapshot(): DiagnosticSnapshot
}

type QueueRecord = { level: DiagnosticLevel; sequence: number; bytes: number; encoded: string }
type MutableState = {
  level: DiagnosticLevel
  maxFileBytes: number
  retentionDays: number
  maxTotalBytes: number
  nextSequence: number
  written: number
  contractViolations: number
  oversize: number
  dropped: Record<DiagnosticLevel, number>
  reportedDropped: Record<DiagnosticLevel, number>
  reportedContractViolations: number
  disabled: boolean
  closing: boolean
}

/** 创建普通业务 logger 与仅供 composition root 持有的生命周期 controller。 */
export function createDiagnosticLog(options: DiagnosticRuntimeOptions): { log: DiagnosticLog; lifecycle: DiagnosticLogLifecycle } {
  const startedAtMs = options.startedAtMs ?? Date.now()
  const now = options.now ?? Date.now
  const queueLimits = options.queue ?? DEFAULT_QUEUE
  const state: MutableState = {
    level: options.level ?? environmentLevel() ?? "info",
    maxFileBytes: options.maxFileBytes ?? 16 * 1024 * 1024,
    retentionDays: options.retentionDays ?? 14,
    maxTotalBytes: options.maxTotalBytes ?? 200 * 1024 * 1024,
    nextSequence: 1,
    written: 0,
    contractViolations: 0,
    oversize: 0,
    dropped: { debug: 0, info: 0, warn: 0, error: 0 },
    reportedDropped: { debug: 0, info: 0, warn: 0, error: 0 },
    reportedContractViolations: 0,
    disabled: false,
    closing: false,
  }
  const queue = new BoundedQueue(queueLimits, state)
  const writer: DiagnosticWriter = options.writer ?? new SegmentWriter({
    component: options.component,
    root: options.root ?? join(homedir(), ".harness", "logs"),
    processId: options.processId ?? process.pid,
    startedAtMs,
    state,
  })
  let draining: Promise<void> | null = null
  let started = options.startWorker !== false

  const emit = (context: DiagnosticContext, level: DiagnosticLevel, event: DiagnosticEvent, fields: unknown): void => {
    if (state.closing || state.disabled || LEVEL_ORDER[level] < LEVEL_ORDER[state.level]) return
    const sequence = state.nextSequence++
    const record = {
      schema_version: 1,
      timestamp_ms: now(),
      level,
      event,
      component: options.component,
      process: { pid: options.processId ?? process.pid, started_at_ms: startedAtMs, record_sequence: sequence },
      project_fingerprint: options.projectFingerprint,
      ...context,
      fields,
    }
    try {
      const encoded = `${JSON.stringify(record)}\n`
      const bytes = Buffer.byteLength(encoded, "utf8")
      if (bytes > MAX_DIAGNOSTIC_RECORD_BYTES) {
        state.oversize += 1
        return
      }
      assertDiagnosticRecord(record)
      queue.enqueue({ level, sequence, bytes, encoded })
      scheduleDrain()
    } catch {
      state.contractViolations += 1
    }
  }

  const makeLog = (context: DiagnosticContext): DiagnosticLog => Object.freeze({
    child(childContext: DiagnosticContext): DiagnosticLog {
      return makeLog(Object.freeze({ ...context, ...childContext }))
    },
    debug(event: DiagnosticEvent, fields: unknown): void { emit(context, "debug", event, fields) },
    info(event: DiagnosticEvent, fields: unknown): void { emit(context, "info", event, fields) },
    warn(event: DiagnosticEvent, fields: unknown): void { emit(context, "warn", event, fields) },
    error(event: DiagnosticEvent, fields: unknown): void { emit(context, "error", event, fields) },
  }) as DiagnosticLog
  const log = makeLog(Object.freeze({}))

  function scheduleDrain(): void {
    if (!started || draining || state.disabled) return
    draining = Promise.resolve().then(drain).finally(() => {
      draining = null
      if (queue.length > 0 && !state.disabled) scheduleDrain()
    })
  }

  async function drain(): Promise<void> {
    try {
      const records = queue.takeAll()
      for (const record of records) await writer.append(record.encoded, record.bytes)
      state.written += records.length
      await writeAggregateEvents()
    } catch {
      state.disabled = true
      await writer.abort()
    }
  }

  async function writeAggregateEvents(): Promise<void> {
    if (state.disabled) return
    const pendingDropped = Object.fromEntries((Object.keys(state.dropped) as DiagnosticLevel[]).map(level => [level, state.dropped[level] - state.reportedDropped[level]])) as Record<DiagnosticLevel, number>
    const droppedCount = Object.values(pendingDropped).reduce((sum, count) => sum + count, 0)
    if (droppedCount > 0 || state.oversize > 0) {
      const encoded = internalRecord("warn", "logging.dropped", {
        debug_count: pendingDropped.debug,
        info_count: pendingDropped.info,
        warn_count: pendingDropped.warn,
        error_count: pendingDropped.error,
        invalid_count: 0,
        oversize_count: state.oversize,
        reason: state.oversize > 0 ? "record_too_large" : "queue_full",
      })
      if (encoded) await writer.append(encoded, Buffer.byteLength(encoded));
      state.reportedDropped = { ...state.dropped }
      state.oversize = 0
    }
    const violations = state.contractViolations - state.reportedContractViolations
    if (violations > 0) {
      const encoded = internalRecord("warn", "logging.contract_violation", { invalid_level_count: 0, invalid_event_count: 0, invalid_field_count: violations })
      if (encoded) await writer.append(encoded, Buffer.byteLength(encoded))
      state.reportedContractViolations = state.contractViolations
    }
  }

  function internalRecord(level: DiagnosticLevel, event: DiagnosticEvent, fields: unknown): string | null {
    const record = {
      schema_version: 1, timestamp_ms: now(), level, event, component: options.component,
      process: { pid: options.processId ?? process.pid, started_at_ms: startedAtMs, record_sequence: state.nextSequence++ },
      project_fingerprint: options.projectFingerprint, fields,
    }
    try {
      assertDiagnosticRecord(record)
      return `${JSON.stringify(record)}\n`
    } catch {
      return null
    }
  }

  log.info("logging.started", {
    effective_level: state.level,
    max_queue_records: queueLimits.maxRecords,
    max_queue_bytes: queueLimits.maxBytes,
    reserved_queue_records: queueLimits.reservedRecords,
    reserved_queue_bytes: queueLimits.reservedBytes,
    max_file_bytes: state.maxFileBytes,
  })

  const lifecycle: DiagnosticLogLifecycle = {
    start(): void { started = true; scheduleDrain() },
    reconfigure(settings, source = "initialize"): void {
      const previous = state.level
      state.level = settings.level
      if (settings.maxFileBytes !== undefined) state.maxFileBytes = settings.maxFileBytes
      if (settings.retentionDays !== undefined) state.retentionDays = settings.retentionDays
      if (settings.maxTotalBytes !== undefined) state.maxTotalBytes = settings.maxTotalBytes
      emit({}, "info", "logging.reconfigured", { previous_level: previous, effective_level: state.level, source })
    },
    async close() {
      if (!state.disabled) emit({}, "info", "logging.stopped", {
        flush_outcome: "completed", queued_count: queue.length, written_count: state.written,
        dropped_count: Object.values(state.dropped).reduce((sum, count) => sum + count, 0), duration_ms: 0,
      })
      state.closing = true
      started = true
      scheduleDrain()
      const finished = await Promise.race([
        (async () => { while (draining) await draining })().then(() => true),
        new Promise<false>(resolve => setTimeout(() => resolve(false), options.closeTimeoutMs ?? 2_000)),
      ])
      if (!finished) {
        queue.clear()
        state.disabled = true
        void writer.abort()
      } else {
        await writer.close()
      }
      return {
        outcome: state.disabled ? "disabled" : finished ? "completed" : "timeout",
        writtenRecords: state.written,
        droppedRecords: Object.values(state.dropped).reduce((sum, count) => sum + count, 0),
      }
    },
    snapshot(): DiagnosticSnapshot {
      return {
        queuedRecords: queue.length, queuedBytes: queue.bytes, writtenRecords: state.written,
        nextRecordSequence: state.nextSequence, contractViolations: state.contractViolations,
        dropped: { ...state.dropped }, oversize: state.oversize, disabled: state.disabled,
      }
    },
  }
  return { log, lifecycle }
}

class BoundedQueue {
  private records: QueueRecord[] = []
  bytes = 0

  constructor(private limits: DiagnosticQueueLimits, private state: MutableState) {}
  get length(): number { return this.records.length }

  enqueue(record: QueueRecord): void {
    const priority = record.level === "warn" || record.level === "error"
    const maxRecords = priority ? this.limits.maxRecords : this.limits.maxRecords - this.limits.reservedRecords
    const maxBytes = priority ? this.limits.maxBytes : this.limits.maxBytes - this.limits.reservedBytes
    if (record.level === "info") this.evictUntilFits("debug", record, maxRecords, maxBytes)
    if (priority) {
      this.evictUntilFits("debug", record, maxRecords, maxBytes)
      this.evictUntilFits("info", record, maxRecords, maxBytes)
    }
    if (this.records.length + 1 > maxRecords || this.bytes + record.bytes > maxBytes) {
      this.state.dropped[record.level] += 1
      return
    }
    this.records.push(record)
    this.bytes += record.bytes
  }

  takeAll(): QueueRecord[] {
    const result = this.records.sort((left, right) => left.sequence - right.sequence)
    this.records = []
    this.bytes = 0
    return result
  }

  clear(): void { this.records = []; this.bytes = 0 }

  private evictUntilFits(level: DiagnosticLevel, incoming: QueueRecord, maxRecords: number, maxBytes: number): void {
    while ((this.records.length + 1 > maxRecords || this.bytes + incoming.bytes > maxBytes)) {
      const index = this.records.findIndex(record => record.level === level)
      if (index < 0) return
      const [removed] = this.records.splice(index, 1)
      this.bytes -= removed!.bytes
      this.state.dropped[level] += 1
    }
  }
}

class SegmentWriter {
  private handle: FileHandle | null = null
  private activePath: string | null = null
  private segment = 0
  private bytes = 0

  constructor(private options: { component: DiagnosticComponent; root: string; processId: number; startedAtMs: number; state: MutableState }) {}

  async append(encoded: string, bytes: number): Promise<void> {
    if (!this.handle) await this.openSegment()
    if (this.bytes > 0 && this.bytes + bytes > this.options.state.maxFileBytes) {
      await this.finalizeSegment()
      await this.openSegment()
    }
    await this.handle!.writeFile(encoded, { encoding: "utf8" })
    this.bytes += bytes
  }

  async close(): Promise<void> { await this.finalizeSegment() }
  async abort(): Promise<void> {
    await this.handle?.close().catch(() => undefined)
    this.handle = null
  }

  private async openSegment(): Promise<void> {
    const date = new Date(this.options.startedAtMs).toISOString().slice(0, 10)
    const directory = join(this.options.root, date)
    await mkdir(this.options.root, { recursive: true, mode: 0o700 })
    if ((await lstat(this.options.root)).isSymbolicLink()) throw new Error("Diagnostic log root must not be a symlink")
    if (process.platform !== "win32") await chmod(this.options.root, 0o700)
    await mkdir(directory, { recursive: true, mode: 0o700 })
    if (process.platform !== "win32") await chmod(directory, 0o700)
    while (true) {
      const name = `${this.options.component}-${this.options.startedAtMs}-${this.options.processId}-${String(this.segment).padStart(4, "0")}.active.jsonl`
      const path = join(directory, name)
      try {
        this.handle = await open(path, "ax", 0o600)
        if (process.platform !== "win32") await chmod(path, 0o600)
        this.activePath = path
        this.bytes = 0
        await this.retainClosed()
        return
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
        this.segment += 1
      }
    }
  }

  private async finalizeSegment(): Promise<void> {
    if (!this.handle || !this.activePath) return
    const activePath = this.activePath
    await this.handle.close()
    this.handle = null
    this.activePath = null
    await rename(activePath, activePath.replace(".active.jsonl", ".jsonl"))
    this.segment += 1
  }

  private async retainClosed(): Promise<void> {
    const candidates: Array<{ path: string; mtimeMs: number; size: number }> = []
    let protectedBytes = 0
    for (const dateName of await readdir(this.options.root).catch(() => [])) {
      if (!DATE_DIRECTORY.test(dateName)) continue
      const directory = join(this.options.root, dateName)
      if (!(await lstat(directory).catch(() => null))?.isDirectory()) continue
      for (const name of await readdir(directory).catch(() => [])) {
        const path = join(directory, name)
        const info = await lstat(path).catch(() => null)
        if (!info?.isFile() || info.isSymbolicLink()) continue
        if (ACTIVE_FILE.test(name)) {
          protectedBytes += info.size
          continue
        }
        if (!CLOSED_FILE.test(name)) continue
        candidates.push({ path, mtimeMs: info.mtimeMs, size: info.size })
      }
    }
    candidates.sort((left, right) => left.mtimeMs - right.mtimeMs)
    const cutoff = Date.now() - this.options.state.retentionDays * 86_400_000
    let total = protectedBytes + candidates.reduce((sum, item) => sum + item.size, 0)
    for (const item of candidates) {
      if (item.mtimeMs < cutoff || total > this.options.state.maxTotalBytes) {
        await rm(item.path, { force: true }).catch(() => undefined)
        total -= item.size
      }
    }
  }
}

export function defaultProcessFields(commandKind: string): DiagnosticFieldsMap["process.started"] {
  return { command_kind: commandKind, runtime_version: `bun-${Bun.version}`, platform: `${platform()}-${release()}`, arch: arch() }
}

function environmentLevel(): DiagnosticLevel | null {
  const value = process.env.HARNESS_LOG_LEVEL
  return value === "debug" || value === "info" || value === "warn" || value === "error" ? value : null
}
