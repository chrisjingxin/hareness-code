/** 分层规则测试：固定组合入口和单向依赖边界。 */

import { expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const tuiRoot = resolve(import.meta.dir, "../../src/tui")
const interactiveRoot = resolve(import.meta.dir, "../../src/interactive")

test("TUI 根目录只保留组合入口", () => {
  const rootFiles = readdirSync(tuiRoot, { withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => entry.name)
    .sort()

  expect(rootFiles).toEqual(["app.tsx"])
})

test("Presentation 不直连 IPC，Application 不反向依赖 Presentation", () => {
  expect(layerImports(resolve(tuiRoot, "presentation"))).not.toMatch(/(?:^|[/"])ipc(?:[/"]|$)/m)
  expect(layerImports(resolve(tuiRoot, "application"))).not.toMatch(/(?:^|[/"])presentation(?:[/"]|$)/m)
})

test("interactive 共享模块不依赖 TUI/Web/React/OpenTUI/DOM", () => {
  const imports = layerImports(resolve(interactiveRoot))
  expect(imports).not.toMatch(/(?:^|[/"])\.\.\/tui(?:[/"]|$)/m)
  expect(imports).not.toMatch(/(?:^|[/"])\.\.\/web(?:[/"]|$)/m)
  expect(imports).not.toMatch(/@opentui/m)
  expect(imports).not.toMatch(/(?:^|[/"])react(?:[/"]|$)/m)
  expect(imports).not.toMatch(/(?:^|[/"])react-dom(?:[/"]|$)/m)
})

test("TUI Application 只依赖 interactive 与终端表现模块，不直接调用 IPC", () => {
  const imports = layerImports(resolve(tuiRoot, "application"))
  expect(imports).not.toMatch(/(?:^|[/"])\.\.\/\.\.\/ipc(?:[/"]|$)/m)
})

test("语法资源维护脚本写入 Platform canonical 路径", () => {
  const script = readFileSync(resolve(import.meta.dir, "../../scripts/vendor-syntax-assets.ts"), "utf8")
  expect(script).toContain('resolve(import.meta.dir, "../src/tui/platform")')
  expect(script).toContain('resolve(platformRoot, "assets/syntax")')
  expect(script).toContain('resolve(platformRoot, "generated-syntax-parsers.ts")')
  expect(script).not.toContain('resolve(tuiRoot, "assets/syntax")')
})

function layerImports(directory: string): string {
  return readdirSync(directory, { withFileTypes: true })
    .filter(entry => entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name))
    .map(entry => readFileSync(resolve(directory, entry.name), "utf8"))
    .flatMap(source => source.match(/^import .*$/gm) ?? [])
    .join("\n")
}
