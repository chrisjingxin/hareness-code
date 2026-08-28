/** harness logs 离线查询：安全发现 JSONL，并按 Thread/Run 投影中文诊断视图。 */

import { createHash } from "node:crypto"
import { lstat, readdir, readFile, realpath, stat } from "node:fs/promises"
import { homedir } from "node:os"
import { isAbsolute, join, relative } from "node:path"
import {
  assertDiagnosticQueryResult,
  assertDiagnosticRecord,
  type DiagnosticLevel,
  type DiagnosticQueryResult,
  type DiagnosticRecord,
} from "@za38/protocol/diagnostic-log"

export type LogsQuery = {
  cwd: string
  json: boolean
  flat?: boolean
  limit: number
  thread?: string
  run?: string
  level?: DiagnosticLevel
  event?: string
  component?: "cli" | "agent"
  cursor?: string
}

export type LogsQueryResult = DiagnosticQueryResult

export class LogsQueryError extends Error {
  constructor(readonly code: string, message: string) {
    super(message)
    this.name = "LogsQueryError"
  }
}

const CLOSED_RE = /^(cli|agent)-(\d+)-(\d+)-(\d{4})\.jsonl$/
const ACTIVE_RE = /^(cli|agent)-(\d+)-(\d+)-(\d{4})\.active\.jsonl$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const MAX_QUERY_OUTPUT_BYTES = 256 * 1024
const MAX_CURSOR_LENGTH = 16_384
const CURSOR_VERSION = 1
const PROCESS_EVENTS = new Set([
  "process.started",
  "process.stopped",
  "logging.stopped",
  "ipc.initialize.completed",
  "mcp.connection.completed",
  "mcp.connection.failed",
  "mcp.connection.closed",
])

export type DiscoveredFile = {
  path: string
  relPath: string
  isActive: boolean
  sizeAtQuery: number
}

type SourcedRecord = {
  record: DiagnosticRecord
  sourcePath: string
  sourceOffset: number
}

type ParseDiagnostics = {
  malformed: number
  invalid: number
  partial: number
  unsupported: number
}

type QueryFilters = LogsQueryResult["filters"]

type CursorFile = {
  path: string
  watermark: number
  digest: string
}

type RecordSortKey = [number, string, number, number, number, string, number]

type SnapshotCursor = {
  cursor_version: 1
  schema_version: 1
  project_fingerprint: string
  selector: { kind: "thread" | "run"; id: string }
  filters: QueryFilters
  files: CursorFile[]
  positions: Array<{ path: string; offset: number }>
  last_sort_key: RecordSortKey
}

type RunRow = {
  run_id: string
  mode?: "build" | "compose"
  outcome?: string
  duration_ms?: number
  started_at_ms: number
}

function getDefaultLogRoot(): string {
  return join(homedir(), ".harness", "logs")
}

async function getProjectFingerprint(cwd: string): Promise<string> {
  const canonical = await realpath(cwd)
  return createHash("sha256").update(canonical, "utf8").digest("hex")
}

function queryFilters(query: LogsQuery): QueryFilters {
  return {
    minimum_level: query.level ?? "info",
    event: query.event ?? null,
    component: query.component ?? null,
  }
}

function cursorSelector(query: LogsQuery): { kind: "thread" | "run"; value: string } | undefined {
  if (query.thread) return { kind: "thread", value: query.thread }
  if (query.run) return { kind: "run", value: query.run }
  return undefined
}

function recordSortKey(item: SourcedRecord): RecordSortKey {
  const record = item.record
  return [
    record.timestamp_ms,
    record.component,
    record.process.started_at_ms,
    record.process.pid,
    record.process.record_sequence,
    item.sourcePath,
    item.sourceOffset,
  ]
}

function compareSortKey(left: RecordSortKey, right: RecordSortKey): number {
  for (let index = 0; index < left.length; index += 1) {
    const a = left[index]!
    const b = right[index]!
    if (a === b) continue
    return a < b ? -1 : 1
  }
  return 0
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index])
}

function isSafeIdentifier(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9._:/-]{1,120}$/.test(value)
}

function isFingerprint(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value)
}

function isCursorPath(value: unknown): value is string {
  if (typeof value !== "string" || value.includes("\\") || value.startsWith("/") || value.includes("..")) return false
  const parts = value.split("/")
  if (parts.length !== 2 || !DATE_RE.test(parts[0]!)) return false
  return CLOSED_RE.test(parts[1]!) || ACTIVE_RE.test(parts[1]!)
}

function decodeCursor(token: string): SnapshotCursor {
  if (
    token.length < 1
    || token.length > MAX_CURSOR_LENGTH
    || !/^[A-Za-z0-9_-]+$/.test(token)
  ) throw new LogsQueryError("INVALID_CURSOR", "cursor 格式无效")
  let value: unknown
  try {
    const decoded = Buffer.from(token, "base64url")
    if (decoded.toString("base64url") !== token) throw new Error("non-canonical cursor")
    value = JSON.parse(decoded.toString("utf8"))
  } catch {
    throw new LogsQueryError("INVALID_CURSOR", "cursor 无法解码")
  }
  if (!isPlainObject(value) || !exactKeys(value, [
    "cursor_version",
    "schema_version",
    "project_fingerprint",
    "selector",
    "filters",
    "files",
    "positions",
    "last_sort_key",
  ])) throw new LogsQueryError("INVALID_CURSOR", "cursor 字段无效")
  const selector = value.selector
  const filters = value.filters
  if (
    value.cursor_version !== CURSOR_VERSION
    || value.schema_version !== 1
    || !isFingerprint(value.project_fingerprint)
    || !isPlainObject(selector)
    || !exactKeys(selector, ["kind", "id"])
    || (selector.kind !== "thread" && selector.kind !== "run")
    || !isSafeIdentifier(selector.id)
    || !isPlainObject(filters)
    || !exactKeys(filters, ["minimum_level", "event", "component"])
    || !["debug", "info", "warn", "error"].includes(String(filters.minimum_level))
    || !(filters.event === null || isSafeIdentifier(filters.event))
    || !(filters.component === null || filters.component === "cli" || filters.component === "agent")
    || !Array.isArray(value.files)
    || value.files.length > 128
    || !Array.isArray(value.positions)
    || value.positions.length > 128
    || !Array.isArray(value.last_sort_key)
    || value.last_sort_key.length !== 7
  ) throw new LogsQueryError("INVALID_CURSOR", "cursor 字段无效")
  const files: CursorFile[] = []
  for (const item of value.files) {
    if (
      !isPlainObject(item)
      || !exactKeys(item, ["path", "watermark", "digest"])
      || !isCursorPath(item.path)
      || !Number.isSafeInteger(item.watermark)
      || Number(item.watermark) < 0
      || !isFingerprint(item.digest)
    ) throw new LogsQueryError("INVALID_CURSOR", "cursor 文件水位无效")
    files.push({ path: item.path, watermark: Number(item.watermark), digest: item.digest })
  }
  const positions: Array<{ path: string; offset: number }> = []
  for (const item of value.positions) {
    if (
      !isPlainObject(item)
      || !exactKeys(item, ["path", "offset"])
      || !isCursorPath(item.path)
      || !Number.isSafeInteger(item.offset)
      || Number(item.offset) < 0
    ) throw new LogsQueryError("INVALID_CURSOR", "cursor source position 无效")
    positions.push({ path: item.path, offset: Number(item.offset) })
  }
  const watermarks = new Map(files.map(file => [file.path, file.watermark]))
  if (positions.some(position => !watermarks.has(position.path) || position.offset > watermarks.get(position.path)!)) {
    throw new LogsQueryError("INVALID_CURSOR", "cursor source position 与文件水位不一致")
  }
  const last = value.last_sort_key
  if (
    !Number.isSafeInteger(last[0])
    || (last[1] !== "cli" && last[1] !== "agent")
    || !Number.isSafeInteger(last[2])
    || !Number.isSafeInteger(last[3])
    || !Number.isSafeInteger(last[4])
    || !isCursorPath(last[5])
    || !Number.isSafeInteger(last[6])
  ) throw new LogsQueryError("INVALID_CURSOR", "cursor 排序键无效")
  return {
    cursor_version: 1,
    schema_version: 1,
    project_fingerprint: value.project_fingerprint,
    selector: { kind: selector.kind, id: selector.id },
    filters: filters as QueryFilters,
    files,
    positions,
    last_sort_key: [
      Number(last[0]),
      last[1],
      Number(last[2]),
      Number(last[3]),
      Number(last[4]),
      last[5],
      Number(last[6]),
    ],
  }
}

