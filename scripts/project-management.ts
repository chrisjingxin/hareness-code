/**
 * 仓库协作基础设施：维护任务看板、文档链接、统一版本和自动生成的更新日志。
 * 所有命令只处理仓库内受控文件，避免把外部项目管理系统作为事实来源。
 */

import { execFileSync } from "node:child_process"
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises"
import { basename, dirname, extname, join, relative, resolve } from "node:path"

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

export type SemVer = {
  raw: string
  major: number
  minor: number
  patch: number
  prerelease: string[]
}

type CommandOptions = Record<string, string>

const root = resolve(import.meta.dir, "..")
const versionFiles = [
  "packages/cli/package.json",
  "packages/protocol/package.json",
] as const

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
  const files = (await listMarkdownFiles(directory)).filter(file => basename(file) !== "README.md")
  const tasks = await Promise.all(files.map(async file => parseTaskDocument(await readFile(file, "utf8"), relative(projectRoot, file))))
  const ids = new Set<string>()
  for (const task of tasks) {
    validateTask(task)
    if (ids.has(task.metadata.id)) throw new Error(`任务 ID 重复：${task.metadata.id}`)
    ids.add(task.metadata.id)
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
    "任务文件位于 `docs/developer/tasks/`。认领请使用 `bun run task:claim -- <ID> --owner <名称> --branch <分支>`；完成请使用 `bun run task:complete` 并提供测试证据。",
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
  await writeFile(join(projectRoot, "docs/developer/任务看板.md"), renderTaskBoard(tasks), "utf8")
}

/** 验证已提交的任务看板与任务源一致，防止生成文件过期。 */
export async function checkTasks(projectRoot = root): Promise<void> {
  const tasks = await loadTasks(projectRoot)
  const expected = renderTaskBoard(tasks)
  const board = await readFile(join(projectRoot, "docs/developer/任务看板.md"), "utf8")
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

/** 校验文档入口、任务看板和所有本地 Markdown 链接。 */
export async function checkDocs(projectRoot = root): Promise<void> {
  const required = [
    "README.md",
    "docs/user/快速开始.md",
    "docs/user/模型配置.md",
    "docs/user/交互使用.md",
    "docs/user/故障排查.md",
    "docs/developer/架构总览.md",
    "docs/developer/开发工作流.md",
    "docs/developer/变更检查清单.md",
    "docs/developer/任务看板说明.md",
    "docs/developer/任务看板.md",
    "docs/developer/adr/README.md",
    "docs/developer/tasks/README.md",
  ]
  for (const path of required) {
    try {
      await readFile(join(projectRoot, path), "utf8")
    } catch {
      throw new Error(`缺少必需文档：${path}`)
    }
  }

  const documents = [join(projectRoot, "README.md"), ...await listMarkdownFiles(join(projectRoot, "docs"))]
  const taskIds = new Set((await loadTasks(projectRoot)).map(task => task.metadata.id))
  for (const document of documents) {
    const content = await readFile(document, "utf8")
    for (const link of markdownLinks(content)) {
      if (isExternalLink(link)) continue
      const target = link.split("#", 1)[0]
      if (!target) continue
      const resolved = resolve(dirname(document), target)
      try {
        await readFile(resolved)
      } catch {
        throw new Error(`${relative(projectRoot, document)} 包含无效本地链接：${link}`)
      }
    }

    // 文档中若提到任务 ID，必须对应真实任务源，避免说明文档指向已删除的事项。
    for (const id of content.match(/\bZC-\d{3,}\b/g) ?? []) {
      if (!taskIds.has(id)) throw new Error(`${relative(projectRoot, document)} 引用了不存在的任务：${id}`)
    }
  }
}

/** 解析严格的 SemVer 字符串，支持预发布和构建元数据。 */
export function parseSemVer(value: string): SemVer {
  const match = value.trim().match(/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$/)
  if (!match) throw new Error(`无效 SemVer：${value}`)
  return {
    raw: value.trim(),
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    prerelease: match[4]?.split(".") ?? [],
  }
}

/** 比较两个 SemVer；正数表示左侧版本更高。 */
export function compareSemVer(left: SemVer, right: SemVer): number {
  for (const field of ["major", "minor", "patch"] as const) {
    if (left[field] !== right[field]) return left[field] > right[field] ? 1 : -1
  }
  if (!left.prerelease.length && !right.prerelease.length) return 0
  if (!left.prerelease.length) return 1
  if (!right.prerelease.length) return -1
  const count = Math.max(left.prerelease.length, right.prerelease.length)
  for (let index = 0; index < count; index++) {
    const a = left.prerelease[index]
    const b = right.prerelease[index]
    if (a === b) continue
    if (a === undefined) return -1
    if (b === undefined) return 1
    const aNumber = /^\d+$/.test(a)
    const bNumber = /^\d+$/.test(b)
    if (aNumber && bNumber) return Number(a) > Number(b) ? 1 : -1
    if (aNumber !== bNumber) return aNumber ? -1 : 1
    return a > b ? 1 : -1
  }
  return 0
}

/** 按 Conventional Commit 标题生成中文 Changelog 版本节。 */
export function renderChangelogSection(version: string, date: string, subjects: readonly string[]): string {
  const groups = new Map<string, string[]>()
  const labels: Array<[string, string]> = [
    ["feat", "新增"],
    ["fix", "修复"],
    ["perf", "优化"],
    ["refactor", "优化"],
    ["security", "安全"],
    ["docs", "文档"],
  ]
  for (const subject of subjects) {
    const match = subject.match(/^([a-z]+)(?:\([^)]*\))?!?:\s*(.+)$/i)
    const type = match?.[1].toLowerCase() ?? "other"
    const message = match?.[2] ?? subject
    const label = labels.find(([prefix]) => prefix === type)?.[1] ?? "其他"
    groups.set(label, [...(groups.get(label) ?? []), message])
  }
  const orderedLabels = ["新增", "修复", "优化", "安全", "文档", "其他"]
  const sections = orderedLabels.flatMap(label => {
    const items = groups.get(label)
    return items?.length ? [`### ${label}`, ...items.map(item => `- ${item}`), ""] : []
  })
  if (!sections.length) sections.push("### 其他", "- 初始化版本记录。", "")
  return [`## [${version}] - ${date}`, "", ...sections].join("\n")
}

