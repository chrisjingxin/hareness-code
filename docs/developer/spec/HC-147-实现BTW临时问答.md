# HC-147：实现 BTW 临时问答规格说明

> 原始需求：[HC-147](../task/archive/HC-147-实现BTW临时问答.md)  
> 竞品调研：[147-BTW临时问答调研](../research/147-BTW临时问答调研.md)

---

## 1. 通俗问题与目标说明

在当前的终端 Coding 过程中，用户常需要就代码错误、概念或背景向模型快速提一个旁路问题。
但用户不希望这个问题变成主会话的一轮正式对话，因为：
- 会污染长会话的历史记录；
- 会增加后续压缩与持久化的 token 负担；
- 会干扰自动化流程或 Compose Work Item 状态。

通过实现 `/btw` 命令，用户可以在任何时刻输入 `/btw <问题>`：
1. 界面弹出一个独立的半透明 / 悬浮浮层（`BtwModal`），显示「正在回答...」并展示流式/生成的文本；
2. 模型基于当前会话的历史快照进行单轮纯文本问答，严格不使用任何工具，问答结果完全不写入数据库或 Transcript；
3. 用户按 `Esc` 或 `Enter` 可以随时关闭浮层（若在生成中则安全中止）；按 `c` 键可一键将回答复制到系统剪贴板；
4. 若用户仅输入 `/btw` 没有带问题，系统仅提示轻量用法通知 `用法：/btw <你的问题>`，不产生无效调用。

---

## 2. 状态机与核心流程

### 2.1 整体流程

```text
输入栏输入 "/btw [question]"
  │
  ├─ 无 question ──→ 输出 Notice("用法：/btw <你的问题>") ──→ 结束
  │
  └─ 有 question ──→ 打开 TUI BtwModal(status: "loading")
                      │
                      ├── 发起 threads.side_question RPC 请求
                      │     │
                      │     └── Python Sidecar:
                      │           ├─ 读取 thread_id 已提交消息历史
                      │           ├─ 组装 <btw> 旁路提示词 (0 工具)
                      │           ├─ 调用当前 ModelProfile
                      │           └─ 返回 reply_text (不落盘、不写持久化)
                      │
                      ├── 接收到响应 ──→ BtwModal(status: "ready", content: reply_text)
                      │     ├─ 按 'c' ──→ 复制到剪贴板并提示通知
                      │     ├─ 按 'Esc' / 'Enter' ──→ 关闭 BtwModal
                      │     └─ 按 'Up' / 'Down' ──→ 滚动内容
                      │
                      └── 发生异常/取消 ──→ BtwModal(status: "error" / 关闭)
```

### 2.2 BtwModal 状态模型

```typescript
export interface BtwState {
  isOpen: boolean
  question: string
  status: "loading" | "ready" | "error"
  content: string
  error?: string
  scrollOffset: number
}
```

---

## 3. 公开接口与协议设计

### 3.1 Protocol Schema (`packages/protocol/schema/v3.json`)

新增 operation `threads.side_question`：
- **Operation Name**: `threads.side_question`
- **Capability**: `threads.read`
- **Params**: `threadsSideQuestionParams`
  ```json
  {
    "thread_id": { "$ref": "#/$defs/id" },
    "question": { "type": "string", "minLength": 1 },
    "model_profile_id": { "$ref": "#/$defs/id" }
  }
  ```
  必填项：`thread_id`、`question`。`model_profile_id` 可选。
- **Result**: `threadsSideQuestionResult`
  ```json
  {
    "reply_text": { "type": "string" },
    "model_profile_id": { "type": "string" }
  }
  ```
  必填项：`reply_text`。

### 3.2 Python Sidecar 接口 (`packages/agent`)