function encodeCursor(cursor: SnapshotCursor): string {
  const token = Buffer.from(JSON.stringify(cursor), "utf8").toString("base64url")
  if (token.length > MAX_CURSOR_LENGTH) throw new LogsQueryError("INVALID_CURSOR", "cursor snapshot 过大")
  return token
}

function validateCursorBinding(cursor: SnapshotCursor, query: LogsQuery, project: string): void {
  const selector = cursorSelector(query)
  const filters = queryFilters(query)
  if (
    cursor.project_fingerprint !== project
    || !selector
    || selector.kind !== cursor.selector.kind
    || !cursor.selector.id.startsWith(selector.value)
    || JSON.stringify(filters) !== JSON.stringify(cursor.filters)
  ) throw new LogsQueryError("CURSOR_MISMATCH", "cursor 与当前 workspace、选择器或过滤条件不一致")
}

async function discoverLogFiles(root: string): Promise<DiscoveredFile[]> {
  const results: DiscoveredFile[] = []
  let realRoot: string
  try {
    if (!(await stat(root)).isDirectory()) {
      throw new LogsQueryError("LOG_ROOT_UNREADABLE", "日志根路径不是目录")
    }
    realRoot = await realpath(root)
  } catch (error) {
    if (error instanceof LogsQueryError) throw error
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return results
    throw new LogsQueryError("LOG_ROOT_UNREADABLE", "日志目录不可读取")
  }

  let dateEntries
  try {
    dateEntries = await readdir(root, { withFileTypes: true })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return results
    throw new LogsQueryError("LOG_ROOT_UNREADABLE", "日志目录不可读取")
  }
  const dates = dateEntries
    .filter(entry => entry.isDirectory() && DATE_RE.test(entry.name))
    .map(entry => entry.name)
    .sort()

  for (const date of dates) {
    const directory = join(root, date)
    let entries
    try {
      entries = await readdir(directory, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      if (!entry.isFile()) continue
      const isActive = ACTIVE_RE.test(entry.name)
      const path = join(directory, entry.name)
      try {
        const resolved = await realpath(path)
        const containment = relative(realRoot, resolved)
        if (containment.startsWith("..") || isAbsolute(containment)) continue
        const fileStat = await stat(resolved)
        if (!fileStat.isFile()) continue
        results.push({
          path: resolved,
          relPath: `${date}/${entry.name}`,
          isActive,
          sizeAtQuery: fileStat.size,
        })
      } catch {
        continue
      }
    }
  }
  return results
}

async function snapshotFiles(files: DiscoveredFile[]): Promise<CursorFile[]> {
  const result: CursorFile[] = []
  for (const file of files) {
    let bytes: Buffer
    try {
      bytes = (await readFile(file.path)).subarray(0, file.sizeAtQuery)
    } catch {
      throw new LogsQueryError("CURSOR_EXPIRED", "日志 snapshot 在查询期间发生变化")
    }
    result.push({
      path: file.relPath,
      watermark: file.sizeAtQuery,
      digest: createHash("sha256").update(bytes).digest("hex"),
    })
  }
  return result
}

async function restoreSnapshotFiles(root: string, cursorFiles: CursorFile[]): Promise<DiscoveredFile[]> {
  let realRoot: string
  try {
    realRoot = await realpath(root)
  } catch {
    throw new LogsQueryError("CURSOR_EXPIRED", "cursor 对应的日志目录已不存在")
  }
  const result: DiscoveredFile[] = []
  for (const cursorFile of cursorFiles) {
    const [date, name] = cursorFile.path.split("/") as [string, string]
    const candidate = join(root, date, name)
    try {
      const linkStat = await lstat(candidate)
      if (!linkStat.isFile()) throw new Error("not a regular file")
      const resolved = await realpath(candidate)
      const containment = relative(realRoot, resolved)
      if (containment.startsWith("..") || isAbsolute(containment)) throw new Error("outside log root")
      const fileStat = await stat(resolved)
      if (!fileStat.isFile() || fileStat.size < cursorFile.watermark) throw new Error("snapshot shrank")
      const bytes = (await readFile(resolved)).subarray(0, cursorFile.watermark)
      const digest = createHash("sha256").update(bytes).digest("hex")
      if (digest !== cursorFile.digest) throw new Error("snapshot replaced")
      result.push({
        path: resolved,
        relPath: cursorFile.path,
        isActive: ACTIVE_RE.test(name),
        sizeAtQuery: cursorFile.watermark,
      })
    } catch {
      throw new LogsQueryError("CURSOR_EXPIRED", `cursor snapshot 文件已失效：${cursorFile.path}`)
    }
  }
  return result
}

async function readRecordsSafe(file: DiscoveredFile): Promise<{
  records: SourcedRecord[]
  diagnostics: ParseDiagnostics
}> {
  const records: SourcedRecord[] = []
  const diagnostics = { malformed: 0, invalid: 0, partial: 0, unsupported: 0 }
  let bytes: Buffer
  try {
    bytes = (await readFile(file.path)).subarray(0, file.sizeAtQuery)
  } catch {
    return { records, diagnostics }
  }

  let offset = 0
  while (offset < bytes.length) {
    const newline = bytes.indexOf(0x0a, offset)
    if (newline < 0) {
      if (bytes.subarray(offset).toString("utf8").trim()) diagnostics.partial += 1
      break
    }
    const sourceOffset = offset
    const line = bytes.subarray(offset, newline).toString("utf8").trim()
    offset = newline + 1
    if (!line) continue
    let value: unknown
    try {
      value = JSON.parse(line)
    } catch {
      diagnostics.malformed += 1
      continue
    }
    if ((value as { schema_version?: unknown }).schema_version !== 1) {
      diagnostics.unsupported += 1
      continue
    }
    try {
      assertDiagnosticRecord(value)
      records.push({ record: value, sourcePath: file.relPath, sourceOffset })
    } catch {
      diagnostics.invalid += 1
    }
  }
  return { records, diagnostics }
}

function compareRecords(left: SourcedRecord, right: SourcedRecord): number {
  const a = left.record
  const b = right.record
  return a.timestamp_ms - b.timestamp_ms
    || a.component.localeCompare(b.component)
    || a.process.started_at_ms - b.process.started_at_ms
    || a.process.pid - b.process.pid
    || a.process.record_sequence - b.process.record_sequence
    || left.sourcePath.localeCompare(right.sourcePath)
    || left.sourceOffset - right.sourceOffset
}

async function collectRecords(
  files: DiscoveredFile[],
  project: string,
): Promise<{ records: SourcedRecord[]; diagnostics: ParseDiagnostics }> {
  const records: SourcedRecord[] = []
  const diagnostics = { malformed: 0, invalid: 0, partial: 0, unsupported: 0 }
  for (const file of files) {
    const parsed = await readRecordsSafe(file)
    diagnostics.malformed += parsed.diagnostics.malformed
    diagnostics.invalid += parsed.diagnostics.invalid
    diagnostics.partial += parsed.diagnostics.partial
    diagnostics.unsupported += parsed.diagnostics.unsupported
    records.push(...parsed.records.filter(item => item.record.project_fingerprint === project))
  }
  records.sort(compareRecords)
  return { records, diagnostics }
}

function processKey(record: DiagnosticRecord): string {
  return `${record.component}:${record.process.started_at_ms}:${record.process.pid}`
}

function resolvePrefix(values: Iterable<string>, selector: string, kind: "THREAD" | "RUN"): string {
  const matches = [...new Set(values)].filter(value => value.startsWith(selector)).sort()
  if (matches.length === 0) throw new LogsQueryError(`${kind}_NOT_FOUND`, `${selector} 没有匹配的 ${kind === "THREAD" ? "Thread" : "Run"}`)
  if (matches.length > 1) throw new LogsQueryError(`${kind}_PREFIX_AMBIGUOUS`, `${selector} 匹配多个 ${kind === "THREAD" ? "Thread" : "Run"}`)
  return matches[0]!
}

function eventMatches(record: DiagnosticRecord, query: LogsQuery): boolean {
  const order: Record<DiagnosticLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 }
  if (order[record.level] < order[query.level ?? "info"]) return false
  if (query.component && record.component !== query.component) return false
  if (!query.event) return true
  if (query.event.includes(".")) return record.event === query.event
  return record.event === query.event || record.event.startsWith(`${query.event}.`)
}

