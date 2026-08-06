/**
 * 最终架构验收矩阵（方案 §14，A-01~A-12）。
 *
 * 每项验收必须有可重复执行的断言：静态/架构断言直接在本文件执行；单元级与
 * 集成级验收引用对应测试套件文件存在性并附代表性断言；端到端连续性（A-05）
 * 由 `test:web:e2e`（tests/e2e/，真实浏览器 + fake Agent）覆盖，本文件断言
 * 该脚本与用例存在。证据台账记录在 ZC-115。
 */

import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { expectNoMatch, layerImports, readAllSourceFiles, sourceFiles } from "./arch-imports"

const cliSrcRoot = resolve(import.meta.dir, "../../src")
const tuiRoot = resolve(cliSrcRoot, "tui")
const webRoot = resolve(cliSrcRoot, "web")
const interactiveRoot = resolve(cliSrcRoot, "interactive")
const presentationSharedRoot = resolve(cliSrcRoot, "presentation-shared")
const coordinatorRoot = resolve(cliSrcRoot, "presentation-coordinator")
const repoRoot = resolve(import.meta.dir, "../../../..")

test("A-01 单一 InteractiveController：生产调用点仅 CLI Composition Root", () => {
  const callers = sourceFiles(cliSrcRoot)
    .filter(file => !file.endsWith("interactive/controller.ts"))
    .filter(file => readFileSync(file, "utf8").includes("createInteractiveController("))
  expect(callers.map(file => file.replace(`${cliSrcRoot}/`, ""))).toEqual(["index.ts"])
})

test("A-02 共享 Timeline reducer 唯一：TUI/Web 不直接解析 Agent Event", () => {
  // Agent Event 的 sequence 校验与 Timeline 纯投影只允许在 interactive/ 内
  // （state.ts reducer 与 timeline-feature）；TUI/Web 只消费序列化视图。
  const tuiAndWeb = readAllSourceFiles(tuiRoot) + readAllSourceFiles(webRoot)
  expectNoMatch(tuiAndWeb, /EventType\./, "A-02 TUI/Web 不得引用 EventType")
  expectNoMatch(tuiAndWeb, /applyAgentEvent|processAgentEvent/, "A-02 TUI/Web 不得直接消费 Agent Event")
})

test("A-03 Browser 不创建 AgentClient/Controller，不连接 Python Host", () => {
  const webSource = readAllSourceFiles(webRoot)
  expectNoMatch(webSource, /AgentClient/, "A-03 Browser 不得创建 AgentClient")
  expectNoMatch(webSource, /createInteractiveController/, "A-03 Browser 不得自建 Controller")
  expectNoMatch(webSource, /host\.control\./, "A-03 Browser 不得使用 Host 控制权")
  expectNoMatch(webSource, /attachment/, "A-03 Browser 不得接触 attachment token")
})

test("A-04 Handoff 只改变 Presentation owner：Coordinator 零 host.control", () => {
  const coordinatorSource = readAllSourceFiles(coordinatorRoot)
  expectNoMatch(coordinatorSource, /host\.control/, "A-04 Coordinator 不得调用 Host 控制权")
  expectNoMatch(coordinatorSource, /ControlLease|attachment/, "A-04 Coordinator 不得接触 Host 控制租约/attachment")
})

test("A-05 TUI→Web→TUI 连续性：E2E 脚本与真实浏览器用例存在", () => {
  const rootPackage = JSON.parse(readFileSync(resolve(repoRoot, "package.json"), "utf8")) as {
    scripts: Record<string, string>
  }
  expect(rootPackage.scripts["test:web:e2e"]).toBeDefined()
  // 连续性断言（A-05）由 tests/e2e/ 的真实浏览器用例执行（Controller 未重建、
  // Thread/Timeline 连续）；此处保证用例文件存在且非空。
  const e2eFiles = sourceFiles(resolve(import.meta.dir, "../e2e"))
  expect(e2eFiles.length).toBeGreaterThan(0)
  const e2eSource = e2eFiles.map(file => readFileSync(file, "utf8")).join("\n")
  // e2e 通过 UI 交互覆盖返回 TUI（.return-button）与 web-active 状态。
  expect(e2eSource).toContain(".return-button")
  expect(e2eSource).toContain("web-active")
})

