---
id: HC-108-legacy
title: Web 代码高亮切换 Shiki 并建立共享语言目录
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-108-shiki
scope: 将 Web fenced code 高亮从自研 web-tree-sitter 链路替换为单例 Shiki Worker（shiki/core + JavaScript regex engine + fine-grained imports），新增 presentation-shared/language-catalog.ts 共享语言目录，删除 web-tree-sitter 的 client/protocol/capture/span 实现、WASM 路由与相关资产生成，并保持安全降级与无网络依赖。
acceptance: Web 代码块由唯一 Shiki Worker 高亮且运行期零网络请求；未知语言、Worker 失败或资源加载失败安全降级为纯文本且复制始终可用；web-tree-sitter 依赖、`/web/syntax/tree-sitter.wasm` 与 `/web/syntax/lang/*.wasm` 路由、SyntaxClient 旧实现全部删除；TUI 的 OpenTUI Tree-sitter 高亮与测试零变化；`bun run typecheck`、`bun test --isolate tests/web`、`bun run build`、`bun run project:check` 全绿；架构测试断言 web/syntax 不再引用 tui/platform/assets。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/adr/0003-single-interactive-core-dual-renderer.md
test_evidence: bun run typecheck (pass), bun test --isolate (332 pass, 0 fail), bun run build (pass), bun run project:check (pass)
references: docs/developer/task/HC-107-修复WebComposer、补.md
completed_at: 2026-08-05
---

## 背景

最终架构方案（单一 Interactive Core · 双原生 Renderer，2026-08-04 定稿）决策 D-08/D-09：**Web 高亮使用 Shiki Worker 单例，不使用 Web Tree-sitter 捕获链；TUI 保留 OpenTUI Tree-sitter 高亮，不尝试复用 Web 高亮引擎**。两端只共享语言规范与代码块展示策略（A-09、A-11）。

当前 Web 高亮链路（`packages/cli/src/web/syntax/`）是基于 `web-tree-sitter@0.25.10` 的自研实现：

```text
Markdown parser → CodeBlock → SyntaxClient（主线程）→ Worker（web-tree-sitter Parser + .scm query）
  → offset/scope span → React <span> 渲染
```

- `client.ts`：SyntaxClient，管理 Worker 实例、128 条/4 MiB LRU、1 500 ms 超时、64 KiB/2 000 行限额与熔断。
- `worker.ts`：web-tree-sitter 离线解析，`initParser()` 通过 `/web/syntax/tree-sitter.wasm` 定位 core，语言 WASM 走 `/web/syntax/lang/<asset-id>.wasm`。
- `protocol.ts`：SyntaxWorkerRequest/SyntaxWorkerResponse。
- `catalog.generated.ts`：由 `packages/cli/scripts/vendor-syntax-assets.ts` 生成（从 `syntax-parsers.config.json` 拉取 14 个语言的 WASM/query）。
- `packages/cli/src/web/server.ts`：静态路由 `/web/syntax-worker.js`、`/web/syntax/tree-sitter.wasm`、`/web/syntax/lang/{id}.wasm`；CSP 含 `'wasm-unsafe-eval'`。
- `packages/cli/src/web/bundle.ts`：`WebAssets` 携带 syntaxWorkerScript/treeSitterWasm/languageWasms。
- `packages/cli/src/web/presentation/code-block.tsx`：安全 span 渲染（无 `dangerouslySetInnerHTML`，A-10 已满足），数据来源是上述 SyntaxClient。

HC-107（进行中）的设计文档曾明确"仓库已锁定 `web-tree-sitter@0.25.10`，不需要引入 Shiki"；最终架构方案（2026-08-04）推翻了该决策。本任务先落地 Shiki 替换，HC-107 后续只保留 Composer 修复与设计精修部分，其代码高亮范围以本任务结果为准。

## 当前存在的问题

1. Web 高亮与 TUI 高亮共用 Tree-sitter 资产（worker 直接 import `tui/platform/assets/syntax/*.scm`），违反方案"两端不共享 Grammar/Token/Renderer"的边界（A-11 的"不互相依赖平台资源"）。
2. 需要为 14 种语言持续维护 WASM + query 资产下载/校验/生成管线（vendor-syntax-assets.ts），供应链与构建复杂度高。
3. CSP 必须开放 `'wasm-unsafe-eval'`，缩小了浏览器安全面。
4. `web-tree-sitter` 语言覆盖与维护依赖上游 grammar 发布节奏，且与 TUI 的 parser 版本耦合。