function processScopedRecords(records: SourcedRecord[], participants: Set<string>): SourcedRecord[] {
  const bounds = new Map<string, { start: number; stop: number }>()
  for (const item of records) {
    const key = processKey(item.record)
    if (!participants.has(key)) continue
    const current = bounds.get(key) ?? { start: Number.NEGATIVE_INFINITY, stop: Number.POSITIVE_INFINITY }
    if (item.record.event === "process.started") current.start = item.record.timestamp_ms
    if (item.record.event === "process.stopped") current.stop = item.record.timestamp_ms
    bounds.set(key, current)
  }
  return records.filter(item => {
    const record = item.record
    if (record.thread_id || record.run_id || !PROCESS_EVENTS.has(record.event)) return false
    const bound = bounds.get(processKey(record))
    return bound !== undefined && record.timestamp_ms >= bound.start && record.timestamp_ms <= bound.stop
  })
}

function associatedWithThread(records: SourcedRecord[], threadId: string): SourcedRecord[] {
  const direct = records.filter(item => item.record.thread_id === threadId)
  const participants = new Set(direct.map(item => processKey(item.record)))
  return [...direct, ...processScopedRecords(records, participants)].sort(compareRecords)
}

function associatedWithRun(records: SourcedRecord[], runId: string): SourcedRecord[] {
  const direct = records.filter(item => item.record.run_id === runId)
  const participants = new Set(direct.map(item => processKey(item.record)))
  return [...direct, ...processScopedRecords(records, participants)].sort(compareRecords)
}

function fields(record: DiagnosticRecord): Record<string, unknown> {
  return record.fields as Record<string, unknown>
}

function terminalOutcome(record: DiagnosticRecord): string | undefined {
  if (record.event === "run.completed") return "completed"
  if (record.event === "run.failed") return "failed"
  if (record.event === "run.cancelled") return "cancelled"
  return undefined
}

function runRows(records: SourcedRecord[], threadId?: string): RunRow[] {
  const byRun = new Map<string, RunRow>()
  for (const item of records) {
    const record = item.record
    if (!record.run_id || (threadId && record.thread_id !== threadId)) continue
    const current = byRun.get(record.run_id) ?? { run_id: record.run_id, started_at_ms: record.timestamp_ms }
    current.started_at_ms = Math.min(current.started_at_ms, record.timestamp_ms)
    if (record.event === "run.started") {
      const modeValue = fields(record).mode
      if (modeValue === "build" || modeValue === "compose") current.mode = modeValue
    }
    const outcomeValue = terminalOutcome(record)
    if (outcomeValue) {
      current.outcome = outcomeValue
      const durationValue = fields(record).duration_ms
      if (typeof durationValue === "number") current.duration_ms = durationValue
    }
    byRun.set(record.run_id, current)
  }
  return [...byRun.values()].sort((a, b) => a.started_at_ms - b.started_at_ms || a.run_id.localeCompare(b.run_id))
}

function threadList(records: SourcedRecord[]): NonNullable<LogsQueryResult["threads"]> {
  const ids = [...new Set(records.flatMap(item => item.record.thread_id ? [item.record.thread_id] : []))]
  return ids.map(threadId => {
    const related = records.filter(item => item.record.thread_id === threadId)
    const runs = runRows(records, threadId)
    const latest = runs.at(-1)
    return {
      thread_id: threadId,
      last_activity_ms: Math.max(...related.map(item => item.record.timestamp_ms)),
      run_count: runs.length,
      ...(latest?.mode ? { latest_mode: latest.mode } : {}),
      ...(latest?.outcome ? { latest_outcome: latest.outcome } : {}),
      ...(latest?.duration_ms !== undefined ? { latest_duration_ms: latest.duration_ms } : {}),
    }
  }).sort((a, b) => b.last_activity_ms - a.last_activity_ms || a.thread_id.localeCompare(b.thread_id))
}

function queryDiagnostics(files: DiscoveredFile[], parsed: ParseDiagnostics): LogsQueryResult["diagnostics"] {
  const active = files.filter(file => file.isActive)
  return {
    malformed_count: parsed.malformed,
    invalid_count: parsed.invalid,
    unsupported_schema_count: parsed.unsupported,
    partial_line_count: parsed.partial,
    orphan_active_count: active.length,
    orphan_active_bytes: active.reduce((total, file) => total + file.sizeAtQuery, 0),
  }
}

function cursorPositions(records: SourcedRecord[], lastSortKey: RecordSortKey): Array<{ path: string; offset: number }> {
  const positions = new Map<string, number>()
  for (const item of records) {
    if (compareSortKey(recordSortKey(item), lastSortKey) > 0) break
    positions.set(item.sourcePath, Math.max(positions.get(item.sourcePath) ?? 0, item.sourceOffset))
  }
  return [...positions].sort(([left], [right]) => left.localeCompare(right)).map(([path, offset]) => ({ path, offset }))
}

function outputBytes(result: LogsQueryResult, query: LogsQuery): number {
  const output = query.json ? JSON.stringify(result, null, 2) : renderLogsHuman(result, query, 88)
  return Buffer.byteLength(`${output}\n`, "utf8")
}

