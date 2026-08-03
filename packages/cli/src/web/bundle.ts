/** 加载 Web 构建产物；源码开发模式下则即时构建浏览器脚本。 */

import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

export async function browserBundle(): Promise<string> {
  const built = resolve(import.meta.dir, "../../dist/web.js")
  if (existsSync(built)) return readFile(built, "utf8")
  const result = await Bun.build({
    entrypoints: [resolve(import.meta.dir, "app.ts")],
    target: "browser",
    minify: true,
  })
  if (!result.success || !result.outputs[0]) {
    throw new Error(
      result.logs.map(log => log.message).join("\n") || "Web bundle build failed",
    )
  }
  return result.outputs[0].text()
}