/** 通过唯一版本来源同步所有包与运行时常量，并在同一操作内刷新 Changelog。 */
export async function setVersion(projectRoot: string, version: string, subjects?: readonly string[]): Promise<void> {
  const target = parseSemVer(version)
  const versionPath = join(projectRoot, "VERSION")
  const changelogPath = join(projectRoot, "CHANGELOG.md")
  const existing = await readOptional(changelogPath) ?? "# 更新日志\n\n"
  const currentSource = await readOptional(versionPath)
  if (currentSource !== undefined) {
    const current = parseSemVer(currentSource.trim())
    const comparison = compareSemVer(target, current)
    // 首次引入本机制时允许以已有版本补建 CHANGELOG；后续版本必须严格递增。
    if (comparison < 0 || (comparison === 0 && existing !== "# 更新日志\n\n")) {
      throw new Error(`新版本必须高于当前版本 ${current.raw}`)
    }
  }

  await writeFile(versionPath, `${target.raw}\n`, "utf8")
  for (const file of versionFiles) await setPackageVersion(join(projectRoot, file), target.raw)
  await replaceSingle(join(projectRoot, "packages/agent/pyproject.toml"), /^version = ".*"$/m, `version = "${target.raw}"`)
  await replaceSingle(join(projectRoot, "packages/agent/harness_agent/__init__.py"), /^__version__ = ".*"$/m, `__version__ = "${target.raw}"`)
  await replaceSingle(join(projectRoot, "packages/cli/src/tui/model.ts"), /^export const CLI_VERSION = ".*"$/m, `export const CLI_VERSION = "${target.raw}"`)

  if (existing.includes(`## [${target.raw}] -`)) throw new Error(`CHANGELOG 已包含版本 ${target.raw}`)
  const messages = subjects ?? readCommitSubjects(projectRoot)
  await writeFile(changelogPath, `${changelogHeader(existing)}${renderChangelogSection(target.raw, today(), messages)}${changelogBody(existing)}`, "utf8")
}

/** 验证版本字段、顶层 Changelog 节和所有发布文件的一致性。 */
export async function checkRelease(projectRoot = root): Promise<void> {
  const version = parseSemVer((await readFile(join(projectRoot, "VERSION"), "utf8")).trim()).raw
  for (const file of versionFiles) {
    const value = JSON.parse(await readFile(join(projectRoot, file), "utf8")) as { version?: unknown }
    if (value.version !== version) throw new Error(`${file} 版本与 VERSION 不一致`)
  }
  const pyproject = await readFile(join(projectRoot, "packages/agent/pyproject.toml"), "utf8")
  const pythonInit = await readFile(join(projectRoot, "packages/agent/harness_agent/__init__.py"), "utf8")
  const cliModel = await readFile(join(projectRoot, "packages/cli/src/tui/model.ts"), "utf8")
  if (!pyproject.includes(`version = "${version}"`) || !pythonInit.includes(`__version__ = "${version}"`) || !cliModel.includes(`CLI_VERSION = "${version}"`)) {
    throw new Error("Python Agent 或 CLI 运行时版本与 VERSION 不一致")
  }
  const changelog = await readFile(join(projectRoot, "CHANGELOG.md"), "utf8")
  if (!new RegExp(`^# 更新日志\\r?\\n\\r?\\n## \\[${escapeRegExp(version)}\\] - \\d{4}-\\d{2}-\\d{2}`).test(changelog)) {
    throw new Error("CHANGELOG 顶部缺少当前 VERSION 的版本节")
  }
}

