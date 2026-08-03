/**
 * 统一版本、发布一致性和 Changelog 生成。
 */

import { execFileSync } from "node:child_process"
import { readFile, writeFile } from "node:fs/promises"
import { join, resolve } from "node:path"

const root = resolve(import.meta.dir, "../..")
const versionFiles = [
  "packages/cli/package.json",
  "packages/protocol/package.json",
] as const

export type SemVer = {
  raw: string
  major: number
  minor: number
  patch: number
  prerelease: string[]
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
  await replaceSingle(join(projectRoot, "packages/cli/src/interactive/runtime.ts"), /^export const CLI_VERSION = ".*"$/m, `export const CLI_VERSION = "${target.raw}"`)

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
  const cliModel = await readFile(join(projectRoot, "packages/cli/src/interactive/runtime.ts"), "utf8")
  if (!pyproject.includes(`version = "${version}"`) || !pythonInit.includes(`__version__ = "${version}"`) || !cliModel.includes(`CLI_VERSION = "${version}"`)) {
    throw new Error("Python Agent 或 CLI 运行时版本与 VERSION 不一致")
  }
  const changelog = await readFile(join(projectRoot, "CHANGELOG.md"), "utf8")
  if (!new RegExp(`^# 更新日志\\r?\\n\\r?\\n## \\[${escapeRegExp(version)}\\] - \\d{4}-\\d{2}-\\d{2}`).test(changelog)) {
    throw new Error("CHANGELOG 顶部缺少当前 VERSION 的版本节")
  }
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
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
