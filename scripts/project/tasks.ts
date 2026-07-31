/**
 * 任务源、状态流转和只读看板。
 */

import { readFile, readdir, writeFile } from "node:fs/promises"
import { basename, dirname, extname, join, relative, resolve } from "node:path"

const root = resolve(import.meta.dir, "../..")

export const TASK_STATUSES = ["待认领", "进行中", "阻塞", "待验收", "已完成"] as const
export const TASK_PRIORITIES = ["P0", "P1", "P2"] as const

const TASK_FIELDS = [
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

type TaskField = typeof TASK_FIELDS[number]

export type TaskRecord = {
  file: string
  metadata: Record<TaskField, string>
  body: string
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
  for (const field of TASK_FIELDS) {
    const value = values.get(field)
    if (!value) throw new Error(`${file} 缺少必填元数据：${field}`)
    metadata[field] = value
  }
  return { file, metadata, body: match[2] }
}

/** 校验任务状态机、认领信息和完成证据，阻止不完整事项进入看板。 */
export function validateTask(task: TaskRecord): void {
  const { metadata } = task
  if (!/^ZC-\d{3,}$/.test(metadata.id)) throw new Error(`${task.file} 的 id 必须形如 ZC-001`)
  if (!TASK_PRIORITIES.includes(metadata.priority as typeof TASK_PRIORITIES[number])) {
    throw new Error(`${task.file} 的 priority 必须为 ${TASK_PRIORITIES.join("/")}`)
  }
  if (!TASK_STATUSES.includes(metadata.status as typeof TASK_STATUSES[number])) {
    throw new Error(`${task.file} 的 status 无效：${metadata.status}`)
  }

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
}

/** 将任务元数据写回固定顺序的 front matter，降低多人协作时的无效 diff。 */
export function renderTaskDocument(task: TaskRecord): string {
  const frontMatter = TASK_FIELDS.map(field => `${field}: ${task.metadata[field]}`).join("\n")
  return `---\n${frontMatter}\n---\n${task.body.trimEnd()}\n`
}

/** 从任务目录读取全部 Markdown 任务，并保证任务 ID 唯一。 */
export async function loadTasks(projectRoot = root): Promise<TaskRecord[]> {
  const directory = join(projectRoot, "docs/developer/tasks")
  // 只加载任务目录根部的活动任务；archive 只保留审计记录，不应重新进入看板或认领流程。
  const files = (await listMarkdownFiles(directory)).filter(file => /^ZC-\d{3,}\.md$/.test(basename(file)) && dirname(file) === directory)
  const tasks = await Promise.all(files.map(async file => parseTaskDocument(await readFile(file, "utf8"), relative(projectRoot, file))))
  const ids = new Set<string>()
  for (const task of tasks) {
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
    const documentImpact = `用户：${value.user_docs}<br>开发：${value.developer_docs}`
    return `| ${value.id} | ${value.priority} | ${value.status} | ${escapeTable(value.title)} | ${escapeTable(value.owner)} | ${escapeTable(value.branch)} | ${escapeTable(documentImpact)} |`
  })
  return [
    "<!-- 此文件由 `bun run tasks:sync` 生成，请勿手动编辑。 -->",
    "# 任务看板",
    "",
    "活动任务文件位于 `docs/developer/tasks/`；已完成任务归档于 `docs/developer/tasks/archive/`，不进入看板。认领请使用 `bun run task:claim -- <ID> --owner <名称> --branch <分支>`；完成请使用 `bun run task:complete` 并提供测试证据。",
    "",
    "| ID | 优先级 | 状态 | 标题 | 负责人 | 分支 | 文档影响 |",
    "| --- | --- | --- | --- | --- | --- | --- |",
    ...(rows.length ? rows : ["| - | - | - | 暂无任务 | - | - | - |"]),
    "",
  ].join("\n")
}

/** 同步任务看板文件，供提交前和任务状态变更后调用。 */
export async function syncTasks(projectRoot = root): Promise<void> {
  const tasks = await loadTasks(projectRoot)
  await writeFile(join(projectRoot, "docs/developer/tasks/任务看板.md"), renderTaskBoard(tasks), "utf8")
}

/** 验证已提交的任务看板与任务源一致，防止生成文件过期。 */
export async function checkTasks(projectRoot = root): Promise<void> {
  const tasks = await loadTasks(projectRoot)
  const expected = renderTaskBoard(tasks)
  const board = await readFile(join(projectRoot, "docs/developer/tasks/任务看板.md"), "utf8")
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
  await saveTask(projectRoot, task)
  await syncTasks(projectRoot)
}

/** 记录完成证据并关闭任务；文档影响由任务文件本身作为审计记录。 */
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
  await saveTask(projectRoot, task)
  await syncTasks(projectRoot)
}

async function findTask(projectRoot: string, id: string): Promise<TaskRecord> {
  const tasks = await loadTasks(projectRoot)
  const task = tasks.find(item => item.metadata.id === id)
  if (!task) throw new Error(`未找到任务：${id}`)
  return task
}

/** 读取归档任务 ID，使历史文档可以继续互相引用，但不参与活动任务流程。 */
export async function loadArchivedTaskIds(projectRoot: string): Promise<Set<string>> {
  const directory = join(projectRoot, "docs/developer/tasks/archive")
  const files = (await listMarkdownFiles(directory)).filter(file => /^ZC-\d{3,}\.md$/.test(basename(file)))
  const tasks = await Promise.all(files.map(async file => parseTaskDocument(await readFile(file, "utf8"), relative(projectRoot, file))))
  tasks.forEach(validateTask)
  return new Set(tasks.map(task => task.metadata.id))
}

async function saveTask(projectRoot: string, task: TaskRecord): Promise<void> {
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

function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT"
}
