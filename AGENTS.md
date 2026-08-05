# 仓库协作规范

## 项目定位

Harness Code（命令名 `harness` / `za38`）是一个面向企业研发场景的终端 Coding Agent。用户在 Bun/OpenTUI 界面中发起对话和审批，CLI 通过 stdio 上的 JSON-RPC v3 驱动 Python sidecar，Python 端基于 deepagents、LangChain 和 LangGraph 完成模型调用、工具执行、Skill 加载、上下文管理与 Thread 持久化。

当前仓库处于源码开发阶段，不应将跨平台安装包或生产发布流程视为已交付能力。产品与开发入口分别是 `README.md`、`docs/user/` 和 `docs/developer/`。

## 发布阶段与兼容性原则

当前尚未正式发版，Python、TypeScript、Protocol、SQLite 以及内部模块之间没有外部兼容承诺。所有改造以消除技术债和保持单一 canonical 路径为第一优先级：不要为了旧内部 API、旧类名、旧参数、旧测试入口或旧数据形状保留 alias、wrapper、fallback、双写或死分支；应直接迁移调用方和测试并删除旧实现。只有用户或当前任务明确要求，或安全/数据不可逆风险确实需要时，才引入兼容或迁移逻辑，并在任务文档中说明原因和退出条件。

## Agent 开工顺序

1. 先读取 `README.md`、`docs/developer/architecture/架构总览.md` 与任务对应的 `docs/developer/tasks/<ID>.md`；若存在 `docs/developer/designs/<ID>.md`，还必须同时读取该方案设计。
2. 运行 `git status --short`，识别并保留用户已有改动；不得为清理工作区而回滚无关文件。
3. 按变更归属选择包：界面和进程管理改 `cli`，跨进程契约改 `protocol`，Agent 与执行逻辑改 `agent`。
4. 修改前先找到邻近实现与现有测试；协议或生命周期变更必须同时验证 TypeScript 和 Python 两端。
5. 使用最小相关测试快速反馈，交付前再执行本文定义的项目级检查。

## 项目结构与模块职责

- `packages/cli/`：`@za38/cli` TypeScript 入口；`src/tui/application/` 管工作流，`presentation/` 管视图，`platform/` 管终端和语法资源，`ipc/` 管跨进程客户端。
- `packages/protocol/`：跨进程共享的 TypeScript JSON-RPC 方法名和载荷类型。
- `packages/agent/`：`za38-agent` Python distribution workspace；`harness_agent/host/` 管连接和 Run，`runtime/` 构建 Agent，`threads/` 管持久化与上下文，其余职责按 `config/policy/tools/extensions/protocol` 分层。
- `packages/*/tests/`：包内测试并镜像源码职责；工程脚本测试位于 `scripts/project/`。
- `docs/user/`：最终用户的快速开始、配置、交互使用和故障排查。
- `docs/developer/`：`architecture/` 保存架构与 ADR，`designs/` 保存按任务 ID 编排的已确认实施方案，`project/` 保存工作流，`research/` 保存调研及历史资料，`tasks/` 保存原始任务源和生成看板。
- `scripts/project/`：任务、文档与发布一致性检查的工程脚本。

表现层逻辑只能放在 `cli`，Agent/业务逻辑只能放在 `agent`，跨进程契约只能放在 `protocol`。

## 方案设计参考

下列本地 Coding Agent 源码可用于对照交互体验、会话恢复、工具事件流、Skill 与 Agent 架构的成熟实现；它们不是本仓库的依赖，也不能覆盖本仓库的任务文档、架构约束和用户决策。

