/** Git 工作区状态探测：把 git 命令输出收敛为四种稳定状态，供 TUI 与 Web 共用展示。 */

import { execFile } from "node:child_process"
import { lstat, readFile } from "node:fs/promises"
import { isAbsolute, relative, resolve } from "node:path"
import { promisify } from "node:util"

import type { GitChangedFile, GitWorkspaceState } from "../interactive/runtime"

const execFileAsync = promisify(execFile)
const UNTRACKED_FILE_SIZE_LIMIT = 1024 * 1024
const UNTRACKED_TOTAL_SIZE_LIMIT = 4 * 1024 * 1024

/** 单条 git 探测的失败原因；成功时只携带 stdout。 */
type ProbeResult =
  | { ok: true; stdout: string }
  | { ok: false; reason: "missing" | "not-repository" | "unavailable" }

/** 探测失败统一收敛为不可用状态，避免把 git 内部错误细节泄漏到界面。 */
function unavailable(): GitWorkspaceState {
  return { kind: "unavailable", message: "Git 状态不可用" }
}

/** 执行单条 git 探测并分类失败原因；超时与未知错误统一视为不可用。 */
async function probeGit(args: readonly string[], cwd: string, timeoutMs: number): Promise<ProbeResult> {
  try {
    const { stdout } = await execFileAsync("git", ["-C", cwd, ...args], {
      encoding: "utf8",
      timeout: timeoutMs,
      windowsHide: true,
    })
    return { ok: true, stdout }
  } catch (error) {
    const err = error as NodeJS.ErrnoException & { killed?: boolean; stderr?: unknown }
    if (err.code === "ENOENT") return { ok: false, reason: "missing" }
    if (typeof err.stderr === "string" && err.stderr.includes("not a git repository")) {
      return { ok: false, reason: "not-repository" }
    }
    return { ok: false, reason: "unavailable" }
  }
}

/**
 * 探测 cwd 的 Git 工作区状态：先定位仓库根，再区分分支与 detached HEAD。
 * 每条探测独立超时，最多 3 条、最坏 timeoutMs × 3；典型分支场景只需 2 条。
 */
export async function detectGitWorkspace(cwd: string, timeoutMs = 500): Promise<GitWorkspaceState> {
  const rootProbe = await probeGit(["rev-parse", "--show-toplevel"], cwd, timeoutMs)
  if (!rootProbe.ok) {
    // 明确不是 git 仓库时无需继续探测；其余失败（git 缺失/超时/未知）直接不可用。
    if (rootProbe.reason === "not-repository") return { kind: "not-repository" }
    return unavailable()
  }
  const root = rootProbe.stdout.trim()
  if (!root) return unavailable()

  const branchProbe = await probeGit(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd, timeoutMs)
  if (branchProbe.ok) {
    const branch = branchProbe.stdout.trim()
    if (branch) return { kind: "branch", branch, root }
  }

  // detached HEAD 时 symbolic-ref 必然失败，退回解析短 SHA。
  const shaProbe = await probeGit(["rev-parse", "--short", "HEAD"], cwd, timeoutMs)
  if (shaProbe.ok) {
    const shortSha = shaProbe.stdout.trim()
    if (shortSha) return { kind: "detached", shortSha, root }
  }

  return unavailable()
}

function changedFileStatus(status: string): GitChangedFile["status"] {
  if (status === "??") return "untracked"
  if (status === "AA" || status === "DD" || status.includes("U")) return "conflicted"
  if (status.includes("R")) return "renamed"
  if (status.includes("C")) return "copied"
  if (status.includes("D")) return "deleted"
  if (status.includes("A")) return "added"
  return "modified"
}

type GitLineStats = Pick<GitChangedFile, "addedLines" | "removedLines">

function numstatCount(value: string): number | null {
  if (value === "-") return null
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : null
}

