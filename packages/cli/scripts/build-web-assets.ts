/** Web 生产资产构建：生成 app、syntax Worker、离线 WASM 与受限资源清单。 */

import { cp, mkdir, rm, writeFile } from "node:fs/promises"
import { resolve } from "node:path"

import { bundledSyntaxLanguages } from "../src/web/syntax/catalog.generated"
import { readBuiltWebAssets, type WebAssetsManifest } from "../src/web/bundle"

const cliRoot = resolve(import.meta.dir, "..")
const sourceRoot = resolve(cliRoot, "src")
const distRoot = resolve(cliRoot, "dist")
const syntaxDistRoot = resolve(distRoot, "web-syntax")

/** 构建所有运行时必需的 Web 资源；不会从网络下载任何资产。 */
async function main(): Promise<void> {
  await mkdir(distRoot, { recursive: true })
  await rm(syntaxDistRoot, { recursive: true, force: true })
  // Bun 的 Node 主包构建会把 Worker 依赖命名为 worker.js；Web 运行时只认
  // manifest 指向的固定文件名，故在最后生成 Web 资产前清理这一个旧副产物。
  await rm(resolve(distRoot, "worker.js"), { force: true })
  await mkdir(resolve(syntaxDistRoot, "lang"), { recursive: true })

  const appResult = await Bun.build({
    entrypoints: [resolve(sourceRoot, "web/app.tsx")],
    outdir: distRoot,
    entryNaming: "web.[ext]",
    target: "browser",
    minify: true,
  })
  const workerResult = await Bun.build({
    entrypoints: [resolve(sourceRoot, "web/syntax/worker.ts")],
    target: "browser",
    minify: true,
    // web-tree-sitter 同时发布 Node 分支；浏览器运行时不会走该分支，但 Bun 仍会
    // 静态分析其 dynamic import。保留为 external，避免把 Node builtin 误打进 Worker。
    external: ["module", "node:module"],
  })
  ensureBuildSucceeded(appResult, "Web app")
  ensureBuildSucceeded(workerResult, "Web syntax Worker")
  const workerOutput = workerResult.outputs.find(output => output.type.startsWith("text/javascript"))
  if (!workerOutput) throw new Error("Web syntax Worker 未生成 JavaScript 输出")
  await Bun.write(resolve(distRoot, "web-syntax-worker.js"), workerOutput)

  await cp(
    resolve(cliRoot, "node_modules/web-tree-sitter/tree-sitter.wasm"),
    resolve(syntaxDistRoot, "tree-sitter.wasm"),
  )

  const languageWasms: Record<string, string> = {}
  for (const entry of bundledSyntaxLanguages) {
    const source = resolve(sourceRoot, `tui/platform/assets/syntax/${entry.filetype}/${entry.wasmFileName}`)
    const output = `web-syntax/lang/${entry.assetId}.wasm`
    await cp(source, resolve(distRoot, output))
    languageWasms[entry.assetId] = output
  }

  const manifest: WebAssetsManifest = {
    version: 1,
    script: "web.js",
    style: "web.css",
    syntaxWorkerScript: "web-syntax-worker.js",
    treeSitterWasm: "web-syntax/tree-sitter.wasm",
    languageWasms,
  }
  await writeFile(resolve(distRoot, "web-assets.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8")

  // 在 build 阶段按生产 loader 再读一次，防止 manifest 与真实产物的名称漂移。
  const assets = await readBuiltWebAssets(distRoot)
  if (!assets || assets.languageWasms.size !== bundledSyntaxLanguages.length) {
    throw new Error("Web 资产构建不完整：manifest 未包含完整 Syntax Worker/WASM catalog")
  }
}

/** 统一把 Bun 构建日志收敛成可诊断错误，避免生成不完整 manifest。 */
function ensureBuildSucceeded(result: BuildArtifact, label: string): void {
  if (result.success) return
  throw new Error(`${label} 构建失败：${result.logs.map(log => log.message).join("\n")}`)
}

type BuildArtifact = {
  readonly success: boolean
  readonly logs: readonly { readonly message: string }[]
}

await main()
