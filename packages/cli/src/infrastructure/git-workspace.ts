/** Git 工作区状态探测：把 git 命令输出收敛为四种稳定状态，供 TUI 与 Web 共用展示。 */

import { execFile } from "node:child_process"
import { promisify } from "node:util"

import type { GitWorkspaceState } from "../interactive/runtime"

const execFileAsync = promisify(execFile)

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
