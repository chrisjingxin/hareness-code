/** Web 分层规则测试：presentation 不持有 IPC、agent-transport、handoff 凭据；composition root 是唯一装配入口。 */

import { expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const webRoot = resolve(import.meta.dir, "../../src/web")
const interactiveRoot = resolve(import.meta.dir, "../../src/interactive")

test("Web 根目录只保留组合入口与白名单基础设施文件", () => {
  const entries = readdirSync(webRoot, { withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => entry.name)
    .sort()
  expect(entries).toEqual([
    "agent-transport.ts",
    "app.tsx",
    "bootstrap-url.ts",
    "browser.ts",
    "bundle.ts",
    "connection-supervisor.ts",
    "css.d.ts",
    "handoff-coordinator.ts",
    "handoff-port.ts",
    "html.ts",
    "server.ts",
  ])
})

test("Web application 不直连 AgentClient、transport、JSON-RPC method string 或 handoff 凭据", () => {
  const imports = layerImports(resolve(webRoot, "application"))
  expect(imports).not.toMatch(/from\s+["'][^"']*\/ipc\//)
  expect(imports).not.toMatch(/from\s+["'][^"']*agent-transport/)
  expect(imports).not.toMatch(/from\s+["'][^"']*connection-supervisor/)
  expect(imports).not.toMatch(/from\s+["'][^"']*handoff-coordinator/)
  expect(imports).not.toMatch(/AgentClient/)
  expect(imports).not.toMatch(/WebSocketRpcTransport/)
  expect(imports).not.toMatch(/attachment_id/)
  expect(imports).not.toMatch(/authenticate/)
})

test("Web presentation 不直连 IPC、agent-transport、handoff 凭据或 JSON-RPC method string", () => {
  const imports = layerImports(resolve(webRoot, "presentation"))
  expect(imports).not.toMatch(/from\s+["'][^"']*\/ipc\//)
  expect(imports).not.toMatch(/from\s+["'][^"']*agent-transport/)
  expect(imports).not.toMatch(/from\s+["'][^"']*handoff-(?:port|coordinator)/)
  expect(imports).not.toMatch(/AgentClient/)
  expect(imports).not.toMatch(/WebSocketRpcTransport/)
  expect(imports).not.toMatch(/attachment_id|attachment_token|authToken/)
  expect(imports).not.toMatch(/skills\.set_enabled|host\.control\.|threads\.open|mcp\.add/)
  expect(imports).not.toMatch(/dangerouslySetInnerHTML/)
})

test("Web composition root 是唯一同时装配 infrastructure 与 React 的入口", () => {
  const app = readFileSync(resolve(webRoot, "app.tsx"), "utf8")
  expect(app).toContain("createInteractiveController")
  expect(app).toContain("createRoot")
  expect(app).toContain("createWebInteractiveAdapter")
  expect(app).toContain("createWebHandoffPort")
  expect(app).toContain("WebApp")
})

test("presentation 可以使用交互式类型与 @za38/protocol 枚举", () => {
  const source = readDirectory(resolve(webRoot, "presentation"))
  // 类型导入允许指向 interactive 与 @za38/protocol。
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