- **Host 路由**：`AgentHost._handle_threads_side_question(params, request_id)`
- **执行内核**：
  1. 通过 `ThreadPersistence` 读取指定 `thread_id` 的已持久化历史消息（若当前 thread 尚无消息则为空列表）；
  2. 剥离半成品与非文本副作用，组装 System Reminder / Prompt：
     ```markdown
     <btw>
     This is an ephemeral side question for the current interactive session.
     Answer briefly and directly using the conversation context already provided.
     NEVER use tools.
     NEVER ask follow-up questions.
     Question:
     {question}
     </btw>
     ```
  3. 获取当前激活或指定的 `ModelProfile` 对应的 LLM 客户端；
  4. 纯文本单轮调用（`tools=[]` 或不绑定任何 tools），获取回复；
  5. 不向 Transcript 追加消息，不写 SQLite checkpoint，不触发 Work Item ledger 变更。
  6. 返回 `{"reply_text": answer, "model_profile_id": model_id}`。

### 3.3 CLI / Interactive Core 接口 (`packages/cli`)

- **Command Dispatcher** (`command-dispatcher.ts`)：
  - `assist.btw` 处理：
    - 参数为空：返回 `{ type: "notice", message: "用法：/btw <你的问题>" }`
    - 参数非空：返回 `{ type: "side-question", question: args, threadId: context.threadId }`
- **TUI Adapter** (`tui/application/adapter.ts`)：
  - 维护 `btwState: BtwState`；
  - 接收到 `side-question` 时：
    - 设置 `btwState = { isOpen: true, question, status: "loading", content: "", scrollOffset: 0 }`；
    - 调用 IPC client `threads.side_question({ thread_id, question })`；
    - 响应后更新 `btwState.status = "ready"` 与 `content`；
    - 键盘分发：当 `btwState.isOpen` 时，优先由 BtwModal 捕获按键（`Esc`/`Enter`/`c`/`Up`/`Down`/`PageUp`/`PageDown`）。

### 3.4 TUI 表现组件 (`packages/cli/src/tui/presentation/btw-modal.tsx`)

- 居中/覆盖式弹窗卡片；
- 标题：`[BTW 临时问答]`；
- 副标题/原问题：展示用户提出的 `question`；
- 内容区：
  - `loading`：显示加载动画与「正在回答...」；
  - `ready`：渲染 Markdown 文本，支持有界滚动；
  - `error`：红字显示错误信息；
- 底部操作栏（Hint）：`Esc/Enter 关闭 · c 复制回答 · ↑/↓ 滚动`。

---

## 4. 关键 Invariants（系统不变量）

1. **零副作用与隔离性（Context Quarantine）**：
   `/btw` 问答严格禁止调用工具（0 tools）、禁止修改文件、禁止发起子进程。问答内容绝对不得进入主会话 Transcript、LangGraph 模型投影、SQLite 持久化或 Compose Work Item。
2. **非阻塞性**：
   `/btw` 问答由独立轻量请求处理，不占用 `RunCoordinator` 的独占写锁（Run lock），不会导致主 Thread 状态被锁死。
3. **安全关闭**：
   用户在任何时刻按 `Esc` 都能立即关闭弹窗；若请求仍在等待，客户端丢弃响应，不产生界面残留。
4. **统一性**：
   Build 模式与 Compose 模式下 `/btw` 行为完全一致，均可用于临时提问。

---

## 5. 错误语义

- **未连接/Thread 不存在**：若 `thread_id` 无效，sidecar 返回空历史并基于纯 Prompt 回答或返回相应 RPC 错误。
- **模型未配置/调用失败**：若 API 密钥失效或网络中断，捕获异常并在浮层中友好显示错误原因，支持 `Esc` 退出。
- **无问题输入**：直接拦截在客户端，显示通知 `用法：/btw <你的问题>`，不发送 RPC。

---

## 6. 测试与可观察验证

1. **协议层测试**：
   - 验证 `v3.json` 包含 `threads.side_question`，运行 `bun run protocol:check` 验证生成与校验。
2. **Python 端单元测试**：
   - `test_side_question`：验证 `threads.side_question` 能正确返回纯文本回答，且不向 SQLite/Transcript 写入任何记录，不调用工具。
3. **CLI / Command Dispatcher 测试**：
   - `assist.btw` 无参数返回 notice；
   - `assist.btw` 有参数返回 `side-question` 命令结果。
4. **TUI 弹窗与按键测试**：
   - 弹窗展示、loading 状态、ready 状态渲染；
   - `Esc`/`Enter` 关闭；
   - `c` 键复制调用 clipboard；
   - `Up`/`Down` 滚动。
