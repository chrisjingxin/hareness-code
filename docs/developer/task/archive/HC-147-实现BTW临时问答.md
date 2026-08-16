---
id: HC-147
title: 实现BTW临时问答
feature_area: 交互与命令系统
parent_task: -
decomposed_by: chrisjingxin
priority: P1
status: 已完成
owner: chrisjingxin
branch: feat/hc-147-btw
reviewed_at: 2026-08-15
review_due: -
scope: 实现 /btw 临时旁路问答（Side Question）完整端到端链路：跨进程 JSON-RPC 协议增加 threads.side_question 方法；Python sidecar 接入只读单轮问答执行器（0 工具、纯文本、不写 Thread Transcript / LangGraph / SQLite / Work Item ledger）；CLI InteractiveController 与 CommandDispatcher 接入 assist.btw 调度；TUI 表现层实现 BtwModal 独立浮层弹窗，支持流式输出、上下滚动、Esc/Enter 快速关闭与复制按钮/快捷键。
acceptance: 1. 在 Build 和 Compose 模式下输入 /btw <question> 能拉起独立浮层弹窗，基于当前 Thread 历史上下文流式得到纯文本回答；2. /btw 问答过程严格禁止调用任何 Tool，回答完全不写入主 Transcript、SQLite 持久化或 Work Item ledger；3. 浮层内按 Esc/Enter 随时关闭并终止流式，按 c 键或点击按钮可将回答内容复制到系统剪贴板；4. 仅输入 /btw 时输出轻量用法通知「用法：/btw <你的问题>」且不调用模型；5. 跨端协议 schema、Python sidecar、TypeScript CLI 与 TUI 单元测试全部通过。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-147-实现BTW临时问答.md、docs/developer/plan/HC-147-实现BTW临时问答.md、docs/developer/todo/HC-147-实现BTW临时问答.md、docs/developer/research/147-BTW临时问答调研.md
test_evidence: bun test (28 passed); pytest test_side_question.py (3 passed); bun run project:check (passed)
references: docs/developer/research/147-BTW临时问答调研.md、docs/developer/task/archive/HC-140-重构组合工作模式.md
completed_at: 2026-08-15
---

# HC-147 实现 BTW 临时问答

## 背景

用户在与 Agent 协作过程中，常需要提问一些概念性、解释性或背景相关的旁路问题（例如：“这个报错是什么意思？”、“解释一下上面提到的概念”）。目前 Harness Code 在 Command Registry 中已预留了 `assist.btw` 定义，但具体分发与跨进程执行逻辑未接入（会提示“/btw 尚未接入当前客户端”）。

通过调研竞品（Oh My Pi、Claude Code、Codex）成熟设计，并结合 Harness Code 现有的 `RunCoordinator`、`SideAnswerPort` 与双端架构，本任务将 `/btw` 完整落地为一个轻量、只读、完全不污染主会话上下文的即焚式问答功能。

需求已使用 `mattpocock:grill-me` 与用户确认，调研结论见 [147-BTW临时问答调研](../../research/147-BTW临时问答调研.md)，规格见 [规格说明](../../spec/HC-147-实现BTW临时问答.md)。

## 用户最终得到什么

1. **随时发起旁路提问**：在终端输入 `/btw <你的问题>` 回车，不论在 Build 模式还是 Compose 模式，均能立即在当前界面弹出半透明/独立浮层弹窗并看到流式回答。
2. **零上下文污染**：问答纯粹基于当前会话的历史背景，回答结束后不会在聊天记录、持久化数据库或 Compose 工作台留痕，不会占用后续长会话上下文空间。
3. **安全即焚**：问答模型无法使用任何修改或探测工具，仅做纯文本回复。
4. **便捷快捷键**：
   - 按 `Esc` 或 `Enter`：立即关闭浮层并销毁临时会话（若仍在生成中则自动中断）；
   - 按 `c` 键或点击「复制」按钮：将生成的答案一键复制到剪贴板，方便粘贴使用；
   - 支持 `Up`/`Down` 翻页或滚动浏览长文本。
5. **参数缺省防护**：只输入 `/btw` 时，终端直接提示 `用法：/btw <你的问题>`，不产生无效网络与模型开销。

## 范围

- **协议层 (`packages/protocol`)**：在 JSON-RPC v3 schema 中增加 `threads.side_question` RPC 请求与响应定义，并重新生成 TS/Python 契约代码。
- **Python 端 (`packages/agent`)**：
  - 在 `AgentHost` 注册 `threads.side_question` 处理函数；
  - 基于当前激活模型与指定 `thread_id` 的已提交上下文，注入 `<btw>` 旁路提问系统提示词（强制 0 工具、单轮问答、不持久化）；
  - 将 `work_item_engine.py` 中的 `SideAnswerPort` 生产实现接入同一底层能力。
- **CLI 交互层 (`packages/cli`)**：
  - `command-dispatcher.ts` 接入 `assist.btw`；
  - `InteractiveController` 支持 `btw.ask` 或执行 side question 请求与取消。
- **TUI 表现层 (`packages/cli/src/tui`)**：
  - 在 `presentation/` 增加 `BtwModal` 浮层组件；
  - `tui/application/adapter.ts` 管理 `/btw` 弹窗状态生命周期与快捷键分发。
- **文档更新**：更新 `docs/user/交互使用.md`。

## 验收项

- [x] `packages/protocol/schema/v3.json` 包含 `threads.side_question` 契约，并通过 `bun run protocol:check`。
- [x] Python 端能够基于 `thread_id` 提取历史消息，调用模型直接流式/单次返回纯文本，不触发任何 Tool Policy、不写入 SQLite / Transcript。
- [x] TypeScript CLI 在接收到 `/btw <question>` 时向 sidecar 发起请求，无参数时给出友好提示。
- [x] TUI 界面在收到 `/btw` 后渲染 `BtwModal` 浮层，支持流式文本更新、滚动浏览、`Esc`/`Enter` 关闭与复制功能。
- [x] Build 模式和 Compose 模式下 `/btw` 命令均正常运作。
- [x] 针对协议、Python 端、CLI 核心与 TUI 弹窗编写完备的自动化测试。

## 评审记录 (agent-skills:code-review-and-quality)

1. **正确性（Correctness）**：
   - 契约 `threads.side_question` 完全符合 v3 协议规范；
   - Python 端单轮问答注入 `<btw>` 上下文隔离，不向 SQLite 或 Transcript 产生任何写入；
   - 空参数友好提示与带参数请求完全符合预期，Build 和 Compose 模式均通过自动化测试。
2. **可读性与简洁性（Readability & Simplicity）**：
   - TUI 表现层结构清晰，`BtwState` 状态流与生命周期明确；
   - 按钮文案简洁（「复制」与「Esc / Enter 关闭」）。
3. **架构合规性（Architecture）**：
   - 遵循分层规范：`application` 模块不直接依赖 `presentation`，`presentation` 不直连 `ipc`，`interactive` 不依赖 React/OpenTUI；
   - 架构测试 `architecture.test.ts` 7 项测试全部通过。
4. **安全性（Security）**：
   - `threads.side_question` 校验 `threads.read` capability，禁止调用任何 Tool，模型输出经脱敏处理。
5. **性能（Performance）**：
   - 旁路异步非阻塞请求，关闭浮层后清理临时状态，无内存泄漏与多余重绘。