export async function queryLogs(query: LogsQuery, rootOverride?: string): Promise<LogsQueryResult> {
  if (query.thread && query.run) throw new LogsQueryError("THREAD_RUN_CONFLICT", "--thread 与 --run 不能同时使用")
  if (query.flat && !query.thread && !query.run) throw new LogsQueryError("FLAT_SELECTOR_REQUIRED", "--flat 只能与 --thread 或 --run 一起使用")
  if (query.flat && query.json) throw new LogsQueryError("FLAT_JSON_CONFLICT", "--flat 与 --json 不能同时使用")
  if (query.cursor && !query.thread && !query.run) throw new LogsQueryError("CURSOR_MISMATCH", "--cursor 只能与 --thread 或 --run 一起使用")
  if (query.cursor && !query.flat && !query.json) throw new LogsQueryError("CURSOR_REQUIRES_FLAT_OR_JSON", "--cursor 只能用于 --flat 或 --json")
  const project = await getProjectFingerprint(query.cwd)
  const root = rootOverride ?? getDefaultLogRoot()
  const decodedCursor = query.cursor ? decodeCursor(query.cursor) : undefined
  if (decodedCursor) validateCursorBinding(decodedCursor, query, project)
  const files = decodedCursor
    ? await restoreSnapshotFiles(root, decodedCursor.files)
    : await discoverLogFiles(root)
  const collected = await collectRecords(files, project)
  const base = {
    query_schema_version: 1 as const,
    project_fingerprint: project,
    filters: queryFilters(query),
    diagnostics: queryDiagnostics(files, collected.diagnostics),
  }

  if (!query.thread && !query.run) {
    const matched = threadList(collected.records)
    const threads = matched.slice(0, query.limit)
    const makeResult = (): LogsQueryResult => ({
      ...base,
      threads: [...threads],
      events: [],
      matched_count: matched.length,
      returned_count: threads.length,
      truncated: matched.length > threads.length,
      next_cursor: null,
    })
    let result = makeResult()
    while (threads.length > 0 && outputBytes(result, query) > MAX_QUERY_OUTPUT_BYTES) {
      threads.pop()
      result = makeResult()
    }
    if (outputBytes(result, query) > MAX_QUERY_OUTPUT_BYTES) {
      throw new LogsQueryError("QUERY_OUTPUT_TOO_LARGE", "查询摘要超过 256 KiB")
    }
    assertDiagnosticQueryResult(result)
    return result
  }

  const allRecords = collected.records
  const selectedThread = decodedCursor?.selector.kind === "thread"
    ? decodedCursor.selector.id
    : query.thread
    ? resolvePrefix(allRecords.flatMap(item => item.record.thread_id ? [item.record.thread_id] : []), query.thread, "THREAD")
    : undefined
  const selectedRun = decodedCursor?.selector.kind === "run"
    ? decodedCursor.selector.id
    : query.run
    ? resolvePrefix(allRecords.flatMap(item => item.record.run_id ? [item.record.run_id] : []), query.run, "RUN")
    : undefined
  const associated = selectedThread
    ? associatedWithThread(allRecords, selectedThread)
    : associatedWithRun(allRecords, selectedRun!)
  const matched = associated.filter(item => eventMatches(item.record, query))
  const eligible = decodedCursor
    ? matched.filter(item => compareSortKey(recordSortKey(item), decodedCursor.last_sort_key) > 0)
    : matched
  const summary = selectedThread
    ? (() => {
        const runs = runRows(allRecords, selectedThread)
        return {
          run_count: runs.length,
          ...(runs.at(-1)?.outcome ? { outcome: runs.at(-1)!.outcome } : {}),
          runs: runs.slice(-1000).map(({ run_id, mode, outcome, duration_ms }) => ({
            run_id,
            ...(mode ? { mode } : {}),
            ...(outcome ? { outcome } : {}),
            ...(duration_ms !== undefined ? { duration_ms } : {}),
          })),
        }
      })()
    : runSummary(associated)
  const snapshot = decodedCursor?.files ?? await snapshotFiles(
    files.filter(file => associated.some(item => item.sourcePath === file.relPath)),
  )
  const selected = eligible.slice(0, query.limit)
  const makeResult = (): LogsQueryResult => {
    const hasMore = eligible.length > selected.length
    const last = selected.at(-1)
    const nextCursor = hasMore && last && (query.flat || query.json)
      ? encodeCursor({
          cursor_version: 1,
          schema_version: 1,
          project_fingerprint: project,
          selector: selectedThread
            ? { kind: "thread", id: selectedThread }
            : { kind: "run", id: selectedRun! },
          filters: queryFilters(query),
          files: snapshot,
          positions: cursorPositions(associated, recordSortKey(last)),
          last_sort_key: recordSortKey(last),
        })
      : null
    return {
      ...base,
      ...(selectedThread ? { thread_id: selectedThread } : { run_id: selectedRun }),
      summary,
      events: selected.map(item => item.record),
      matched_count: matched.length,
      returned_count: selected.length,
      truncated: hasMore,
      next_cursor: nextCursor,
    }
  }
  let result = makeResult()
  while (selected.length > 0 && outputBytes(result, query) > MAX_QUERY_OUTPUT_BYTES) {
    selected.pop()
    result = makeResult()
  }
  if (outputBytes(result, query) > MAX_QUERY_OUTPUT_BYTES || (eligible.length > 0 && selected.length === 0)) {
    throw new LogsQueryError("QUERY_OUTPUT_TOO_LARGE", "单条完整日志记录无法放入 256 KiB 输出")
  }
  assertDiagnosticQueryResult(result)
  return result
}

function runSummary(records: SourcedRecord[]): NonNullable<LogsQueryResult["summary"]> {
  const terminal = [...records].reverse().find(item => terminalOutcome(item.record))?.record
  if (!terminal) return {}
  const value = fields(terminal)
  return {
    outcome: terminalOutcome(terminal),
    ...(typeof value.duration_ms === "number" ? { duration_ms: value.duration_ms } : {}),
    ...(typeof value.active_ms === "number" ? { active_ms: value.active_ms } : {}),
    ...(typeof value.interaction_wait_ms === "number" ? { interaction_wait_ms: value.interaction_wait_ms } : {}),
    ...(typeof value.retry_wait_ms === "number" ? { retry_wait_ms: value.retry_wait_ms } : {}),
    ...(typeof value.first_visible_activity_ms === "number" || value.first_visible_activity_ms === null
      ? { first_visible_activity_ms: value.first_visible_activity_ms }
      : {}),
  }
}