## 为什么现在要修改

- 最终架构方案将 Shiki 定为 Web 侧唯一高亮引擎（D-08），本任务是 7 个迁移阶段中的阶段 1，独立可验证。
- HC-107 正在推进 tree-sitter 高亮，若不在其完成前切换，会产生返工；本任务应先于 HC-107 高亮验收落地。
- Shiki（`shiki/core` + JavaScript regex engine）无需 Oniguruma WASM，可用 fine-grained imports 按需加载语言与主题，天然满足 CSP 收紧与单 Worker 单例要求。

## 目标设计

```text
Markdown parser → CodeBlock → ShikiHighlightService（主线程，LRU/限额/熔断沿用现有阈值）
  → page-level Worker → 单例 Shiki highlighter（shiki/core + JS regex）
  → 紧凑 token line 数据 → React <span> 安全渲染（不重建 DOM 字符串）
```

### 新增/修改模块

- `packages/cli/src/presentation-shared/language-catalog.ts`（新建，本任务首个共享模块，方案 §9.4）：`canonical`、`aliases`、`tuiParser`、`webLanguage`、`fallback`。首批必须覆盖：JavaScript、TypeScript、JSX、TSX、JSON、Python、Bash/Shell、Go、Java、C、C++、HTML、CSS、YAML、Markdown、纯文本。TUI 的 `tui/platform/syntax-parsers.ts` 与 Web 的 Shiki loader 都消费此目录，但不互相引用平台资源。
- `packages/cli/src/web/syntax/highlight-service.ts`（替换 `client.ts`）：沿用现有限额策略（128 条/4 MiB LRU、64 KiB/2 000 行、1 500 ms 超时、熔断），缓存 key = code hash + canonical language + theme；Worker 失败/未知语言/资源失败 → 纯文本降级。
- `packages/cli/src/web/syntax/worker.ts`（重写）：shiki/core + JavaScript regex engine；单例 highlighter；fine-grained imports（语言/主题按需）；返回紧凑 token line 数据（无 HTML 字符串）。
- `packages/cli/src/web/syntax/language-loader.ts`（新建，替换 catalog.generated.ts 的 Web 部分）：Shiki 语言注册表，来自 language-catalog.ts 的 canonical/webLanguage 映射。
- `packages/cli/src/web/syntax/protocol.ts`：保留请求/响应契约形状（offset/scope 语义可复用），worker 载荷改为紧凑 token line。
- `packages/cli/src/web/server.ts`：删除 `/web/syntax/tree-sitter.wasm` 与 `/web/syntax/lang/{id}.wasm` 路由；保留单个 Worker 文件路由（文件名可沿用 `/web/syntax-worker.js`）；CSP 移除 `'wasm-unsafe-eval'`。
- `packages/cli/src/web/bundle.ts`：`WebAssets` 删除 treeSitterWasm/languageWasms，保留 syntaxWorkerScript。
- `packages/cli/scripts/vendor-syntax-assets.ts` + `syntax-parsers.config.json`：收缩为仅服务 TUI（`tui/platform/assets/syntax/`、`generated-syntax-parsers.ts`），删除 Web catalog 生成。
- `packages/cli/package.json`：新增 `shiki` 依赖（企业镜像确认可用后固定版本）；删除 `web-tree-sitter`。

### 流式输出策略（方案 §9.3）

| 状态 | 行为 |
| --- | --- |
| 代码围栏未闭合 | 保持纯文本，不触发高亮 |
| 围栏已闭合但仍在流式变化 | 80–150 ms debounce 后请求高亮 |
| 代码块稳定 | 使用缓存结果；仅 code/lang/theme 变化时重算 |
| 超大代码块 | 延迟高亮或纯文本降级，复制功能始终可用 |
| 主题切换 | 从缓存重读（key 含 theme），不重建 Worker |

### 删除清单

