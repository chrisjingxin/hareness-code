# HC-147：实现 BTW 临时问答执行清单

> 原始需求：[HC-147](../task/archive/HC-147-实现BTW临时问答.md)  
> 实施计划：[HC-147 计划](../plan/HC-147-实现BTW临时问答.md)

---

## WP1: 协议契约与 Python 端只读问答服务

- [x] **Task 1.1**：在 `packages/protocol/schema/v3.json` 增加 `threads.side_question` 操作与数据类型定义。
  - 完成信号：运行 `bun run protocol:generate && bun run protocol:check` 退出码为 0，生成 TS 和 Python 契约。
- [x] **Task 1.2**：编写 Python 端失败测试 `packages/agent/tests/host/test_side_question.py`。
  - 覆盖正常问答、无历史问答、不向 Transcript/SQLite/Work Item 写入数据、0 工具调用的断言。
  - 完成信号：`cd packages/agent && .venv/bin/python -m pytest packages/agent/tests/host/test_side_question.py` 失败（RED）。
- [x] **Task 1.3**：在 `packages/agent/harness_agent/host/agent_host.py` 实现 `_handle_threads_side_question`。
  - 接入历史消息读取、`<btw>` prompt 包装与无工具模型调用。
  - 完成信号：`cd packages/agent && .venv/bin/python -m pytest packages/agent/tests/host/test_side_question.py` 全部通过（GREEN）。

---

## WP2: CLI 调度层与命令分发

- [x] **Task 2.1**：编写 CLI 命令测试 `packages/cli/tests/interactive/commands.test.ts` 针对 `/btw`。
  - 覆盖：无参数时返回 `用法：/btw <你的问题>`，有参数时返回 `side-question` 结果。
  - 完成信号：测试由于未实现对应 handler 而失败（RED）。
- [x] **Task 2.2**：在 `packages/cli/src/interactive/command-dispatcher.ts` 注册 `assist.btw` handler。
  - 完成信号：`cd packages/cli && bun test tests/interactive/commands.test.ts` 通过（GREEN）。

---

## WP3: TUI BtwModal 浮层与快捷键闭环 【可演示停点 1】

> **怎么看阶段性效果**：
> 1. 运行 `bun run dev`；
> 2. 在对话界面中输入 `/btw 什么是Python` 并回车；
> 3. 观察终端中央弹出 `[BTW 临时问答]` 浮层并展示回答；
> 4. 按 `c` 键，界面提示「已复制回答到剪贴板」；
> 5. 按 `Esc` 或 `Enter` 键，浮层顺利关闭，主界面没有任何多余消息。

- [x] **Task 3.1**：创建 `packages/cli/src/tui/presentation/btw-modal.tsx` 浮层组件。
  - 支持 loading 态、Markdown 正文渲染、error 态及底部快捷键指引。
- [x] **Task 3.2**：在 `packages/cli/src/tui/application/adapter.ts` 接入 `BtwState` 管理与 IPC 发起。
  - 接入 `threads.side_question` 调用、结果更新及 `Esc`/`Enter`/`c`/`Up`/`Down` 快捷键分发。
- [x] **Task 3.3**：在 `packages/cli/src/tui/presentation/thread-view.tsx` / `app.tsx` 挂载 `BtwModal`。
- [x] **Task 3.4**：编写 TUI 浮层与交互测试，验证浮层弹出、键盘分发与复制逻辑。
  - 完成信号：`cd packages/cli && bun test tests/tui/btw-modal.test.ts` 全部通过。

---

## WP4: 双模式回归、用户文档与项目级检查

- [x] **Task 4.1**：验证 Build 模式与 Compose 模式下 `/btw` 行为一致且均不破坏各自生命周期。
- [x] **Task 4.2**：更新用户文档 `docs/user/交互使用.md` 中的 `/btw` 命令说明。
- [x] **Task 4.3**：运行项目全套质量检查：
  - `bun run typecheck`
  - `bun run test`
  - `bun run project:check`
