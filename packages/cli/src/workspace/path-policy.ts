/**
 * 工作区路径策略：把任意用户输入解析为"工作区根内的真实路径"。
 *
 * 所有对外路径一律相对 workspace 根；绝对路径、`..` 穿越、NUL 字节、
 * Windows UNC 前缀在进入 fs 之前被拒绝，symlink 目标经 realpath 后
 * 必须仍落在根内（分隔边界校验，不能裸 startsWith root）。
 */

import { realpath } from "node:fs/promises"
import path from "node:path"

import type { WorkspaceErrorCode } from "./types"

/** 携带稳定错误码的工作区错误；explorer 据此收敛 outcome 与界面状态。 */
export class WorkspaceError extends Error {
  readonly code: WorkspaceErrorCode

  constructor(code: WorkspaceErrorCode, message: string) {
    super(message)
    this.name = "WorkspaceError"
    this.code = code
  }
}

/** 构造错误消息的单一入口：文案只在此处维护，保证全部脱敏。 */
export function workspaceError(code: WorkspaceErrorCode, message: string): WorkspaceError {
  return new WorkspaceError(code, message)
}

/**
 * 解析工作区根（构造时一次，之后为固定根）。
 * 失败时由调用方把树置为 error 状态，不阻断 CLI 启动。
 */
export async function resolveWorkspaceRoot(workspace: string): Promise<string> {
  return realpath(workspace)
}

/**
 * 相对路径安全校验：拒绝空串、绝对路径、NUL 字节、`..` 段、
 * `\\`（Windows UNC）与 `//`（POSIX 网络路径）前缀。
 */
export function isSafeRelativePath(relativePath: string): boolean {
  if (relativePath === "") return false
  if (path.isAbsolute(relativePath)) return false
  if (relativePath.includes("\0")) return false
  if (relativePath.startsWith("\\\\") || relativePath.startsWith("//")) return false
  const normalized = path.normalize(relativePath)
  if (normalized === ".." || normalized.startsWith(`..${path.sep}`) || normalized.split(path.sep).includes("..")) return false
  return true
}

/**
 * 把相对路径解析为根内的真实路径：安全校验 → resolve → realpath →
 * 分隔边界校验。symlink 目标越界或文件不存在都收敛为稳定错误码。
 */
export async function resolveWithinRoot(root: string, relativePath: string): Promise<string> {
  if (!isSafeRelativePath(relativePath)) {
    throw workspaceError("invalid-path", "无效的文件路径")
  }
  const target = path.resolve(root, relativePath)
  let realTarget: string
  try {
    realTarget = await realpath(target)
  } catch (error) {
    throw mapFsError(error)
  }
  if (realTarget !== root && !realTarget.startsWith(root === path.sep ? root : root + path.sep)) {
    throw workspaceError("outside-workspace", "路径超出工作区范围")
  }
  return realTarget
}

/** 把常见 fs 错误映射为稳定错误码；未知错误统一收敛为 io-error，不透传系统细节。 */
export function mapFsError(error: unknown): WorkspaceError {
  const err = error as NodeJS.ErrnoException
  switch (err.code) {
    case "ENOENT":
      return workspaceError("not-found", "文件或目录不存在")
    case "EACCES":
    case "EPERM":
      return workspaceError("permission-denied", "没有访问权限")
    case "EISDIR":
      return workspaceError("not-file", "目标是目录")
    case "ENOTDIR":
      return workspaceError("not-directory", "路径不是目录")
    default:
      return workspaceError("io-error", "读取文件失败")
  }
}