/** 根据子命令运行项目管理操作，供 package.json 统一调用。 */
export async function main(argv = process.argv.slice(2)): Promise<void> {
  const [command, ...args] = argv
  const options = parseOptions(args)
  switch (command) {
    case "docs:check":
      await checkDocs(root)
      return
    case "tasks:sync":
      await syncTasks(root)
      return
    case "tasks:check":
      await checkTasks(root)
      return
    case "task:claim": {
      const id = positional(args)[0]
      if (!id) throw new Error("用法：task:claim <ID> --owner <名称> --branch <分支>")
      await claimTask(root, id, requiredOption(options, "owner"), requiredOption(options, "branch"))
      return
    }
    case "task:complete": {
      const id = positional(args)[0]
      if (!id) throw new Error("用法：task:complete <ID> --evidence <测试证据> [--references <提交或 PR>]")
      await completeTask(root, id, requiredOption(options, "evidence"), options.references)
      return
    }
    case "version:set": {
      const version = positional(args)[0]
      if (!version) throw new Error("用法：version:set <SemVer>")
      await setVersion(root, version)
      return
    }
    case "release:check":
      await checkRelease(root)
      return
    case "project:check":
      await checkDocs(root)
      await checkTasks(root)
      await checkRelease(root)
      return
    default:
      throw new Error("用法：docs:check|tasks:sync|tasks:check|task:claim|task:complete|version:set|release:check|project:check")
  }
}

async function findTask(projectRoot: string, id: string): Promise<TaskRecord> {
  const tasks = await loadTasks(projectRoot)
  const task = tasks.find(item => item.metadata.id === id)
  if (!task) throw new Error(`未找到任务：${id}`)
  return task
}

async function saveTask(projectRoot: string, task: TaskRecord): Promise<void> {
  await writeFile(join(projectRoot, task.file), renderTaskDocument(task), "utf8")
}

async function listMarkdownFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true })
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

function markdownLinks(content: string): string[] {
  return [...content.matchAll(/\[[^\]]*\]\(([^)]+)\)/g)].map(match => match[1]?.trim() ?? "")
}

function isExternalLink(value: string): boolean {
  return /^(?:https?:|mailto:|#)/i.test(value)
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

function parseOptions(args: readonly string[]): CommandOptions {
  const options: CommandOptions = {}
  for (let index = 0; index < args.length; index++) {
    const value = args[index]
    if (!value?.startsWith("--")) continue
    const [key, inline] = value.slice(2).split("=", 2)
    const next = inline ?? args[index + 1]
    if (!key || !next || next.startsWith("--")) throw new Error(`选项 ${value} 缺少值`)
    options[key] = next
    if (inline === undefined) index += 1
  }
  return options
}

function positional(args: readonly string[]): string[] {
  const values: string[] = []
  for (let index = 0; index < args.length; index++) {
    const value = args[index]
    if (!value?.startsWith("--")) values.push(value)
    else if (!value.includes("=")) index += 1
  }
  return values
}

function requiredOption(options: CommandOptions, key: string): string {
  const value = options[key]
  if (!value) throw new Error(`缺少 --${key}`)
  return value
}

async function setPackageVersion(file: string, version: string): Promise<void> {
  const packageJson = JSON.parse(await readFile(file, "utf8")) as Record<string, unknown>
  packageJson.version = version
  await writeFile(file, `${JSON.stringify(packageJson, null, 2)}\n`, "utf8")
}

async function replaceSingle(file: string, pattern: RegExp, replacement: string): Promise<void> {
  const source = await readFile(file, "utf8")
  if (!pattern.test(source)) throw new Error(`${file} 缺少待同步版本字段`)
  await writeFile(file, source.replace(pattern, replacement), "utf8")
}

async function readOptional(path: string): Promise<string | undefined> {
  try {
    return await readFile(path, "utf8")
  } catch (error) {
    if (!isNotFound(error)) throw error
    return undefined
  }
}

function readCommitSubjects(projectRoot: string): string[] {
  let latestTag: string | undefined
  try {
    latestTag = execFileSync("git", ["describe", "--tags", "--match", "v*", "--abbrev=0"], { cwd: projectRoot, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim() || undefined
  } catch {
    latestTag = undefined
  }
  const range = latestTag ? `${latestTag}..HEAD` : "HEAD"
  const output = execFileSync("git", ["log", range, "--format=%s", "--reverse"], { cwd: projectRoot, encoding: "utf8" })
  return output.split("\n").map(value => value.trim()).filter(Boolean)
}

function changelogHeader(existing: string): string {
  return "# 更新日志\n\n"
}

function changelogBody(existing: string): string {
  const body = existing.replace(/^# 更新日志\r?\n*/, "").trim()
  return body ? `${body}\n` : ""
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT"
}

if (import.meta.main) {
  main().catch(error => {
    console.error(`project-management: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  })
}
