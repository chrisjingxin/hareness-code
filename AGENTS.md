# 仓库协作规范

## 项目结构与模块职责

- `packages/cli/`：`@za38/cli` TypeScript 入口、OpenTUI 表现层与 IPC 客户端测试。
- `packages/protocol/`：跨进程共享的 TypeScript JSON-RPC 方法名和载荷类型。
- `packages/agent/`：`za38-agent` Python 包。`harness_agent/server.py` 负责 stdio JSON-RPC，`agent.py` 构建 deepagents 图，`providers/` 存放模型适配器。
- `packages/*/tests/`：包内测试；Python 测试位于 `packages/agent/tests/`。
- `docs/user/`：最终用户的快速开始、配置、交互使用和故障排查。
- `docs/developer/`：架构、工作流、检查清单、ADR 与任务源；`tasks/` 中一任务一文件，`任务看板.md` 为生成物。
- `docs/compose/`：设计历史；`.agent/` 保存 Agent 实施计划和交接状态，不作为开发文档入口。

表现层逻辑只能放在 `cli`，Agent/业务逻辑只能放在 `agent`，跨进程契约只能放在 `protocol`。

## 构建、测试与开发命令

- `bun run dev`：开发模式运行 CLI 工作区入口。
- `bun run build`：构建全部 Bun 工作区包。
- `bun run typecheck`：检查 OpenTUI/TypeScript 类型。
- `bun run test`：运行工作区测试脚本。
- `cd packages/cli && bun test`：运行 TypeScript IPC/TUI 测试。
- `cd packages/agent && .venv/bin/python -m pytest -q`：使用项目虚拟环境运行 Python 测试。
- `bun run project:check`：同时检查文档链接、任务状态、生成看板和版本/Changelog 一致性。
- `bun run task:claim -- <ID> --owner <名称> --branch <分支>`：认领 `docs/developer/tasks/` 中的任务。
- `bun run task:complete -- <ID> --evidence "<命令与结果>"`：记录证据并完成任务。
- `bun run version:set <SemVer>`：唯一允许修改根 `VERSION`、各包版本与 `CHANGELOG.md` 的入口。

## 代码风格与命名

TypeScript 使用 ESM、2 空格缩进；变量/函数使用 `camelCase`，类/类型使用 `PascalCase`。协议名称必须保持稳定字符串，例如 `stream/text`、`stream/done`。

Python 使用 4 空格缩进；模块/函数使用 `snake_case`，类使用 `PascalCase`，公开 API 必须有类型标注和简洁 docstring。Python 服务端 stdout 只能输出换行分隔的 JSON-RPC；诊断信息写入 stderr 或结构化日志。

维护中的 TS/TSX/Python 生产源码必须具有中文文件说明；类和公开方法/函数必须具有中文 JSDoc 或 docstring。复杂私有函数须在关键决策、状态转换、并发、终端兼容或安全边界处添加中文注释，说明意图而非复述代码。自动生成文件、第三方资源和行为命名测试仅保留来源或用途说明。

当前未配置格式化或 lint 工具。请遵循邻近代码风格，避免无关格式调整。

## 测试规范

Python 测试命名为 `test_<行为>`，Bun 测试命名为 `*.test.ts`。修改 IPC 时必须同时覆盖 Python 端派发/流式行为和 TypeScript 帧处理。优先使用 mock 模型或 mock HTTP 服务，测试中禁止使用真实模型凭据。

修改服务端生命周期时，必须补充取消、中断/恢复、畸形帧和终态错误事件的回归测试。

## 协作与功能完成定义

仓库 Markdown 是任务与文档的唯一事实来源。任务只能编辑 `docs/developer/tasks/<ID>.md`，不得直接编辑生成的 `docs/developer/任务看板.md`；认领、完成后运行 `bun run tasks:sync`。

一个功能只有同时满足下列条件才能标记完成：代码已实现、自动化测试已通过且证据已写入任务；用户可感知变更已更新 `docs/user/`；架构、协议或配置变更已更新 `docs/developer/`；任务状态、关联提交/PR 与版本影响均已记录。无版本变更也必须在任务中说明。

根目录 `VERSION` 是唯一版本来源。禁止手工修改任何分散版本字段或 `CHANGELOG.md` 顶部版本节；必须使用 `bun run version:set <SemVer>`，随后运行 `bun run release:check`。提交前运行 `bun run project:check`、`bun run typecheck` 和 `bun run test`。

## 提交与 Pull Request

沿用现有 Conventional Commit 风格，例如 `feat: Node IPC 客户端，JSON-RPC over stdio`、`fix: handle cancelled agent run`。提交应按包保持聚焦。

PR 必须说明影响层、行为变化、已运行测试，以及配置或协议影响。OpenTUI 变更应附终端截图；新增环境变量或文件系统权限必须明确说明。

## 安全与配置

禁止提交 API Key、网关凭据或其他秘密。模型秘密通过 TOML 配置引用的环境变量提供。工作区路径、MCP 配置、shell 命令和流式工具输出均应视为不可信输入。
