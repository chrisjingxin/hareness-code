/** 加载 Web JS/CSS/Worker 构建产物；源码开发模式下即时构建同一份资源。 */

import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

export type WebAssets = {
  script: string
  style: string
  syntaxWorkerScript: string
}

/**
 * Web 生产资产清单。所有路径都是 dist 目录内的相对路径，server 只会把它们投影为
 * 固定白名单 URL，不会使用请求路径访问文件系统。
 */
export type WebAssetsManifest = {
  readonly version: 1
  readonly script: string
  readonly style: string
  readonly syntaxWorkerScript: string
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

/** 加载当前运行形态所需的完整 Web 资产；source 与 dist 返回同一资源形状。 */
export async function browserBundle(): Promise<WebAssets> {
  const locations = resolveWebBundleLocations(import.meta.dir)
  const localSourceEntrypoint = locations.sourceEntrypoints[0]!
  if (existsSync(localSourceEntrypoint)) return buildSourceBundle(localSourceEntrypoint)
  for (const directory of locations.builtDirectories) {
    const assets = await readBuiltWebAssets(directory)
    if (assets) return assets
  }

  const sourceEntrypoint = locations.sourceEntrypoints.find(entrypoint => existsSync(entrypoint))
  if (!sourceEntrypoint) throw new Error("Web app entrypoint is missing")
  return buildSourceBundle(sourceEntrypoint)
}

async function buildSourceBundle(sourceEntrypoint: string): Promise<WebAssets> {
  const workerEntrypoint = resolve(import.meta.dir, "syntax/worker.ts")
  const appResult = await Bun.build({
    entrypoints: [sourceEntrypoint],
    target: "browser",
    minify: true,
    external: ["module", "node:module", "fs", "node:fs", "path", "node:path"],
  })
  const workerResult = await Bun.build({
    entrypoints: [workerEntrypoint],
    target: "browser",
    minify: true,
    external: ["module", "node:module", "fs", "node:fs", "path", "node:path"],
  })

  const scriptOutput = appResult.outputs.find(output => output.path.endsWith(".js"))
  const styleOutput = appResult.outputs.find(output => output.path.endsWith(".css"))
  const workerOutput = workerResult.outputs.find(output => output.path.endsWith(".js"))

  if (!appResult.success || !workerResult.success || !scriptOutput || !styleOutput || !workerOutput) {
    throw new Error(
      [...appResult.logs, ...workerResult.logs].map(log => log.message).join("\n") || "Web bundle build failed",
    )
  }

  const script = await scriptOutput.text()
  const style = await styleOutput.text()
  const syntaxWorkerScript = workerOutput ? await workerOutput.text() : ""

  return {
    script,
    style,
    syntaxWorkerScript,
  }
}

/** 读取生产构建清单；清单缺失表示该目录不是可运行的 Web dist。 */
export async function readBuiltWebAssets(directory: string): Promise<WebAssets | null> {
  const manifestPath = resolve(directory, "web-assets.json")
  if (!existsSync(manifestPath)) return null

  const raw = JSON.parse(await readFile(manifestPath, "utf8")) as unknown
  if (!isWebAssetsManifest(raw)) {
    throw new Error(`Web 资产清单无效：${manifestPath}`)
  }

  const [script, style, syntaxWorkerScript] = await Promise.all([
    readFile(resolveAssetPath(directory, raw.script), "utf8"),
    readFile(resolveAssetPath(directory, raw.style), "utf8"),
    readFile(resolveAssetPath(directory, raw.syntaxWorkerScript), "utf8"),
  ])
  return { script, style, syntaxWorkerScript }
}

/** 只接受 dist 内普通相对文件名，避免被损坏的清单带出构建目录。 */
function resolveAssetPath(directory: string, relativePath: string): string {
  if (!relativePath || relativePath.startsWith("/") || relativePath.split(/[\\/]+/).includes("..")) {
    throw new Error(`Web 资产路径无效：${relativePath}`)
  }
  return resolve(directory, relativePath)
}

function isWebAssetsManifest(value: unknown): value is WebAssetsManifest {
  if (!value || typeof value !== "object") return false
  const manifest = value as Partial<WebAssetsManifest>
  return manifest.version === 1
    && typeof manifest.script === "string"
    && typeof manifest.style === "string"
    && typeof manifest.syntaxWorkerScript === "string"
}