test("A-06 dispatch 均返回 Typed IntentOutcome，拒绝不清空草稿", () => {
  const types = readFileSync(resolve(interactiveRoot, "types.ts"), "utf8")
  expect(types).toContain("dispatch(intent: InteractiveIntent): Promise<IntentOutcome>")
  // 拒绝保留草稿的端到端断言由 adapter parity 与 adapter 测试覆盖。
  const parity = readFileSync(resolve(import.meta.dir, "../interactive/adapter-parity.test.ts"), "utf8")
  expect(parity).toContain("保留草稿")
})

test("A-07 interactive 对 ipc/react/opentui/dom/websocket 零依赖", () => {
  const imports = layerImports(interactiveRoot)
  expectNoMatch(imports, /\/ipc(?:\/|")/, "A-07 interactive 不得依赖 ipc")
  expectNoMatch(imports, /@opentui|react|react-dom/, "A-07 interactive 不得依赖 UI 库")
  expectNoMatch(imports, /\.\.\/tui|\.\.\/web/, "A-07 interactive 不得依赖表现层")
  expectNoMatch(readAllSourceFiles(interactiveRoot), /WebSocket|document\.|window\./, "A-07 interactive 不得接触 DOM/WebSocket")
})

test("A-08 Presentation 统一消费 FeatureAvailability，不查协议 Capability", () => {
  const tuiPresentation = readAllSourceFiles(resolve(tuiRoot, "presentation"))
  const webPresentation = readAllSourceFiles(resolve(webRoot, "presentation"))
  expectNoMatch(tuiPresentation + webPresentation, /\bCapability\b/, "A-08 presentation 不得直接判断协议 Capability")
})

test("A-09 Web 高亮用单例 Shiki Worker，未知/失败安全降级", () => {
  const syntaxSource = readAllSourceFiles(resolve(webRoot, "syntax"))
  expect(syntaxSource).toContain("shiki")
  expect(syntaxSource).toContain("Worker")
  // 未知语言/超长代码块降级纯文本的断言由 tests/web/syntax 覆盖。
  const workerTests = readFileSync(resolve(import.meta.dir, "../web/syntax/worker.test.ts"), "utf8")
  expect(workerTests.length).toBeGreaterThan(0)
})

test("A-10 Web 不用 dangerouslySetInnerHTML 渲染 Markdown/代码", () => {
  const webPresentation = readAllSourceFiles(resolve(webRoot, "presentation"))
  expectNoMatch(webPresentation, /dangerouslySetInnerHTML/, "A-10 Web 渲染不得使用 dangerouslySetInnerHTML")
})

test("A-11 TUI Tree-sitter 与 Web Shiki 共享 alias/fallback 不互依平台资源", () => {
  // 语言目录唯一：presentation-shared/language-catalog.ts，TUI/Web 各自平台资源不互引。
  const sharedSource = readAllSourceFiles(presentationSharedRoot)
  expect(sharedSource).toContain("language-catalog")
  const tuiImports = layerImports(resolve(tuiRoot, "platform"))
  const webImports = layerImports(resolve(webRoot, "syntax"))
  expectNoMatch(tuiImports, /\.\.\/\.\.\/web/, "A-11 TUI 平台资源不得引用 Web")
  expectNoMatch(webImports, /\.\.\/\.\.\/tui/, "A-11 Web 高亮不得引用 TUI")
})

test("A-12 未引入 Harness Session 概念", () => {
  const srcSource = readAllSourceFiles(cliSrcRoot)
  expectNoMatch(srcSource, /HarnessSession|harness_session|Harness Session/, "A-12 不得引入 Harness Session 领域对象")
})
