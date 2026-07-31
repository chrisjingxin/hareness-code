/** TUI 分层规则测试：固定组合入口和单向依赖边界。 */

import { expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const tuiRoot = resolve(import.meta.dir, "../../src/tui")

test("TUI 根目录只保留组合入口", () => {
  const rootFiles = readdirSync(tuiRoot, { withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => entry.name)
    .sort()

  expect(rootFiles).toEqual(["app.tsx"])
})

test("Presentation 不直连 IPC，Application 不反向依赖 Presentation", () => {
  expect(layerImports("presentation")).not.toMatch(/(?:^|[/"])ipc(?:[/"]|$)/m)
  expect(layerImports("application")).not.toMatch(/(?:^|[/"])presentation(?:[/"]|$)/m)
})

test("语法资源维护脚本写入 Platform canonical 路径", () => {
  const script = readFileSync(resolve(import.meta.dir, "../../scripts/vendor-syntax-assets.ts"), "utf8")
  expect(script).toContain('resolve(import.meta.dir, "../src/tui/platform")')
  expect(script).toContain('resolve(platformRoot, "assets/syntax")')
  expect(script).toContain('resolve(platformRoot, "generated-syntax-parsers.ts")')
  expect(script).not.toContain('resolve(tuiRoot, "assets/syntax")')
})

function layerImports(layer: "application" | "presentation"): string {
  const directory = resolve(tuiRoot, layer)
  return readdirSync(directory, { withFileTypes: true })
    .filter(entry => entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name))
    .map(entry => readFileSync(resolve(directory, entry.name), "utf8"))
    .flatMap(source => source.match(/^import .*$/gm) ?? [])
    .join("\n")
}
