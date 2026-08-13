/**
 * 任务源、状态流转和只读看板。
 *
 * 约定见 AGENTS.md：
 * - 活动任务：docs/developer/task/HC-XXX-功能简介.md
 * - 已完成归档：docs/developer/task/archive/
 * - 看板：docs/developer/task/任务看板.md
 */

import { mkdir, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises"
import { basename, dirname, extname, join, relative, resolve } from "node:path"

const root = resolve(import.meta.dir, "../..")

export const TASK_DIR = "docs/developer/task"
export const TASK_ARCHIVE_DIR = `${TASK_DIR}/archive`
export const TASK_BOARD_PATH = `${TASK_DIR}/任务看板.md`

/** 任务 ID：HC-001 或历史 HC-098-legacy */
export const TASK_ID_PATTERN = /^HC-\d{3,}(?:-legacy)?$/
/** 引用扫描用：HC-001 / HC-098-legacy */
export const TASK_ID_MATCH_GLOBAL = /\bHC-\d{3,}(?:-legacy)?\b/g

export const TASK_STATUSES = ["待认领", "进行中", "阻塞", "待验收", "已完成", "已过时"] as const
export const TASK_PRIORITIES = ["P0", "P1", "P2"] as const

/** 功能简介最长 15 个 Unicode 字元（中文按字计）。 */
export const TASK_BRIEF_MAX_CHARS = 15

const REQUIRED_TASK_FIELDS = [
  "id",
  "title",
  "priority",
  "status",
  "owner",
  "branch",
  "scope",
  "acceptance",
  "user_docs",
  "developer_docs",
  "test_evidence",
  "references",
  "completed_at",
] as const

const TRACEABILITY_TASK_FIELDS = [
  "feature_area",
  "parent_task",
  "decomposed_by",
  "reviewed_at",
  "review_due",
] as const

const TASK_FIELDS = [
  "id",
  "title",
  "feature_area",
  "parent_task",
  "decomposed_by",
  "priority",
  "status",
  "owner",
  "branch",
  "reviewed_at",
  "review_due",
  "scope",
  "acceptance",
  "user_docs",
  "developer_docs",
  "test_evidence",
  "references",
  "completed_at",
] as const

type TaskField = typeof TASK_FIELDS[number]

const TRACEABILITY_DEFAULTS: Record<typeof TRACEABILITY_TASK_FIELDS[number], string> = {
  feature_area: "历史未归类",
  parent_task: "-",
  decomposed_by: "历史未记录",
  reviewed_at: "-",
  review_due: "-",
}

export type TaskRecord = {
  file: string
  metadata: Record<TaskField, string>
  body: string
}

/** 从 title 生成文件名用的功能简介（≤15 字，去掉路径不安全字符）。 */
export function taskBriefFromTitle(title: string, maxChars = TASK_BRIEF_MAX_CHARS): string {
  const cleaned = title
    .normalize("NFKC")
    .replace(/[/\\:*?"<>|]/g, "")
    .replace(/\s+/g, "")
    .trim()
  if (!cleaned) throw new Error("任务 title 无法生成功能简介")
  const brief = [...cleaned].slice(0, maxChars).join("")
  if (!brief) throw new Error("任务 title 无法生成功能简介")
  return brief
}

/** 规范任务文件名：HC-XXX-功能简介.md */
export function taskFileName(id: string, title: string): string {
  if (!TASK_ID_PATTERN.test(id)) throw new Error(`任务 id 必须形如 HC-001：${id}`)
  return `${id}-${taskBriefFromTitle(title)}.md`
}

/** 解析活动/归档任务文件名；必须带功能简介。 */
export function parseTaskFileName(fileName: string): { id: string, brief: string } {
  const match = fileName.match(/^(HC-\d{3,}(?:-legacy)?)-(.+)\.md$/)
  if (!match) {
    throw new Error(`${fileName} 不符合 HC-XXX-功能简介.md 命名（必须含功能简介）`)
  }
  const id = match[1]
  const brief = match[2]
  if (![...brief].length || [...brief].length > TASK_BRIEF_MAX_CHARS) {
    throw new Error(`${fileName} 的功能简介须为 1～${TASK_BRIEF_MAX_CHARS} 个字`)
  }
  if (/[/\\:*?"<>|]/.test(brief)) {
    throw new Error(`${fileName} 的功能简介包含非法字符`)
  }
  return { id, brief }
}

/** 读取固定格式的任务 front matter，并保留任务正文供后续人工维护。 */
export function parseTaskDocument(source: string, file: string): TaskRecord {
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/)
  if (!match) throw new Error(`${file} 缺少任务 front matter`)
  const values = new Map<string, string>()
  for (const line of match[1].split(/\r?\n/)) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue
    const separator = line.indexOf(":")
    if (separator < 1) throw new Error(`${file} 包含无效元数据行：${line}`)
    const key = line.slice(0, separator).trim()
    const value = line.slice(separator + 1).trim()
    if (values.has(key)) throw new Error(`${file} 重复定义元数据：${key}`)
    values.set(key, value)
  }

  const metadata = {} as Record<TaskField, string>
  for (const field of REQUIRED_TASK_FIELDS) {
    const value = values.get(field)
    if (!value) throw new Error(`${file} 缺少必填元数据：${field}`)
    metadata[field] = value
  }
  for (const field of TRACEABILITY_TASK_FIELDS) {
    metadata[field] = values.get(field) || TRACEABILITY_DEFAULTS[field]
  }
  return { file, metadata, body: match[2] }
}

/** 校验任务状态机、认领信息、完成证据与文件命名。 */
export function validateTask(task: TaskRecord): void {
  const { metadata } = task
  if (!TASK_ID_PATTERN.test(metadata.id)) {
    throw new Error(`${task.file} 的 id 必须形如 HC-001 或 HC-001-legacy`)
  }

  const fileName = basename(task.file)
  let parsed: { id: string, brief: string }
  try {
    parsed = parseTaskFileName(fileName)
  } catch (error) {
    throw new Error(`${task.file}：${error instanceof Error ? error.message : String(error)}`)
  }
  if (parsed.id !== metadata.id) {
    throw new Error(`${task.file} 文件名中的 id（${parsed.id}）与 front matter id（${metadata.id}）不一致`)
  }

  if (!TASK_PRIORITIES.includes(metadata.priority as typeof TASK_PRIORITIES[number])) {
    throw new Error(`${task.file} 的 priority 必须为 ${TASK_PRIORITIES.join("/")}`)
  }
  if (!TASK_STATUSES.includes(metadata.status as typeof TASK_STATUSES[number])) {
    throw new Error(`${task.file} 的 status 无效：${metadata.status}`)
  }
  if (metadata.parent_task !== "-" && !TASK_ID_PATTERN.test(metadata.parent_task)) {
    throw new Error(`${task.file} 的 parent_task 必须为任务 ID 或 -`)
  }
  validateReviewDates(task)

  const claimed = metadata.owner !== "未认领" && metadata.branch !== "-"
  if (metadata.status === "进行中" && !claimed) {
    throw new Error(`${task.file} 处于进行中时必须填写 owner 和 branch`)
  }
  if (metadata.status === "待认领" && (metadata.owner !== "未认领" || metadata.branch !== "-")) {
    throw new Error(`${task.file} 待认领状态不得保留 owner 或 branch`)
  }
  if (metadata.status === "已完成") {
    if (metadata.test_evidence === "-" || metadata.completed_at === "-") {
      throw new Error(`${task.file} 已完成任务必须填写测试证据和完成日期`)
    }
    if ([metadata.user_docs, metadata.developer_docs].some(value => !value || value === "待确定")) {
      throw new Error(`${task.file} 已完成任务必须记录用户和开发者文档影响`)
    }
  }
  if (metadata.status === "已过时") {
    if (metadata.reviewed_at === "-" || metadata.review_due !== "-" || metadata.references === "-") {
      throw new Error(`${task.file} 已过时任务必须填写 reviewed_at 和替代 references，并清空 review_due`)
    }
  }
}

/** 将任务元数据写回固定顺序的 front matter，降低多人协作时的无效 diff。 */
export function renderTaskDocument(task: TaskRecord): string {
  const frontMatter = TASK_FIELDS.map(field => `${field}: ${task.metadata[field]}`).join("\n")
  return `---\n${frontMatter}\n---\n${task.body.trimEnd()}\n`
}

function isTaskCandidateFile(fileName: string): boolean {
  // README / 任务看板不是任务源；其余 HC- 前缀 Markdown 都必须符合命名规范。
  return fileName.startsWith("HC-") && fileName.endsWith(".md")
}

/** 从任务目录读取全部活动 Markdown 任务，并保证任务 ID 唯一。 */
export async function loadTasks(projectRoot = root): Promise<TaskRecord[]> {
  const directory = join(projectRoot, TASK_DIR)
  // 只加载任务目录根部的活动任务；archive 只保留审计记录，不应重新进入看板或认领流程。
  const files = (await listMarkdownFiles(directory)).filter(
    file => isTaskCandidateFile(basename(file)) && dirname(file) === directory,
  )
  const tasks = await Promise.all(files.map(async file => parseTaskDocument(await readFile(file, "utf8"), relative(projectRoot, file))))
  const ids = new Set<string>()
  for (const task of tasks) {
    if (task.metadata.status === "已完成") {
      throw new Error(`${task.file} 状态为已完成，应位于 ${TASK_ARCHIVE_DIR}/，请移入归档后再同步`)
    }
    validateTask(task)
    if (ids.has(task.metadata.id)) throw new Error(`任务 ID 重复：${task.metadata.id}`)
    ids.add(task.metadata.id)
  }
  const archivedTaskIds = await loadArchivedTaskIds(projectRoot)
  for (const id of ids) {
    if (archivedTaskIds.has(id)) throw new Error(`活动与归档任务 ID 重复：${id}`)
  }
  return tasks.sort(compareTasks)
}

/** 从任务源生成只读看板；所有人通过任务文件认领和更新，避免表格冲突。 */
export function renderTaskBoard(tasks: readonly TaskRecord[]): string {
  const rows = tasks.map(task => {
    const value = task.metadata
    const ownership = `拆解：${value.decomposed_by}<br>认领：${value.owner}`
    const feature = `板块：${value.feature_area}<br>上层：${value.parent_task}`
    const documentImpact = `用户：${value.user_docs}<br>开发：${value.developer_docs}`
    return `| ${value.id} | ${value.priority} | ${value.status} | ${escapeTable(value.title)} | ${escapeTable(feature)} | ${escapeTable(ownership)} | ${escapeTable(value.branch)} | ${escapeTable(value.review_due)} | ${escapeTable(documentImpact)} |`
  })
  return [
    "<!-- 此文件由 `bun run tasks:sync` 生成，请勿手动编辑。 -->",
    "# 任务看板",
    "",
    `活动任务文件位于 \`${TASK_DIR}/\`（命名 \`HC-XXX-功能简介.md\`）；已完成任务归档于 \`${TASK_ARCHIVE_DIR}/\`，不进入看板。流程：task → spec → plan → todo → implement → review。认领：\`bun run task:claim -- <ID> --owner <名称> --branch <分支>\`；完成：\`bun run task:complete\` 并提供测试证据。`,
    "",
    "| ID | 优先级 | 状态 | 标题 | 功能归属 | 责任人 | 分支 | 下次复核 | 文档影响 |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ...(rows.length ? rows : ["| - | - | - | 暂无任务 | - | - | - | - | - |"]),
    "",
  ].join("\n")
}

/** 同步任务看板文件，供提交前和任务状态变更后调用。 */
export async function syncTasks(projectRoot = root): Promise<void> {
  const tasks = await loadTasks(projectRoot)
  const boardPath = join(projectRoot, TASK_BOARD_PATH)
  await mkdir(dirname(boardPath), { recursive: true })
  await writeFile(boardPath, renderTaskBoard(tasks), "utf8")
}

/** 验证已提交的任务看板与任务源一致，防止生成文件过期。 */
export async function checkTasks(projectRoot = root): Promise<void> {
  const tasks = await loadTasks(projectRoot)
  const expected = renderTaskBoard(tasks)
  const board = await readFile(join(projectRoot, TASK_BOARD_PATH), "utf8")
  if (board !== expected) throw new Error("任务看板已过期，请运行 bun run tasks:sync")
}

/** 认领待办任务并立即重新生成任务看板。 */
export async function claimTask(projectRoot: string, id: string, owner: string, branch: string): Promise<void> {
  if (!owner.trim() || !branch.trim()) throw new Error("认领任务必须提供非空 owner 和 branch")
  const task = await findTask(projectRoot, id)
  if (task.metadata.status !== "待认领") throw new Error(`${id} 当前状态为 ${task.metadata.status}，不能重复认领`)
  task.metadata.status = "进行中"
  task.metadata.owner = owner.trim()
  task.metadata.branch = branch.trim()
  if (task.metadata.reviewed_at === "-") task.metadata.reviewed_at = today()
  if (task.metadata.review_due === "-") task.metadata.review_due = addDays(today(), 14)
  await saveTask(projectRoot, task)
  await syncTasks(projectRoot)
}

/**
 * 记录完成证据并关闭任务，随后移入 archive/。
 * 文档影响由任务文件本身作为审计记录。
 */
export async function completeTask(projectRoot: string, id: string, evidence: string, references?: string): Promise<void> {
  if (!evidence.trim()) throw new Error("完成任务必须提供 --evidence 测试证据")
  const task = await findTask(projectRoot, id)
  if (!(["进行中", "待验收"] as string[]).includes(task.metadata.status)) {
    throw new Error(`${id} 当前状态为 ${task.metadata.status}，不能标记完成`)
  }
  if ([task.metadata.user_docs, task.metadata.developer_docs].some(value => !value || value === "待确定")) {
    throw new Error(`${id} 必须先在任务文件中记录用户和开发者文档影响`)
  }
  task.metadata.status = "已完成"
  task.metadata.test_evidence = evidence.trim()
  task.metadata.references = references?.trim() || task.metadata.references
  task.metadata.completed_at = today()
  task.metadata.reviewed_at = today()
  task.metadata.review_due = "-"
  await saveTask(projectRoot, task)
  await archiveTaskFile(projectRoot, task)
  await syncTasks(projectRoot)
}

async function findTask(projectRoot: string, id: string): Promise<TaskRecord> {
  const tasks = await loadTasks(projectRoot)
  const task = tasks.find(item => item.metadata.id === id)
  if (!task) throw new Error(`未找到任务：${id}`)
  return task
}

/** 将已完成任务文件移入归档目录，并从活动目录移除。 */
async function archiveTaskFile(projectRoot: string, task: TaskRecord): Promise<void> {
  const source = join(projectRoot, task.file)
  const archiveDirectory = join(projectRoot, TASK_ARCHIVE_DIR)
  await mkdir(archiveDirectory, { recursive: true })
  const destination = join(archiveDirectory, basename(task.file))
  await rename(source, destination)
  task.file = relative(projectRoot, destination)
}

/** 读取归档任务 ID，使历史文档可以继续互相引用，但不参与活动任务流程。 */
export async function loadArchivedTaskIds(projectRoot: string): Promise<Set<string>> {
  const directory = join(projectRoot, TASK_ARCHIVE_DIR)
  const files = (await listMarkdownFiles(directory)).filter(file => isTaskCandidateFile(basename(file)))
  const tasks = await Promise.all(files.map(async file => parseTaskDocument(await readFile(file, "utf8"), relative(projectRoot, file))))
  for (const task of tasks) {
    validateTask(task)
  }
  return new Set(tasks.map(task => task.metadata.id))
}

async function saveTask(projectRoot: string, task: TaskRecord): Promise<void> {
  const expectedName = taskFileName(task.metadata.id, task.metadata.title)
  const currentName = basename(task.file)
  const directory = dirname(join(projectRoot, task.file))
  // title 变更时同步文件名，保持 HC-XXX-功能简介 与 title 一致。
  if (currentName !== expectedName) {
    const nextPath = join(directory, expectedName)
    const previousPath = join(projectRoot, task.file)
    await writeFile(nextPath, renderTaskDocument(task), "utf8")
    if (previousPath !== nextPath) {
      try {
        await unlink(previousPath)
      } catch (error) {
        if (!isNotFound(error)) throw error
      }
    }
    task.file = relative(projectRoot, nextPath)
    return
  }
  await writeFile(join(projectRoot, task.file), renderTaskDocument(task), "utf8")
}

export async function listMarkdownFiles(directory: string): Promise<string[]> {
  let entries
  try {
    entries = await readdir(directory, { withFileTypes: true })
  } catch (error) {
    if (isNotFound(error)) return []
    throw error
  }
  const nested = await Promise.all(entries.map(async entry => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return listMarkdownFiles(path)
    return entry.isFile() && extname(entry.name) === ".md" ? [path] : []
  }))
  return nested.flat()
}

function compareTasks(left: TaskRecord, right: TaskRecord): number {
  const priority = TASK_PRIORITIES.indexOf(left.metadata.priority as typeof TASK_PRIORITIES[number]) - TASK_PRIORITIES.indexOf(right.metadata.priority as typeof TASK_PRIORITIES[number])
  return priority || left.metadata.id.localeCompare(right.metadata.id)
}

function escapeTable(value: string): string {
  return value.replaceAll("|", "\\|").replaceAll("\n", "<br>")
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

/** 活动任务的复核日期必须成对存在，且到期后阻止项目检查继续忽略。 */
function validateReviewDates(task: TaskRecord): void {
  const { reviewed_at: reviewedAt, review_due: reviewDue, status } = task.metadata
  for (const [field, value] of [["reviewed_at", reviewedAt], ["review_due", reviewDue]] as const) {
    if (value !== "-" && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw new Error(`${task.file} 的 ${field} 必须为 YYYY-MM-DD 或 -`)
    }
  }
  if ((reviewedAt === "-") !== (reviewDue === "-") && !(["已完成", "已过时"] as string[]).includes(status)) {
    throw new Error(`${task.file} 的 reviewed_at 与 review_due 必须同时填写`)
  }
  if (reviewedAt !== "-" && reviewDue !== "-" && reviewDue < reviewedAt) {
    throw new Error(`${task.file} 的 review_due 不能早于 reviewed_at`)
  }
  if (!(["已完成", "已过时"] as string[]).includes(status) && reviewDue !== "-" && reviewDue < today()) {
    throw new Error(`${task.file} 已到复核日期 ${reviewDue}，请确认任务仍有效并更新 reviewed_at/review_due`)
  }
}

function addDays(date: string, days: number): string {
  const value = new Date(`${date}T00:00:00.000Z`)
  value.setUTCDate(value.getUTCDate() + days)
  return value.toISOString().slice(0, 10)
}

function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT"
}