/** 解析 `git diff --numstat -z`；rename/copy 的 header 路径为空，随后两个 NUL 字段分别是旧/新路径。 */
function parseGitNumstat(stdout: string): Map<string, GitLineStats> {
  const stats = new Map<string, GitLineStats>()
  const fields = stdout.split("\0")
  for (let index = 0; index < fields.length; index += 1) {
    const header = fields[index]
    if (!header) continue
    const firstTab = header.indexOf("\t")
    const secondTab = firstTab < 0 ? -1 : header.indexOf("\t", firstTab + 1)
    if (firstTab < 0 || secondTab < 0) continue

    const inlinePath = header.slice(secondTab + 1)
    let path = inlinePath
    if (!path) {
      index += 2
      path = fields[index] ?? ""
    }
    if (!path) continue
    stats.set(path, {
      addedLines: numstatCount(header.slice(0, firstTab)),
      removedLines: numstatCount(header.slice(firstTab + 1, secondTab)),
    })
  }
  return stats
}

function countTextLines(content: Buffer): number | null {
  if (content.includes(0)) return null
  if (content.length === 0) return 0
  let lines = content[content.length - 1] === 0x0a ? 0 : 1
  for (const byte of content) {
    if (byte === 0x0a) lines += 1
  }
  return lines
}

/** 未跟踪文件不在 `git diff HEAD` 中；仅对工作区内的小型普通文本文件计算新增行。 */
async function untrackedLineStats(
  cwd: string,
  path: string,
  remainingBytes: number,
): Promise<{ stats: GitLineStats; consumedBytes: number }> {
  const unavailable = { stats: { addedLines: null, removedLines: null }, consumedBytes: 0 }
  const root = resolve(cwd)
  const absolutePath = resolve(root, path)
  const relativePath = relative(root, absolutePath)
  if (isAbsolute(relativePath) || relativePath === ".." || relativePath.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`)) {
    return unavailable
  }
  try {
    const metadata = await lstat(absolutePath)
    if (!metadata.isFile() || metadata.isSymbolicLink()) return unavailable
    if (metadata.size > UNTRACKED_FILE_SIZE_LIMIT || metadata.size > remainingBytes) return unavailable
    const content = await readFile(absolutePath)
    const addedLines = countTextLines(content)
    return {
      stats: { addedLines, removedLines: addedLines === null ? null : 0 },
      consumedBytes: content.length,
    }
  } catch {
    return unavailable
  }
}

/**
 * 列出整个 Git 工作树的已跟踪修改、暂存修改和未跟踪文件。
 * `-z` 保留特殊文件名；rename/copy 记录取新路径并跳过随后的旧路径字段。
 * 非 Git、git 缺失、超时或其他探测失败返回 null，由调用方隐藏该区块。
 */
export async function detectGitChangedFiles(cwd: string, timeoutMs = 500): Promise<readonly GitChangedFile[] | null> {
  const [statusProbe, numstatProbe] = await Promise.all([
    probeGit(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd, timeoutMs),
    probeGit(["diff", "--no-ext-diff", "--numstat", "-z", "HEAD", "--"], cwd, timeoutMs),
  ])
  if (!statusProbe.ok) return null

  const fields = statusProbe.stdout.split("\0")
  const statusFiles: Array<Pick<GitChangedFile, "path" | "status">> = []
  for (let index = 0; index < fields.length; index += 1) {
    const record = fields[index]
    if (!record) continue
    const status = record.slice(0, 2)
    const path = record.slice(3)
    if (path) statusFiles.push({ path, status: changedFileStatus(status) })
    if (status.includes("R") || status.includes("C")) index += 1
  }

  const numstat = numstatProbe.ok ? parseGitNumstat(numstatProbe.stdout) : new Map<string, GitLineStats>()
  const files: GitChangedFile[] = []
  let remainingUntrackedBytes = UNTRACKED_TOTAL_SIZE_LIMIT
  for (const file of statusFiles) {
    let stats = numstat.get(file.path)
    if (!stats && file.status === "untracked") {
      const untracked = await untrackedLineStats(cwd, file.path, remainingUntrackedBytes)
      stats = untracked.stats
      remainingUntrackedBytes -= untracked.consumedBytes
    }
    files.push({
      ...file,
      addedLines: stats?.addedLines ?? null,
      removedLines: stats?.removedLines ?? null,
    })
  }
  return files
}
