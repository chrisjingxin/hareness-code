/** 加载 Web JS/CSS 构建产物；源码开发模式下即时构建同一份资源。 */

import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

import { bundledSyntaxLanguages } from "./syntax/catalog.generated"

export type WebAssets = {
  script: string
  style: string
  syntaxWorkerScript: string
  treeSitterWasm: Uint8Array
  languageWasms: ReadonlyMap<string, Uint8Array>
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
  const locations = resolveWebBundleLocations(import.meta.dir)
  const localSourceEntrypoint = locations.sourceEntrypoints[0]!
  if (existsSync(localSourceEntrypoint)) return buildSourceBundle(localSourceEntrypoint)
  const builtDirectories = locations.builtDirectories
  for (const directory of builtDirectories) {
    const built = resolve(directory, "web.js")
    const builtStyle = resolve(directory, "web.css")
    if (!existsSync(built) || !existsSync(builtStyle)) continue
    const [script, style] = await Promise.all([readFile(built, "utf8"), readFile(builtStyle, "utf8")])
    return {
      script,
      style,
      syntaxWorkerScript: "",
      treeSitterWasm: new Uint8Array(),
      languageWasms: new Map(),
    }
  }

  const sourceEntrypoint = locations.sourceEntrypoints.find(entrypoint => existsSync(entrypoint))
  if (!sourceEntrypoint) throw new Error("Web app entrypoint is missing")
  return buildSourceBundle(sourceEntrypoint)
}

async function buildSourceBundle(sourceEntrypoint: string): Promise<WebAssets> {
  const workerEntrypoint = resolve(import.meta.dir, "syntax/worker.ts")
  const result = await Bun.build({
    entrypoints: [sourceEntrypoint, workerEntrypoint],
    target: "browser",
    minify: true,
    external: ["module", "node:module", "fs", "node:fs", "path", "node:path"],
  })

  const scriptOutput = result.outputs.find(output => output.path.endsWith("app.js") || output.path.endsWith("app.tsx.js")) ?? result.outputs[0]
  const styleOutput = result.outputs.find(output => output.path.endsWith(".css"))
  const workerOutput = result.outputs.find(output => output.path.endsWith("worker.js") || output.path.endsWith("worker.ts.js")) ?? result.outputs[1]

  if (!result.success || !scriptOutput || !styleOutput) {
    throw new Error(
      result.logs.map(log => log.message).join("\n") || "Web bundle build failed",
    )
  }

  const script = await scriptOutput.text()
  const style = await styleOutput.text()
  const syntaxWorkerScript = workerOutput ? await workerOutput.text() : ""

  const treeSitterWasmPath = resolve(import.meta.dir, "../../node_modules/web-tree-sitter/tree-sitter.wasm")
  const treeSitterWasm = existsSync(treeSitterWasmPath)
    ? new Uint8Array(await readFile(treeSitterWasmPath))
    : new Uint8Array()

  const languageWasms = new Map<string, Uint8Array>()
  for (const entry of bundledSyntaxLanguages) {
    const wasmPath = resolve(import.meta.dir, `../tui/platform/assets/syntax/${entry.filetype}/${entry.wasmFileName}`)
    if (existsSync(wasmPath)) {
      languageWasms.set(entry.assetId, new Uint8Array(await readFile(wasmPath)))
    }
  }

  return {
    script,
    style,
    syntaxWorkerScript,
    treeSitterWasm,
    languageWasms,
  }
}
