/** Web bundle 测试：验证源码开发入口可以即时构建浏览器脚本。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

import { browserBundle, readBuiltWebAssets, resolveWebBundleLocations } from "../../src/web/bundle"

test("源码开发模式从 bundle 模块构建 Web bundle", async () => {
  const assets = await browserBundle()
  expect(assets.script.length).toBeGreaterThan(0)
  expect(assets.style.length).toBeGreaterThan(0)
  expect(assets.syntaxWorkerScript.length).toBeGreaterThan(0)
  expect(assets.script).toContain("error_name")
})

test("生产 manifest 只从固定 dist 内资源加载 app、worker 脚本", async () => {
  const directory = await mkdtemp(resolve(tmpdir(), "za38-web-assets-"))
  try {
    await Promise.all([
      writeFile(resolve(directory, "web.js"), "console.log('app')"),
      writeFile(resolve(directory, "web.css"), "body{}"),
      writeFile(resolve(directory, "web-syntax-worker.js"), "console.log('worker')"),
    ])
    await writeFile(resolve(directory, "web-assets.json"), JSON.stringify({
      version: 1,
      script: "web.js",
      style: "web.css",
      syntaxWorkerScript: "web-syntax-worker.js",
    }))

    const assets = await readBuiltWebAssets(directory)
    expect(assets?.script).toContain("app")
    expect(assets?.syntaxWorkerScript).toContain("worker")

    await writeFile(resolve(directory, "web-assets.json"), JSON.stringify({
      version: 1,
      script: "../web.js",
      style: "web.css",
      syntaxWorkerScript: "web-syntax-worker.js",
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
