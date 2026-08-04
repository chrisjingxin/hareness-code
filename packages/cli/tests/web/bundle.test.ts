/** Web bundle 测试：验证源码开发入口可以即时构建浏览器脚本。 */

import { expect, test } from "bun:test"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

import { browserBundle, readBuiltWebAssets, resolveWebBundleLocations } from "../../src/web/bundle"
import { bundledSyntaxLanguages } from "../../src/web/syntax/catalog.generated"

test("源码开发模式从 bundle 模块构建 Web bundle", async () => {
  const assets = await browserBundle()
  expect(assets.script.length).toBeGreaterThan(0)
  expect(assets.style.length).toBeGreaterThan(0)
  expect(assets.syntaxWorkerScript.length).toBeGreaterThan(0)
  expect(assets.treeSitterWasm.length).toBeGreaterThan(0)
  expect(assets.languageWasms.size).toBe(bundledSyntaxLanguages.length)
  expect(assets.script).toContain("error_name")
})

test("生产 manifest 只从固定 dist 内资源加载 app、worker 与完整 WASM catalog", async () => {
  const directory = await mkdtemp(resolve(tmpdir(), "za38-web-assets-"))
  try {
    await mkdir(resolve(directory, "web-syntax/lang"), { recursive: true })
    await Promise.all([
      writeFile(resolve(directory, "web.js"), "console.log('app')"),
      writeFile(resolve(directory, "web.css"), "body{}"),
      writeFile(resolve(directory, "web-syntax-worker.js"), "console.log('worker')"),
      writeFile(resolve(directory, "web-syntax/tree-sitter.wasm"), new Uint8Array([0, 97, 115, 109])),
      ...bundledSyntaxLanguages.map(entry => writeFile(
        resolve(directory, `web-syntax/lang/${entry.assetId}.wasm`),
        new Uint8Array([entry.assetId.length]),
      )),
    ])
    await writeFile(resolve(directory, "web-assets.json"), JSON.stringify({
      version: 1,
      script: "web.js",
      style: "web.css",
      syntaxWorkerScript: "web-syntax-worker.js",
      treeSitterWasm: "web-syntax/tree-sitter.wasm",
      languageWasms: Object.fromEntries(
        bundledSyntaxLanguages.map(entry => [entry.assetId, `web-syntax/lang/${entry.assetId}.wasm`]),
      ),
    }))

    const assets = await readBuiltWebAssets(directory)
    expect(assets?.script).toContain("app")
    expect(assets?.syntaxWorkerScript).toContain("worker")
    expect(assets?.treeSitterWasm).toEqual(new Uint8Array([0, 97, 115, 109]))
    expect(assets?.languageWasms.size).toBe(bundledSyntaxLanguages.length)

    await writeFile(resolve(directory, "web-assets.json"), JSON.stringify({
      version: 1,
      script: "../web.js",
      style: "web.css",
      syntaxWorkerScript: "web-syntax-worker.js",
      treeSitterWasm: "web-syntax/tree-sitter.wasm",
      languageWasms: Object.fromEntries(
        bundledSyntaxLanguages.map(entry => [entry.assetId, `web-syntax/lang/${entry.assetId}.wasm`]),
      ),
    }))
    await expect(readBuiltWebAssets(directory)).rejects.toThrow("Web 资产路径无效")
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test("生产 dist 入口也解析到 cli/dist 资源，不误指向 packages/dist", () => {
  const distDir = resolve(import.meta.dir, "../../dist")
  const locations = resolveWebBundleLocations(distDir)
  expect(locations.builtDirectories).toContain(distDir)
  expect(locations.sourceEntrypoints).toContain(resolve(import.meta.dir, "../../src/web/app.tsx"))
})
