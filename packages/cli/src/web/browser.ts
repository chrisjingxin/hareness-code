/** 系统 Browser opener adapter；测试通过注入 fake spawn 与平台隔离子进程。 */

import { spawn, type ChildProcess } from "node:child_process"

import type { WebBrowserOpener } from "../presentation-coordinator"

export type SpawnAdapter = (
  command: string,
  args: readonly string[],
  options: { detached: boolean; stdio: "ignore" },
) => ChildProcess

export type BrowserOpenerOptions = {
  spawn?: SpawnAdapter
  platform?: NodeJS.Platform
}

/** 按平台启动默认浏览器；Promise 只在进程成功 spawn 后 resolve。 */
export function createSystemBrowserOpener(
  options: BrowserOpenerOptions = {},
): WebBrowserOpener {
  const spawnFn = options.spawn ?? spawn
  const platform = options.platform ?? process.platform
  return (url: string) =>
    new Promise<void>((resolve, reject) => {
      const executable = platform === "darwin"
        ? "open"
        : platform === "win32"
          ? "cmd"
          : "xdg-open"
      const args = platform === "win32" ? ["/c", "start", "", url] : [url]
      let child: ChildProcess
      try {
        child = spawnFn(executable, args, { detached: true, stdio: "ignore" })
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)))
        return
      }
      let settled = false
      child.once("error", error => {
        if (settled) return
        settled = true
        reject(error)
      })
      child.once("spawn", () => {
        if (settled) return
        settled = true
        child.unref()
        resolve()
      })
    })
}