function formatDuration(value: unknown): string {
  if (typeof value !== "number") return "-"
  if (value < 1000) return `${value}ms`
  return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)}s`
}

function formatOutcome(value: unknown): string {
  return value === "completed" ? "完成" : value === "failed" ? "失败" : value === "cancelled" ? "取消" : "未知"
}

function formatMode(value: unknown): string {
  return value === "compose" ? "编排" : "构建"
}

function prefix(value: string | undefined): string {
  return value ? value.slice(0, 12) : "?"
}

const ANSI_REGEX = /\u001b\[[0-9;]*[a-zA-Z]/g

export function stripAnsi(text: string): string {
  return text.replace(ANSI_REGEX, "")
}

function isWideCodePoint(code: number): boolean {
  return code >= 0x1100 && (
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
}

/** 按终端 cell 而非 UTF-16 长度计算宽度，保证中文列与 ASCII 列对齐（并剥离 ANSI 样式）。 */
function displayWidth(value: string): number {
  const plain = stripAnsi(value)
  let width = 0
  for (const character of plain) {
    const code = character.codePointAt(0) ?? 0
    if (code <= 0x1f || (code >= 0x7f && code <= 0x9f) || /\p{Mark}/u.test(character)) continue
    width += isWideCodePoint(code) ? 2 : 1
  }
  return width
}

function truncateDisplay(value: string, width: number): string {
  if (width <= 0) return ""
  if (displayWidth(value) <= width) return value
  if (!ANSI_REGEX.test(value)) {
    let result = ""
    let used = 0
    for (const character of value) {
      const characterWidth = displayWidth(character)
      if (used + characterWidth > width - 1) break
      result += character
      used += characterWidth
    }
    return `${result}…`
  }
  let result = ""
  let used = 0
  let insideEscape = false
  let currentEscape = ""
  for (let i = 0; i < value.length; i++) {
    const char = value[i]!
    if (char === "\u001b") {
      insideEscape = true
      currentEscape = char
      continue
    }
    if (insideEscape) {
      currentEscape += char
      if (/[a-zA-Z]/.test(char)) {
        insideEscape = false
        result += currentEscape
        currentEscape = ""
      }
      continue
    }
    const charWidth = displayWidth(char)
    if (used + charWidth > width - 1) break
    result += char
    used += charWidth
  }
  return `${result}…\u001b[0m`
}

function cell(value: unknown, width: number, align: "left" | "right" = "left"): string {
  const text = truncateDisplay(String(value), width)
  const visible = displayWidth(text)
  const padding = " ".repeat(Math.max(0, width - visible))
  return align === "right" ? `${padding}${text}` : `${text}${padding}`
}

function tableRow(columns: Array<{ value: unknown; width: number; align?: "left" | "right" }>): string {
  return columns.map(column => cell(column.value, column.width, column.align)).join("  ").trimEnd()
}

function detailLine(label: string, value: string, columns: number): string {
  return `${cell(label, 10)}${truncateDisplay(value, Math.max(1, columns - 10))}`.trimEnd()
}

export type RenderLogsOptions = {
  color?: boolean
}

function createFormatter(options?: RenderLogsOptions) {
  const color = Boolean(options?.color)
  const wrap = (code: string, text: string) => color ? `\u001b[${code}m${text}\u001b[0m` : text
  return {
    color,
    bold: (t: string) => wrap("1", t),
    dim: (t: string) => wrap("2", t),
    gray: (t: string) => wrap("90", t),
    red: (t: string) => wrap("31", t),
    green: (t: string) => wrap("32", t),
    yellow: (t: string) => wrap("33", t),
    blue: (t: string) => wrap("34", t),
    magenta: (t: string) => wrap("35", t),
    cyan: (t: string) => wrap("36", t),
    duration: (ms: unknown) => {
      const formatted = formatDuration(ms)
      if (!color || typeof ms !== "number" || ms <= 0) return color ? wrap("90", formatted) : formatted
      if (ms >= 10000) return wrap("31;1", formatted)
      if (ms >= 2000) return wrap("33", formatted)
      if (ms >= 500) return wrap("36", formatted)
      return wrap("90", formatted)
    },
    status: (outcome: unknown) => {
      const text = formatOutcome(outcome)
      if (!color) return text
      if (outcome === "completed" || outcome === "success") return wrap("32", `✓ ${text}`)
      if (outcome === "failed" || outcome === "error") return wrap("31;1", `✖ ${text}`)
      if (outcome === "cancelled") return wrap("33", `⊘ ${text}`)
      return text
    },
    levelBadge: (level: string) => {
      const upper = level.toUpperCase()
      if (!color) return upper
      if (upper === "ERROR") return wrap("31;1", upper)
      if (upper === "WARN") return wrap("33", upper)
      if (upper === "INFO") return wrap("36", upper)
      return wrap("90", upper)
    },
    mode: (mode: unknown) => {
      const text = formatMode(mode)
      return color ? wrap("35", text) : text
    },
    divider: (width: number) => {
      const line = "─".repeat(Math.max(1, width))
      return color ? wrap("90", line) : line
    },
  }
}

function workLine(label: string, detail: string, duration: unknown, columns: number, fmt?: ReturnType<typeof createFormatter>): string {
  const durationText = fmt ? fmt.duration(duration) : formatDuration(duration)
  const durationWidth = 8
  const available = Math.max(20, columns - durationWidth - 4)
  const detailNeeded = displayWidth(detail)
  const labelNeeded = displayWidth(label)

  let labelWidth: number
  let detailWidth: number

  if (labelNeeded + detailNeeded <= available) {
    labelWidth = Math.max(labelNeeded, Math.min(available - detailNeeded, Math.floor(available * 0.62)))
    detailWidth = available - labelWidth
  } else {
    const maxDetail = Math.min(detailNeeded, Math.floor(available * 0.35))
    detailWidth = Math.max(6, maxDetail)
    labelWidth = available - detailWidth
  }

  return tableRow([
    { value: label, width: labelWidth },
    { value: detail, width: detailWidth },
    { value: durationText, width: durationWidth, align: "right" },
  ])
}

function mcpToolName(value: Record<string, unknown>): string {
  const server = String(value.server_name ?? "unknown")
  let tool = String(value.tool_name ?? "unknown")
  if (tool.startsWith(`${server}_`)) {
    tool = tool.slice(server.length + 1)
  } else if (tool.startsWith(`${server}.`)) {
    tool = tool.slice(server.length + 1)
  }
  return `${server}.${tool}`
}

function eventLabel(record: DiagnosticRecord): string {
  const value = fields(record)
  if (record.event.startsWith("tool.")) {
    const tool = value.tool_kind === "mcp" && value.server_name
      ? `MCP 工具 ${mcpToolName(value)}`
      : `工具 ${value.tool_name ?? "unknown"}`
    return tool
  }
  if (record.event.startsWith("model.")) return `模型回合 #${value.model_round ?? "?"} / 尝试 #${value.provider_attempt ?? "?"}`
  if (record.event.startsWith("context.compaction.")) return "压缩"
  if (record.event.startsWith("mcp.connection.")) return `MCP ${value.server_name ?? "unknown"}`
  if (record.event === "ipc.initialize.completed") return "Agent 初始化"
  if (record.event === "runtime.acquire.completed") return "运行时准备"
  if (record.event === "context.build.completed") return "上下文构建"
  if (record.event.startsWith("interaction.")) return "用户交互"
  if (record.event.startsWith("persistence.operation.")) return `持久化 ${value.operation ?? "操作"}`
  if (record.event.startsWith("run.")) return "运行"
  return "步骤"
}

type HumanWorkItem = { label: string; detail: string; duration: unknown; depth?: number }

function preparationLines(events: DiagnosticRecord[]): HumanWorkItem[] {
  return events.flatMap(record => {
    const value = fields(record)
    if (record.event === "mcp.connection.completed") {
      return [{ label: `MCP ${value.server_name}`, detail: `${String(value.transport).toUpperCase()} · ${value.tool_count} 工具 · 已连接`, duration: value.duration_ms }]
    }
    if (record.event === "mcp.connection.failed") {
      return [{ label: `MCP ${value.server_name}`, detail: `失败 · ${value.summary_code ?? value.error_type}`, duration: value.duration_ms }]
    }
    if (record.event === "mcp.connection.closed") return [{ label: `MCP ${value.server_name}`, detail: "已关闭", duration: value.duration_ms }]
    if (record.event === "ipc.initialize.completed") return [{ label: "Agent 初始化", detail: "完成", duration: value.duration_ms }]
    return []
  })
}

function processItem(record: DiagnosticRecord, depth = 0): HumanWorkItem | undefined {
  const value = fields(record)
  if (record.event === "catalog.bound") return { label: "目录", detail: `Skill ${value.skill_count} · MCP ${value.mcp_count} · Plugin ${value.plugin_count}`, duration: undefined, depth }
  if (record.event === "skill.read") return { label: `Skill 读取 ${value.skill_id}`, detail: value.kind === "body" ? "正文" : "资源", duration: undefined, depth }
  if (record.event === "hook.completed") {
    const outcome = value.outcome === "allow" ? "允许" : value.outcome === "deny" ? "拒绝" : "完成"
    return { label: `Hook ${value.hook_event}`, detail: `${value.plugin_id} · ${value.tool_name} · ${outcome}`, duration: value.duration_ms, depth }
  }
  if (record.event === "hook.failed") return { label: `Hook ${value.hook_event}`, detail: `${value.plugin_id} · ${value.tool_name} · 失败`, duration: value.duration_ms, depth }
  if (record.event === "context.build.completed") return { label: "上下文", detail: `${value.message_count} 消息 · ${value.estimated_tokens} token · ${value.cache_status === "hit" ? "缓存命中" : "缓存未命中"}`, duration: value.duration_ms, depth }
  if (record.event === "context.compaction.completed") {
    const trigger = value.trigger === "manual" ? "手动压缩" : value.trigger === "overflow" ? "溢出压缩" : "自动压缩"
    return { label: "上下文压缩", detail: `${trigger} · ${value.before_estimated_tokens} → ${value.after_estimated_tokens} token · ${value.artifact_count} 产物`, duration: value.duration_ms, depth }
  }
  if (record.event === "context.compaction.failed") return { label: "上下文压缩", detail: `失败 · ${value.summary_code}`, duration: value.duration_ms, depth }
  if (record.event === "model.completed") return { label: `模型回合 #${value.model_round} / 尝试 #${value.provider_attempt}`, detail: "完成", duration: value.duration_ms, depth }
  if (record.event === "model.failed") return { label: `模型回合 #${value.model_round} / 尝试 #${value.provider_attempt}`, detail: `失败 · ${value.summary_code}`, duration: value.duration_ms, depth }
  if (record.event === "tool.completed") return { label: eventLabel(record), detail: "完成", duration: value.duration_ms, depth }
  if (record.event === "tool.failed") return { label: eventLabel(record), detail: `失败 · ${value.error_code ?? value.summary_code}`, duration: value.duration_ms, depth }
  return undefined
}

function runProcessLines(events: DiagnosticRecord[], runId: string): HumanWorkItem[] {
  const runEvents = events.filter(record => record.run_id === runId)
  const executionStarts = new Map<string, DiagnosticRecord>()
  for (const record of runEvents) {
    const value = fields(record)
    if (
      record.event === "execution.started"
      && typeof record.execution_id === "string"
      && (value.kind === "child" || value.kind === "compose_stage")
    ) executionStarts.set(record.execution_id, record)
  }
  const rawItems: HumanWorkItem[] = []
  for (const record of runEvents) {
    const executionId = record.execution_id
    const start = typeof executionId === "string" ? executionStarts.get(executionId) : undefined
    if (start && record !== start) continue
    if (start && executionId) {
      const startedFields = fields(start)
      const terminal = runEvents.find(candidate => (
        candidate.execution_id === executionId
        && (candidate.event === "execution.completed" || candidate.event === "execution.failed")
      ))
      const terminalFields = terminal ? fields(terminal) : {}
      const label = startedFields.kind === "compose_stage"
        ? `阶段 ${startedFields.agent_id}`
        : `子代理 ${startedFields.agent_id}`
      const detail = terminal?.event === "execution.failed"
        ? `失败 · ${terminalFields.summary_code ?? terminalFields.error_type}`
        : terminal?.event === "execution.completed"
        ? "完成"
        : "运行中"
      rawItems.push({ label, detail, duration: terminalFields.duration_ms, depth: 0 })
      for (const childRecord of runEvents) {
        if (childRecord.execution_id !== executionId || childRecord.event.startsWith("execution.")) continue
        const child = processItem(childRecord, 1)
        if (child) rawItems.push(child)
      }
      continue
    }
    const item = processItem(record)
    if (item) rawItems.push(item)
  }

  const result: HumanWorkItem[] = []
  for (let i = 0; i < rawItems.length; i++) {
    const current = rawItems[i]!
    if (
      current.depth === 0
      && current.label.startsWith("工具 ")
      && current.detail === "完成"
      && (typeof current.duration !== "number" || current.duration < 50)
    ) {
      const toolCounts = new Map<string, number>()
      let totalCount = 0
      let totalDuration = 0
      let j = i
      while (
        j < rawItems.length
        && rawItems[j]!.depth === 0
        && rawItems[j]!.label.startsWith("工具 ")
        && rawItems[j]!.detail === "完成"
        && (typeof rawItems[j]!.duration !== "number" || (rawItems[j]!.duration as number) < 50)
      ) {
        const name = rawItems[j]!.label.slice(3).trim()
        toolCounts.set(name, (toolCounts.get(name) ?? 0) + 1)
        totalCount++
        const dur = rawItems[j]!.duration
        if (typeof dur === "number") totalDuration += dur
        j++
      }
      if (totalCount >= 3) {
        const summary = Array.from(toolCounts.entries())
          .map(([name, count]) => `${name} ×${count}`)
          .join(", ")
        result.push({
          label: `本地工具 ${summary}`,
          detail: "完成",
          duration: totalDuration,
          depth: 0,
        })
        i = j - 1
      } else {
        result.push(current)
      }
    } else {
      result.push(current)
    }
  }
  return result
}

function latestError(events: DiagnosticRecord[]): DiagnosticRecord | undefined {
  return [...events].reverse().find(record => record.level === "error")
}

function miniBar(value: number, max: number, width: number = 10, color: boolean = false): string {
  if (max <= 0) return "░".repeat(width)
  const ratio = Math.min(1, Math.max(0, value / max))
  const filled = Math.round(ratio * width)
  const bar = `${"█".repeat(filled)}${"░".repeat(width - filled)}`
  return color ? `\u001b[36m${"█".repeat(filled)}\u001b[0m\u001b[90m${"░".repeat(width - filled)}\u001b[0m` : bar
}

function slowLines(events: DiagnosticRecord[], runId: string | undefined, columns: number, fmt: ReturnType<typeof createFormatter>): string[] {
  const items = events
    .filter(record => (!runId || record.run_id === runId || (!record.run_id && PROCESS_EVENTS.has(record.event))))
    .filter(record => !record.event.startsWith("logging.") && !record.event.startsWith("process."))
    .filter(record => !record.event.startsWith("run.") && record.event !== "runtime.released" && record.event !== "mcp.connection.closed")
    .map(record => ({ record, value: fields(record).duration_ms }))
    .filter((item): item is { record: DiagnosticRecord; value: number } => typeof item.value === "number" && item.value > 0)
    .filter(item => eventLabel(item.record) !== "步骤")
    .sort((a, b) => (b.value as number) - (a.value as number))
    .slice(0, 5)

  if (!items.length) return []
  const maxVal = items[0]?.value ?? 1

  return items.map(item => {
    const durText = fmt.duration(item.value)
    const labelText = eventLabel(item.record)
    if (columns >= 68) {
      const pct = `${Math.round((item.value / maxVal) * 100)}%`
      const bar = miniBar(item.value, maxVal, 10, fmt.color)
      const labelWidth = Math.max(1, columns - 32)
      return tableRow([
        { value: durText, width: 8, align: "right" },
        { value: bar, width: 10 },
        { value: fmt.color ? fmt.gray(pct) : pct, width: 4, align: "right" },
        { value: labelText, width: labelWidth },
      ])
    }
    return tableRow([
      { value: durText, width: 8, align: "right" },
      { value: labelText, width: Math.max(1, columns - 10) },
    ])
  })
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KiB`
}

function diagnosticSummary(result: LogsQueryResult): string | undefined {
  const value = result.diagnostics
  const items: string[] = []
  if (value.malformed_count) items.push(`损坏 JSON ${value.malformed_count}`)
  if (value.invalid_count) items.push(`无效记录 ${value.invalid_count}`)
  if (value.unsupported_schema_count) items.push(`不支持版本 ${value.unsupported_schema_count}`)
  if (value.partial_line_count) items.push(`不完整末行 ${value.partial_line_count}`)
  if (value.orphan_active_count) items.push(`未正常关闭文件 ${value.orphan_active_count}（${formatBytes(value.orphan_active_bytes)}）`)
  return items.length ? items.join(" · ") : undefined
}

function nextPageCommand(result: LogsQueryResult, query: LogsQuery): string | undefined {
  if (!result.next_cursor) return undefined
  const selector = result.thread_id
    ? `--thread ${result.thread_id}`
    : `--run ${result.run_id}`
  const filters = [
    query.level ? `--level ${query.level}` : "",
    query.event ? `--event ${query.event}` : "",
    query.component ? `--component ${query.component}` : "",
  ].filter(Boolean).join(" ")
  return `harness logs ${selector} --flat --limit ${query.limit}${filters ? ` ${filters}` : ""} --cursor ${result.next_cursor}`
}

function flatSwitchCommand(result: LogsQueryResult, query: LogsQuery): string | undefined {
  if (!result.truncated || query.flat || query.json) return undefined
  const selector = result.thread_id
    ? `--thread ${result.thread_id}`
    : `--run ${result.run_id}`
  const filters = [
    query.level ? `--level ${query.level}` : "",
    query.event ? `--event ${query.event}` : "",
    query.component ? `--component ${query.component}` : "",
  ].filter(Boolean).join(" ")
  return `harness logs ${selector} --flat --limit ${query.limit}${filters ? ` ${filters}` : ""}`
}

function flatFailureCode(value: Record<string, unknown>): string {
  return String(value.error_code ?? value.summary_code ?? value.error_type ?? "FAILED")
}

/** 将白名单事件字段收敛为可扫描的单行详情，不展示原始记录或自由文本。 */
function flatEventDetail(record: DiagnosticRecord): string {
  const value = fields(record)
  if (record.event === "run.started") return `${formatMode(value.mode)} · 开始`
  if (record.event === "run.completed") return "完成"
  if (record.event === "run.cancelled") return "取消"
  if (record.event === "run.failed") return `失败 · ${flatFailureCode(value)}`
  if (record.event === "model.started") return `回合 #${value.model_round} · 尝试 #${value.provider_attempt} · 开始`
  if (record.event === "model.completed") return `回合 #${value.model_round} · 尝试 #${value.provider_attempt} · 完成`
  if (record.event === "model.retry_scheduled") return `回合 #${value.model_round} · 尝试 #${value.provider_attempt} · 等待重试`
  if (record.event === "model.failed") return `回合 #${value.model_round} · 尝试 #${value.provider_attempt} · 失败 · ${flatFailureCode(value)}`
  if (record.event.startsWith("tool.")) {
    const tool = value.tool_kind === "mcp" && value.server_name
      ? `MCP ${mcpToolName(value)}`
      : `工具 ${value.tool_name ?? "unknown"}`
    if (record.event === "tool.started") return `${tool} · 开始`
    if (record.event === "tool.completed") return `${tool} · 完成`
    return `${tool} · 失败 · ${flatFailureCode(value)}`
  }
  if (record.event === "mcp.connection.completed") return `MCP ${value.server_name} · 已连接`
  if (record.event === "mcp.connection.failed") return `MCP ${value.server_name} · 失败 · ${flatFailureCode(value)}`
  if (record.event === "mcp.connection.closed") return `MCP ${value.server_name} · 已关闭`
  if (record.event === "catalog.bound") return `目录 · Skill ${value.skill_count} · MCP ${value.mcp_count} · Plugin ${value.plugin_count}`
  if (record.event === "skill.read") return `Skill ${value.skill_id} · ${value.kind === "body" ? "正文" : "资源"}`
  if (record.event.startsWith("hook.")) {
    const hook = `Hook ${value.hook_event} · ${value.plugin_id} · ${value.tool_name}`
    if (record.event === "hook.started") return `${hook} · 开始`
    if (record.event === "hook.failed") return `${hook} · 失败 · ${flatFailureCode(value)}`
    const outcome = value.outcome === "allow" ? "允许" : value.outcome === "deny" ? "拒绝" : "完成"
    return `${hook} · ${outcome}`
  }
  if (record.event.startsWith("execution.")) {
    const kind = value.kind === "compose_stage" ? "阶段" : value.kind === "child" ? "子代理" : "执行"
    if (record.event === "execution.started") return `${kind} ${value.agent_id} · 开始`
    if (record.event === "execution.completed") return `${kind} ${value.agent_id} · 完成`
    return `${kind} ${value.agent_id} · 失败 · ${flatFailureCode(value)}`
  }
  if (record.event === "context.build.completed") return `上下文 · ${value.message_count} 消息 · ${value.estimated_tokens} token`
  if (record.event === "context.compaction.completed") return `上下文压缩 · ${value.trigger} · ${value.before_estimated_tokens} → ${value.after_estimated_tokens} token`
  if (record.event === "context.compaction.failed") return `上下文压缩 · 失败 · ${flatFailureCode(value)}`
  if (record.event === "ipc.initialize.completed") return "Agent 初始化 · 完成"
  if (record.event === "runtime.acquire.completed") return `运行时准备 · ${value.source === "reused" ? "复用" : "新建"}`
  const label = eventLabel(record)
  if (label === "步骤") return "-"
  if (record.event.endsWith(".started")) return `${label} · 开始`
  if (record.event.endsWith(".completed")) return `${label} · 完成`
  if (record.event.endsWith(".failed")) return `${label} · 失败 · ${flatFailureCode(value)}`
  return label
}

function flatFilterSummary(query: LogsQuery): string {
  return [
    `${(query.level ?? "info").toUpperCase()}+`,
    query.component ?? "全部组件",
    query.event ?? "全部事件",
  ].join(" · ")
}

function flatTime(timestampMs: number): string {
  return new Date(timestampMs).toISOString().slice(11, 23)
}

function renderLogsFlat(result: LogsQueryResult, query: LogsQuery, columns: number, fmt: ReturnType<typeof createFormatter>): string {
  const events = result.events as DiagnosticRecord[]
  const ownerThread = events.find(record => record.thread_id)?.thread_id
  const title = fmt.bold("HARNESS LOGS · FLAT")
  const lines = [title]
  if (result.thread_id) lines.push(detailLine("Thread", result.thread_id, columns))
  if (result.run_id) {
    lines.push(detailLine("Run", result.run_id, columns))
    if (ownerThread) lines.push(detailLine("Thread", ownerThread, columns))
  }
  lines.push(detailLine("筛选", flatFilterSummary(query), columns), "")

  if (columns >= 84) {
    const longestEvent = Math.max(0, ...events.map(record => displayWidth(record.event)))
    const eventWidth = Math.min(30, Math.max(20, Math.min(longestEvent, columns - 58)))
    const detailWidth = columns - 40 - eventWidth
    lines.push(tableRow([
      { value: fmt.bold("时间"), width: 12 },
      { value: fmt.bold("级别"), width: 5 },
      { value: fmt.bold("来源"), width: 5 },
      { value: fmt.bold("EVENT"), width: eventWidth },
      { value: fmt.bold("详情"), width: detailWidth },
      { value: fmt.bold("耗时"), width: 8, align: "right" },
    ]))
    for (const record of events) {
      lines.push(tableRow([
        { value: fmt.gray(flatTime(record.timestamp_ms)), width: 12 },
        { value: fmt.levelBadge(record.level), width: 5 },
        { value: record.component, width: 5 },
        { value: record.event, width: eventWidth },
        { value: flatEventDetail(record), width: detailWidth },
        { value: fmt.duration(fields(record).duration_ms), width: 8, align: "right" },
      ]))
    }
  } else {
    const eventWidth = Math.max(1, columns - 28)
    for (const record of events) {
      lines.push(tableRow([
        { value: fmt.gray(flatTime(record.timestamp_ms)), width: 12 },
        { value: fmt.levelBadge(record.level), width: 5 },
        { value: record.component, width: 5 },
        { value: record.event, width: eventWidth },
      ]))
      const duration = formatDuration(fields(record).duration_ms)
      const detail = `${flatEventDetail(record)}${duration !== "-" ? ` · ${duration}` : ""}`
      lines.push(`  ${truncateDisplay(detail, columns - 2)}`)
    }
  }

  lines.push("", "记录", `共 ${result.matched_count} 条 · 已显示 ${result.returned_count} 条${result.truncated ? " · 已截断" : ""}`)
  const nextCommand = nextPageCommand(result, query)
  if (nextCommand) lines.push("", "下一页", nextCommand)
  const diagnostics = diagnosticSummary(result)
  if (diagnostics) lines.push(detailLine("诊断", diagnostics, columns))
  return lines.map(line => line === nextCommand ? line : truncateDisplay(line, columns)).join("\n")
}

export function renderLogsHuman(
  result: LogsQueryResult,
  query: LogsQuery,
  requestedColumns: number = 88,
  options?: RenderLogsOptions,
): string {
  const columns = Math.max(48, Math.min(120, requestedColumns))
  const fmt = createFormatter(options)

  if (!query.thread && !query.run) {
    const threads = result.threads ?? []
    const lines = [fmt.bold("HARNESS LOGS"), `最近 Thread · ${threads.length}`, ""]
    if (columns >= 60) {
      lines.push(tableRow([
        { value: fmt.bold("最后活动"), width: 16 },
        { value: fmt.bold("THREAD"), width: 12 },
        { value: fmt.bold("次数"), width: 4, align: "right" },
        { value: fmt.bold("模式"), width: 4 },
        { value: fmt.bold("结果"), width: 4 },
        { value: fmt.bold("耗时"), width: 8, align: "right" },
      ]))
    }
    for (const thread of threads) {
      const timestamp = new Date(thread.last_activity_ms).toISOString().replace("T", " ").slice(0, 16)
      if (columns >= 60) {
        lines.push(tableRow([
          { value: fmt.gray(timestamp), width: 16 },
          { value: prefix(thread.thread_id), width: 12 },
          { value: thread.run_count, width: 4, align: "right" },
          { value: fmt.mode(thread.latest_mode), width: 4 },
          { value: fmt.status(thread.latest_outcome), width: 4 },
          { value: fmt.duration(thread.latest_duration_ms), width: 8, align: "right" },
        ]))
      } else {
        lines.push(`${fmt.gray(timestamp.slice(5))}  ${prefix(thread.thread_id)}`)
        lines.push(`  ${thread.run_count} 次 · ${fmt.mode(thread.latest_mode)} · ${fmt.status(thread.latest_outcome)} · ${fmt.duration(thread.latest_duration_ms)}`)
      }
    }
    if (threads.length === 0) lines.push("本工作区暂无诊断日志")
    lines.push("", "提示  harness logs --thread <prefix> 查看详情")
    const diagnostics = diagnosticSummary(result)
    if (diagnostics) lines.push("", "诊断", detailLine("日志", diagnostics, columns))
    return lines.map(line => truncateDisplay(line, columns)).join("\n")
  }

  if (query.flat) return renderLogsFlat(result, query, columns, fmt)

  const events = result.events
  const selectedRuns = query.thread
    ? ((result.summary && "runs" in result.summary) ? result.summary.runs : [])
    : runRows(events.map((record, index) => ({ record: record as DiagnosticRecord, sourcePath: "", sourceOffset: index })))
  const latestRun = selectedRuns.at(-1)
  const ownerThread = (events as DiagnosticRecord[]).find(record => record.run_id === result.run_id)?.thread_id
  const lines = [fmt.bold("HARNESS LOGS")]
  if (query.thread) {
    lines.push(detailLine("Thread", String(result.thread_id), columns))
    lines.push(detailLine("概览", `${selectedRuns.length} 次运行 · 最近${formatOutcome(latestRun?.outcome)}`, columns))
  } else {
    lines.push(detailLine("Run", String(result.run_id), columns))
    if (ownerThread) lines.push(detailLine("Thread", ownerThread, columns))
    lines.push(detailLine("概览", `${formatMode(latestRun?.mode)} · ${formatOutcome(latestRun?.outcome)} · ${formatDuration(latestRun?.duration_ms)}`, columns))
  }

  const failure = latestError(events as DiagnosticRecord[])
  if (failure) {
    const value = fields(failure)
    lines.push("", fmt.bold("失败点"))
    lines.push(detailLine("位置", eventLabel(failure), columns))
    lines.push(detailLine("原因", String(value.error_code ?? value.summary_code ?? value.error_type ?? "FAILED"), columns))
    lines.push(detailLine("Run", `${prefix(failure.run_id)} · ${formatDuration(value.duration_ms)}`, columns))
  }

  lines.push("", fmt.bold("过程"))
  const preparation = preparationLines((events as DiagnosticRecord[]).filter(record => !record.thread_id && !record.run_id))
  if (preparation.length) {
    lines.push(selectedRuns.length ? "├─ 准备" : "└─ 准备")
    preparation.forEach((item, index) => lines.push(workLine(`│  ${index === preparation.length - 1 ? "└─" : "├─"} ${item.label}`, item.detail, item.duration, columns, fmt)))
  }
  selectedRuns.forEach((run, index) => {
    const isLatest = index === selectedRuns.length - 1
    const branch = isLatest ? "└─" : "├─"
    lines.push(workLine(`${branch} Run ${String(index + 1).padStart(2, "0")}  ${prefix(run.run_id)}`, `${formatMode(run.mode)} · ${formatOutcome(run.outcome)}`, run.duration_ms, columns, fmt))
    if (isLatest) {
      const detail = runProcessLines(events as DiagnosticRecord[], run.run_id)
      detail.forEach((item, detailIndex) => {
        const depth = item.depth ?? 0
        const hasLaterSibling = detail.slice(detailIndex + 1).some(candidate => (candidate.depth ?? 0) === depth)
        const indentation = depth === 0 ? "   " : "      "
        lines.push(workLine(`${indentation}${hasLaterSibling ? "├─" : "└─"} ${item.label}`, item.detail, item.duration, columns, fmt))
      })
    }
  })

  const slow = slowLines(events as DiagnosticRecord[], latestRun?.run_id, columns, fmt)
  if (slow.length) lines.push("", fmt.bold("慢项"), tableRow([{ value: "耗时", width: 8 }, { value: "项目", width: Math.max(1, columns - 10) }]), ...slow)

  lines.push("", fmt.bold("记录"))
  lines.push(`共 ${result.matched_count} 条 · 已显示 ${result.returned_count} 条${result.truncated ? " · 已截断" : ""}`)
  if (query.thread && selectedRuns.length > 1) lines.push(detailLine("查看", "更早的运行已折叠 · harness logs --run <run-prefix>", columns))
  if (query.run && ownerThread) lines.push(detailLine("Thread", `harness logs --thread ${prefix(ownerThread)}`, columns))
  const nextCommand = nextPageCommand(result, query)
  if (nextCommand) lines.push("", "下一页", nextCommand)
  const flatCommand = flatSwitchCommand(result, query)
  if (flatCommand) lines.push(detailLine("查看", flatCommand, columns))
  const diagnostics = diagnosticSummary(result)
  if (diagnostics) lines.push(detailLine("诊断", diagnostics, columns))
  return lines.map(line => line === nextCommand ? line : truncateDisplay(line, columns)).join("\n")
}

export async function runLogsQuery(command: LogsQuery): Promise<void> {
  try {
    const result = await queryLogs(command)
    const columns = process.stdout?.columns || 88
    const color = Boolean(process.stdout?.isTTY || process.env.FORCE_COLOR) && !process.env.NO_COLOR
    console.log(command.json ? JSON.stringify(result, null, 2) : renderLogsHuman(result, command, columns, { color }))
  } catch (error) {
    if (!(error instanceof LogsQueryError)) throw error
    if (command.json) console.log(JSON.stringify({ query_schema_version: 1, error: { code: error.code } }))
    else console.error(`${error.code}: ${error.message}`)
    process.exitCode = 2
  }
}
