# HC-147：实现 BTW 临时问答实施计划

> 原始需求：[HC-147](../task/archive/HC-147-实现BTW临时问答.md)  
> 规格说明：[HC-147 规格](../spec/HC-147-实现BTW临时问答.md)

---

## 1. 计划概述

本计划把 `/btw` 临时旁路问答按真实依赖拆解为 4 个工作包（Work Packages）。每一步均以端到端或垂直切片为导向，并在前端浮层打通后设置**可演示停点**，供用户在终端直观体验。

---

## 2. 工作包划分与依赖顺序

```text
WP1: 协议与 Python 端只读问答服务 (Protocol & Sidecar Backend)
  │
  ▼
WP2: CLI 调度层与命令分发 (CLI Command Dispatcher & IPC)
  │
  ▼
WP3: TUI BtwModal 浮层与快捷键闭环 (TUI Presentation & Keybindings)  ──→ 【可演示停点 1】
  │
  ▼
WP4: 双模式回归、文档与项目一致性检查 (E2E & Docs)
```

---

## 3. 详细步骤

### WP1: 协议契约与 Python 端只读问答服务 (Protocol & Backend)

**目标**：完成 `threads.side_question` JSON-RPC 契约与 Python `AgentHost` 端的无副作用执行逻辑。

1. **修改 `packages/protocol/schema/v3.json`**：
   - 增加 `threads.side_question` 操作契约（params: `threadsSideQuestionParams`, result: `threadsSideQuestionResult`，capability: `threads.read`）。
2. **运行契约代码生成**：
   - 执行 `bun run protocol:generate`，生成 TS 和 Python 契约。
   - 执行 `bun run protocol:check` 确保通过。
3. **在 Python 端实现 `AgentHost._handle_threads_side_question`**：
   - 提取指定 `thread_id` 的历史消息列表；
   - 组装包含 `<btw>` 约束的单轮纯文本 prompt；
   - 调用当前绑定的 LLM 模型（0 tools），获取并返回纯文本；
   - 确保完全不向 SQLite、Transcript 或 Work Item 追加记录。
4. **编写并运行 Python 测试**：
   - 编写 `test_side_question` 覆盖有效问答、空历史问答、无副作用断言（0 tool calls, transcript unchanged）。
   - 运行 `pytest packages/agent/tests/host/`。

### WP2: CLI 调度层与命令分发 (CLI Command Dispatcher & IPC)

**目标**：在 TypeScript CLI 中接入 `assist.btw` 命令分发，提供用法校验与 IPC 桥接。

1. **更新 `command-dispatcher.ts`**：
   - 为 `assist.btw` 接入 handler：
     - 若参数为空，返回 `notice("用法：/btw <你的问题>")`；
     - 若参数非空，返回 `{ type: "side-question", question: args, threadId: context.threadId }`。
2. **更新 `InteractiveController` / `adapter.ts`**：
   - 处理 `side-question` 命令结果：通过 `ClientTransport` 发送 `threads.side_question` 请求并管理状态。
3. **编写单元测试**：
   - 编写 `packages/cli/tests/interactive/commands.test.ts` 验证 `/btw` 无参提示与有参分发。

### WP3: TUI BtwModal 浮层与快捷键闭环 (TUI Presentation)

**目标**：在终端呈现独立的 `BtwModal` 浮层组件，支持 Markdown 渲染、流式更新、快捷关闭与一键复制。

1. **新建 `packages/cli/src/tui/presentation/btw-modal.tsx`**：
   - 实现悬浮卡片样式（边框、标题 `[BTW 临时问答]`、原问题展示、状态指示、Markdown 内容）；
   - 支持上下滚动浏览长文本。
2. **在 `tui/application/adapter.ts` 接入 BtwState**：
   - 维护 `btwState`（`isOpen`、`status`、`question`、`content`、`error`、`scrollOffset`）；
   - 拦截键盘事件：
     - `Esc` / `Enter`：关闭浮层；
     - `c`：复制回答文本到系统剪贴板（调用 `copyToClipboard`）并提示轻量通知；
     - `Up` / `Down`：滚动内容。
3. **在 `tui/presentation/thread-view.tsx` 渲染 `BtwModal`**：
   - 当 `btwState.isOpen` 时，在视图上层渲染 `BtwModal`。
4. **验证与停点**：
   - 编写 TUI focused 测试；
   - **可演示停点 1**：在终端运行 `bun run dev`，输入 `/btw 什么是Python`，观察居中浮层弹出并展示回答，按 `c` 复制，按 `Esc` 关闭。

### WP4: 双模式回归、用户文档与项目级检查 (Final Polish)

**目标**：确保 Build 模式与 Compose 模式下一致性，完善用户文档，完成项目质量门禁。

1. **回归验证**：
   - 验证 Build 模式和 Compose 模式下 `/btw` 均正常唤起且不干扰主流程；
   - 验证 Compose 模式下未完成的 Work Item 不受 `/btw` 影响。
2. **更新用户文档**：
   - 更新 `docs/user/交互使用.md` 中的 `/btw` 命令说明。
3. **运行完整质量检查**：
   - `bun run typecheck`
   - `bun run test`
   - `bun run project:check`
