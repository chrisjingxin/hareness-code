/** 加载 Web JS/CSS 构建产物；源码开发模式下即时构建同一份资源。 */

import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

export type WebAssets = {
  script: string
  style: string
}

/** 根据当前 bundle 模块所在目录解析 source 与 dist 两种运行形态的资源位置。 */
export function resolveWebBundleLocations(moduleDir: string): {
  builtDirectories: readonly string[]
  sourceEntrypoints: readonly string[]
} {
  return {
    builtDirectories: [
      resolve(moduleDir, "../../dist"),
      resolve(moduleDir),
    ],
    sourceEntrypoints: [
      resolve(moduleDir, "app.tsx"),
      resolve(moduleDir, "../src/web/app.tsx"),
    ],
  }
}

export async function browserBundle(): Promise<WebAssets> {
  // source 入口位于 src/web，编译后的 index.js 位于 dist；两种运行形态都要
  // 找到同一份 dist/web.{js,css}，否则生产 CLI 会错误解析到 packages/dist。
  const locations = resolveWebBundleLocations(import.meta.dir)
  // 从 src/web 直接运行时始终即时构建源码，避免旧 dist/web.js 掩盖刚修改的
  // Browser 代码；编译后的 dist/index.js 没有同目录 app.tsx，仍读取发布产物。
  const localSourceEntrypoint = locations.sourceEntrypoints[0]!
  if (existsSync(localSourceEntrypoint)) return buildSourceBundle(localSourceEntrypoint)
  const builtDirectories = locations.builtDirectories
  for (const directory of builtDirectories) {
    const built = resolve(directory, "web.js")
    const builtStyle = resolve(directory, "web.css")
    if (!existsSync(built) || !existsSync(builtStyle)) continue
    const [script, style] = await Promise.all([readFile(built, "utf8"), readFile(builtStyle, "utf8")])
    return { script, style }
  }

  const sourceEntrypoint = locations.sourceEntrypoints.find(entrypoint => existsSync(entrypoint))
  if (!sourceEntrypoint) throw new Error("Web app entrypoint is missing")
  return buildSourceBundle(sourceEntrypoint)
}

async function buildSourceBundle(sourceEntrypoint: string): Promise<WebAssets> {
  const result = await Bun.build({
    entrypoints: [sourceEntrypoint],
    target: "browser",
    minify: true,
  })
  const scriptOutput = result.outputs.find(output => output.path.endsWith(".js"))
  const styleOutput = result.outputs.find(output => output.path.endsWith(".css"))
  if (!result.success || !scriptOutput || !styleOutput) {
    throw new Error(
      result.logs.map(log => log.message).join("\n") || "Web bundle build failed",
    )
  }
  return {
    script: await scriptOutput.text(),
    style: await styleOutput.text(),
  }
}