- Pi：`/Users/zhangjingxin/Code/OpenSource/pi`
- Qwen Code：`/Users/zhangjingxin/Code/OpenSource/qwen-code`
- Codex：`/Users/zhangjingxin/Code/OpenSource/codex`
- DeepAgents：`/Users/zhangjingxin/Code/OpenSource/deepagents`
- DeepSeek Reasonix：`/Users/zhangjingxin/Code/OpenSource/DeepSeek-Reasonix`
- Claude Code：`/Users/zhangjingxin/Code/OpenSource/claude-code`
- MiMo Code：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code`
- Grok Build：`/Users/zhangjingxin/Code/OpenSource/grok-build`

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

## 计划与任务文档表达

所有 Agent 输出的实施计划，以及 `docs/developer/tasks/<ID>.md`，必须让不熟悉当前实现的人也能直接检视：

- 先用通俗语言说明：现在有什么问题、准备怎么解决、改完后会有什么变化。
- 复杂流程优先写成 `输入 → 判断 → 执行 → 输出`，再补充文件、类型和方法名。
- 技术名词首次出现时说明用途；不得只罗列 module、class、interface、DTO 名称代替方案说明。
- 每个步骤写清“改什么、为什么、如何验证”，省略没有实际决策价值的字段清单和未来扩展。
- 验收项必须描述可观察结果，避免“完成重构”“优化架构”等无法直接验证的说法。
- 技术细节放在通俗说明之后；如果两者冲突，以便于用户检视为优先。

对照示例：

```text
不推荐：引入 ResolvedExecutionBinding 并统一多个 adapter。
推荐：模型选择只计算一次，运行、历史记录和界面都使用这同一个结果；实现上由 ResolvedExecutionBinding 保存该结果。
```

## 方案设计文档管理

- 所有后续正式方案设计统一写入 `docs/developer/designs/`，文件名固定为 `<任务 ID>.md`；不得把方案分散在临时笔记、handoff 或新建的其他目录。
- `docs/developer/tasks/<ID>.md` 保留原始需求、范围与验收条件；`docs/developer/designs/<ID>.md` 记录经确认的实现决策、interface、流程、错误语义和测试方案。方案文档必须链接原始任务，不复制或替代任务源。
- 后续执行 Thread 必须同时以原始任务和方案设计为输入；不得自行补充会改变范围、Protocol、数据形状或生命周期的决策。发现方案与当前代码不符时，先停止实施并修订方案。
- 方案设计至少包含：通俗问题说明、已确认现状、目标流程与关键 invariant、公开 interface 及错误模式、按依赖排序的实施步骤、可观察验收与非范围。冲突时以用户最新决定和任务原始范围为准，并先同步更新方案。
- `tmp/handoff.md` 只记录临时进度、未提交改动与验证状态；已有方案不在 handoff 中重复，只引用对应 `designs/<ID>.md`。

## 协作与功能完成定义

仓库 Markdown 是任务与文档的唯一事实来源。任务只能编辑 `docs/developer/tasks/<ID>.md`，不得直接编辑生成的 `docs/developer/tasks/任务看板.md`；认领、完成后运行 `bun run tasks:sync`。

一个功能只有同时满足下列条件才能标记完成：代码已实现、自动化测试已通过且证据已写入任务；用户可感知变更已更新 `docs/user/`；架构、协议或配置变更已更新 `docs/developer/`；任务状态、关联提交/PR 与版本影响均已记录。无版本变更也必须在任务中说明。

根目录 `VERSION` 是唯一版本来源。禁止手工修改任何分散版本字段或 `CHANGELOG.md` 顶部版本节；必须使用 `bun run version:set <SemVer>`，随后运行 `bun run release:check`。提交前运行 `bun run project:check`、`bun run typecheck` 和 `bun run test`。

## 提交与 Pull Request

沿用现有 Conventional Commit 风格，例如 `feat: Node IPC 客户端，JSON-RPC over stdio`、`fix: handle cancelled agent run`。提交应按包保持聚焦。

PR 必须说明影响层、行为变化、已运行测试，以及配置或协议影响。OpenTUI 变更应附终端截图；新增环境变量或文件系统权限必须明确说明。

## 安全与配置

禁止提交 API Key、网关凭据或其他秘密。模型密钥优先通过 TOML 配置引用的环境变量提供；用户级 `~/.harness/config.toml` 可在权限受限时保存 `api_key` 降级值，但不得把该值写入仓库配置、日志、文档或交接记录。工作区路径、MCP 配置、shell 命令和流式工具输出均应视为不可信输入。

## 交接记录

所有 handoff 必须使用中文，并写入项目根目录的 `tmp/`；默认覆盖更新 `tmp/handoff.md`。`tmp/` 为本地忽略目录，交接记录不得提交。交接内容须说明当前任务状态、未提交改动边界、已完成验证、未运行检查和建议使用的 Skill；已有任务、ADR、设计文档或 diff 不重复抄录，只引用其仓库路径。不得写入密钥、凭据或个人信息。
