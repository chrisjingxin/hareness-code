/** Web 生产资产构建：生成 app、syntax Worker 脚本与受限资源清单。 */

import { mkdir, rm, writeFile } from "node:fs/promises"
import { resolve } from "node:path"

import { readBuiltWebAssets, type WebAssetsManifest } from "../src/web/bundle"

const cliRoot = resolve(import.meta.dir, "..")
const sourceRoot = resolve(cliRoot, "src")
const distRoot = resolve(cliRoot, "dist")

/** 构建所有运行时必需的 Web 资源；不会从网络下载任何资产。 */
async function main(): Promise<void> {
  await mkdir(distRoot, { recursive: true })

  // Bun 的 Node 主包构建会把 Worker 依赖命名为 worker.js；Web 运行时只认
  // manifest 指向的固定文件名，故在最后生成 Web 资产前清理这一个旧副产物。
  await rm(resolve(distRoot, "worker.js"), { force: true })

  const appResult = await Bun.build({
    entrypoints: [resolve(sourceRoot, "web/app.tsx")],
    outdir: distRoot,
    target: "browser",
    minify: true,
  })
  const workerResult = await Bun.build({
    entrypoints: [resolve(sourceRoot, "web/syntax/worker.ts")],
    target: "browser",
    minify: true,
    external: ["module", "node:module"],
  })
  ensureBuildSucceeded(appResult, "Web app")
  ensureBuildSucceeded(workerResult, "Web syntax Worker")
  const workerOutput = workerResult.outputs.find(output => output.type.startsWith("text/javascript"))
  if (!workerOutput) throw new Error("Web syntax Worker 未生成 JavaScript 输出")
  await Bun.write(resolve(distRoot, "web-syntax-worker.js"), workerOutput)

  // Bun 1.3.x 会忽略 entryNaming，入口输出固定命名为 app.js/app.css；这里从
  // 构建结果读取实际输出并显式写入 manifest 的固定文件名，防止服务端加载到
  // 上次构建残留的陈旧 web.js/web.css。
  const scriptOutput = appResult.outputs.find(output => output.type.startsWith("text/javascript"))
  const styleOutput = appResult.outputs.find(output => output.type.startsWith("text/css"))
  if (!scriptOutput || !styleOutput) {
    throw new Error(`Web app 构建缺少脚本或样式输出：${appResult.outputs.map(output => output.path).join(", ")}`)
  }
  await Promise.all([
    Bun.write(resolve(distRoot, "web.js"), scriptOutput),
    Bun.write(resolve(distRoot, "web.css"), styleOutput),
  ])
  // 清理 Bun 按默认命名写出的 app.js/app.css 副产物，避免与 manifest 文件混淆。
  await rm(resolve(distRoot, "app.js"), { force: true })
  await rm(resolve(distRoot, "app.css"), { force: true })

  const manifest: WebAssetsManifest = {
    version: 1,
    script: "web.js",
    style: "web.css",
    syntaxWorkerScript: "web-syntax-worker.js",
  }
  await writeFile(resolve(distRoot, "web-assets.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8")

  // 在 build 阶段按生产 loader 再读一次，防止 manifest 与真实产物的名称漂移。
  const assets = await readBuiltWebAssets(distRoot)
  if (!assets || !assets.syntaxWorkerScript) {
    throw new Error("Web 资产构建不完整：manifest 未包含完整 Syntax Worker 脚本")
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