- `web/syntax/client.ts`、`web/syntax/catalog.generated.ts`（Web 部分）、worker 内 web-tree-sitter Parser/Query/`initParser` 逻辑、`.scm` 导入。
- 依赖 `web-tree-sitter`；server 的 wasm 路由；bundle 的 wasm 资产；vendor 脚本的 Web 产物。

## 实施步骤

1. 在企业镜像确认 `shiki` 可用后 `bun add shiki`（packages/cli），固定精确版本。
2. 新建 `presentation-shared/language-catalog.ts`（16 语言规范 + alias/fallback），写单元测试：alias 解析、未知语言 fallback、canonical 唯一性。
3. 重写 `web/syntax/worker.ts`（Shiki 单例 + fine-grained imports + 紧凑 token line），新增 `language-loader.ts`；更新 `protocol.ts` 载荷形状。
4. 将 `client.ts` 改为 `highlight-service.ts`：复用限额/LRU/熔断，交换 worker 引擎，缓存 key 增加 theme。
5. 改 `code-block.tsx` 消费新 service 输出（span 渲染接口不变），验证流式 debounce 与降级分支。
6. 删除 wasm 路由与 CSP `'wasm-unsafe-eval'`，更新 bundle.ts 资产清单；收缩 vendor-syntax-assets.ts 为 TUI 专用并重新生成 TUI 资产。
7. 删除 `web-tree-sitter` 依赖与旧 client/protocol/capture/span 代码；新建 `docs/developer/architecture/adr/0003-single-interactive-core-dual-renderer.md` 记录最终架构决策（决策表 D-01~D-10 摘要、术语、拓扑、目录结构、护栏），并在 `架构总览.md` 补"Web 语法高亮"章节。
8. 更新 `docs/user/交互使用.md`（Web 代码高亮能力与降级行为）；更新 `tests/web/architecture.test.ts` 断言 web/syntax 不再 import `tui/platform`；按仓库规范更新 HC-107 的 scope 说明（高亮部分由本任务接管）。
9. 验证：`bun run typecheck`、`bun test --isolate tests/web`、`bun run build`、`bun run project:check`；提交证据写入本任务。

## 范围

- Web fenced code 高亮引擎替换为 Shiki（单 Worker 单例、JS regex、fine-grained imports、LRU、流式 debounce、降级）。
- presentation-shared/language-catalog.ts 首建（16 语言）。
- 删除 web-tree-sitter 全链路与 wasm 路由/CSP 收紧。
- ADR 0003 与架构总览"Web 语法高亮"章节、docs/user 更新。

## 非范围

- TUI 的 OpenTUI Tree-sitter 高亮与 `tui/platform/syntax-*` 不动（D-09）。
- 不引入双主题 token 机制（主题切换走缓存重读）；不做多 Worker/多 highlighter。
- 不重构 Markdown 解析、Timeline、Composer 与 HC-107 其余范围。
- 不修改 packages/protocol；无版本变更（`bun run version:set` 不触发）。
- 不动 Interactive Core、PresentationCoordinator（后续任务）。

## 验收清单

- [ ] `grep -r "web-tree-sitter" packages/cli` 无生产引用；`package.json` 移除该依赖。
- [ ] `/web/syntax/tree-sitter.wasm` 与 `/web/syntax/lang/*` 路由删除后返回 404；CSP 无 `'wasm-unsafe-eval'`。
- [ ] 支持语言（TS/TSX/JS/Python/Go/Bash/JSON/YAML/HTML/CSS/Markdown 等）fenced code 高亮正确；未知语言与 Worker 失败显示纯文本。
- [ ] 流式：围栏未闭合不触发高亮；闭合后 debounce 生效；稳定块命中缓存（devtools 断言无重复请求）。
- [ ] Web 页面运行期 Network 面板无外部语言/主题请求（零网络依赖）。
- [ ] `code-block.tsx` 仍无 `dangerouslySetInnerHTML`（架构测试覆盖）。
- [ ] TUI 测试全绿（`bun test --isolate tests/tui`），TUI 无 diff。
- [ ] `tests/web/architecture.test.ts` 新增断言：web/syntax 不 import `../tui/`、不 import `tui/platform`。
- [ ] 证据写入本任务：typecheck/test/build/project:check 输出与关键 diff 摘要；按仓库规范完成 open-code-review 检视。
