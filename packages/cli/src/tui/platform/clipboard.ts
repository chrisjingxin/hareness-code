/** 系统剪贴板复制工具函数。 */

import { spawn } from "node:child_process"

export async function copyToClipboard(text: string): Promise<boolean> {
  return new Promise<boolean>(resolve => {
    try {
      let proc
      if (process.platform === "darwin") {
        proc = spawn("pbcopy")
      } else if (process.platform === "win32") {
        proc = spawn("clip")
      } else {
        proc = spawn("wl-copy")
      }

      proc.on("error", () => {
        if (process.platform !== "darwin" && process.platform !== "win32") {
          try {
            const xclip = spawn("xclip", ["-selection", "clipboard"])
            xclip.on("error", () => resolve(false))
            xclip.on("close", code => resolve(code === 0))
            xclip.stdin.write(text)
            xclip.stdin.end()
            return
          } catch {
            resolve(false)
            return
          }
        }
        resolve(false)
      })

      proc.on("close", code => {
        resolve(code === 0)
      })

      proc.stdin.write(text)
      proc.stdin.end()
    } catch {
      resolve(false)
    }
  })
}
