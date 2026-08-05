/** Web 分层规则测试：presentation 不持有 IPC、agent-transport、handoff 凭据；composition root 是唯一装配入口。 */

import { expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const webRoot = resolve(import.meta.dir, "../../src/web")
const interactiveRoot = resolve(import.meta.dir, "../../src/interactive")
const cliSrcRoot = resolve(import.meta.dir, "../../src")

test("Web 根目录只保留组合入口与白名单基础设施文件", () => {
  const entries = readdirSync(webRoot, { withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => entry.name)
    .sort()
  expect(entries).toEqual([
    "app.tsx",
    "browser.ts",
    "bundle.ts",
    "css.d.ts",
    "html.ts",
    "server.ts",
    "ui-client.ts",
  ])
})

test("Web 根目录不再存在 Agent 直连与 attachment 相关模块", () => {
  const entries = readdirSync(webRoot, { withFileTypes: true })
    .map(entry => entry.name)
    .sort()
  expect(entries).not.toContain("agent-transport.ts")
  expect(entries).not.toContain("bootstrap-url.ts")
  expect(entries).not.toContain("connection-supervisor.ts")
  expect(entries).not.toContain("handoff-coordinator.ts")
  expect(entries).not.toContain("handoff-port.ts")
})

test("Web application 不直连 Agent/IPC、凭据或服务器侧 Coordinator/Gateway 实现", () => {
  const imports = layerImports(resolve(webRoot, "application"))
  expect(imports).not.toMatch(/from\s+["'][^"']*\/ipc\//)
  expect(imports).not.toMatch(/AgentClient/)
  expect(imports).not.toMatch(/WebSocketRpcTransport/)
  expect(imports).not.toMatch(/authenticate/)
  expect(imports).not.toMatch(/attachment/)
  expect(imports).not.toMatch(/from\s+["'][^"']*presentation-coordinator\/coordinator/)
  expect(imports).not.toMatch(/from\s+["'][^"']*presentation-coordinator\/web-ui-gateway/)
})

test("Web presentation 不直连 IPC、Agent 凭据、服务器侧 Coordinator 或 JSON-RPC method string", () => {
  const imports = layerImports(resolve(webRoot, "presentation"))
  expect(imports).not.toMatch(/from\s+["'][^"']*\/ipc\//)
  expect(imports).not.toMatch(/AgentClient/)
  expect(imports).not.toMatch(/WebSocketRpcTransport/)
  expect(imports).not.toMatch(/attachment_id|attachment_token|authToken/)
  expect(imports).not.toMatch(/skills\.set_enabled|host\.control\.|threads\.open|mcp\.add/)
  expect(imports).not.toMatch(/from\s+["'][^"']*presentation-coordinator\/coordinator/)
  expect(imports).not.toMatch(/from\s+["'][^"']*presentation-coordinator\/web-ui-gateway/)
  expect(imports).not.toMatch(/dangerouslySetInnerHTML/)
})

test("Web 生产源码无 AgentClient / host.control / attachment token 残留", () => {
  const allSource = readAllSourceFiles(webRoot)
  expect(allSource).not.toMatch(/AgentClient/)
  expect(allSource).not.toMatch(/host\.control\./)
  expect(allSource).not.toMatch(/attachment/)
  expect(allSource).not.toMatch(/authenticate/)
})

test("Web syntax 高亮模块独立且不跨端引用 tui/platform 或 ../tui/", () => {
  const syntaxDir = resolve(webRoot, "syntax")
  const imports = layerImports(syntaxDir)
  expect(imports).not.toMatch(/from\s+["'][^"']*tui\/platform/)
  expect(imports).not.toMatch(/from\s+["'][^"']*\.\.\/tui/)
})

test("CLI 生产代码中无 web-tree-sitter 依赖引用", () => {
  const allSource = readAllSourceFiles(cliSrcRoot)
  expect(allSource).not.toMatch(/web-tree-sitter/)
})

test("Web composition root 是唯一装配 WebUiClient/Adapter 与 React 的入口", () => {
  const app = readFileSync(resolve(webRoot, "app.tsx"), "utf8")
  expect(app).toContain("createWebUiClient")
  expect(app).toContain("createRoot")
  expect(app).toContain("createWebInteractiveAdapter")
  expect(app).toContain("WebApp")
  expect(app).not.toContain("createInteractiveController")
  expect(app).not.toContain("AgentClient")
  expect(app).not.toContain("host.control")
})

test("createInteractiveController 生产调用点仅 CLI Composition Root 一处（D-01）", () => {
  const files = sourceFiles(cliSrcRoot)
    .filter(file => !file.endsWith("interactive/controller.ts"))
  const callers = files.filter(file => readFileSync(file, "utf8").includes("createInteractiveController("))
  expect(callers.map(file => file.replace(`${cliSrcRoot}/`, ""))).toEqual(["index.ts"])
})

test("presentation 可以使用交互式类型与 @za38/protocol 枚举", () => {
  const source = readDirectory(resolve(webRoot, "presentation"))
  expect(source).toMatch(/from\s+["']\.\.\/\.\.\/interactive\//)
  expect(source).toMatch(/from\s+["']@za38\/protocol/)
})

test("interactive 共享模块不依赖 TUI/Web/React/OpenTUI/DOM", () => {
  const imports = layerImports(interactiveRoot)
  expect(imports).not.toMatch(/(?:^|[/"])\.\.\/tui(?:[/"]|$)/m)
  expect(imports).not.toMatch(/(?:^|[/"])\.\.\/web(?:[/"]|$)/m)
  expect(imports).not.toMatch(/@opentui/m)
  expect(imports).not.toMatch(/(?:^|[/"])react(?:[/"]|$)/m)
  expect(imports).not.toMatch(/(?:^|[/"])react-dom(?:[/"]|$)/m)
  expect(imports).not.toMatch(/(?:^|[/"])happy-dom(?:[/"]|$)/m)
})

function readDirectory(directory: string): string {
  return readdirSync(directory, { withFileTypes: true })
    .filter(entry => entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name))
    .map(entry => readFileSync(resolve(directory, entry.name), "utf8"))
    .join("\n")
}

function layerImports(directory: string): string {
  return readdirSync(directory, { withFileTypes: true })
    .filter(entry => entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name))
    .map(entry => readFileSync(resolve(directory, entry.name), "utf8"))
    .flatMap(source => source.match(/^import .*$/gm) ?? [])
    .join("\n")
}

function readAllSourceFiles(dir: string): string {
  let results: string[] = []
  for (const file of sourceFiles(dir)) results.push(readFileSync(file, "utf8"))
  return results.join("\n")
}

/** 递归收集目录内全部 TS/TSX 源文件绝对路径。 */
function sourceFiles(dir: string): string[] {
  let results: string[] = []
  const entries = readdirSync(dir, { withFileTypes: true })
  for (const entry of entries) {
    const fullPath = resolve(dir, entry.name)
    if (entry.isDirectory()) {
      results.push(...sourceFiles(fullPath))
    } else if (/\.[cm]?[jt]sx?$/.test(entry.name)) {
      results.push(fullPath)
    }
  }
  return results
}
