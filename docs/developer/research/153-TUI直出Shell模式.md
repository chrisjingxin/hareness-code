# 153 竞品调研：TUI 直出 Shell 模式与状态指示

## 1. 调研对象

- **MiMo Code**：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx`
- **Claude Code**：`!<command>` Shell 交互模式

---

## 2. 交互机制对比分析

### 2.1 触发与模式切换（Mode Entry）
- **MiMo Code**：
  - 当光标位于输入框起始位置（`offset === 0`）且键入 `!` 时，**拦截 `!` 字符的输入**，并将输入框状态机切换为 `mode = "shell"`；
  - 提示符前缀 Badge 从模型名称切换为醒目的 **`💻 Shell`**；
  - 输入框占位符（Placeholder）动态切换为 Shell 命令示例（例如 `输入 Shell 命令，如 git status, ls -la...`）；
  - 输入框整体边框切换为专用的终端青绿高亮。

### 2.2 退出机制（Mode Exit）
- **MiMo Code**：
  - 在 `mode === "shell"` 且输入框文本为空时，按 **`Backspace`** 键即可秒级退回常规对话模式；
  - 在任何时候按 **`Esc`** 键，立即退出 Shell 模式并清空输入；
  - 底部操作栏（FooterRail）实时显示快捷键提示：`Esc / Backspace 退出 Shell 模式`。

### 2.3 执行与持久化（Execution & Timeline）
- 按 **`Enter`** 提交命令时，不触发 LLM 推理与计费；
- 由 Python sidecar 在本地工作区边界内执行非交互式 Shell，支持：
  - 实时 stdout/stderr 增量输出流；
  - 超时保护（默认 120s）；
  - `Esc` 中断/取消运行；
- 命令与输出结果以专门的终端执行卡片（Tool/Shell Card）记录在 Thread 时间线中，后续的 Agent 提问可以直接读取该执行结果上下文。

---

## 3. 本项目落地设计建议

在 Harness Code 的当前架构中：
1. **`InputBar`（表现层）**：
   - 支持 `inputMode: "chat" | "shell"`；
   - 空输入按 `!` 切换为 `shell` 模式，前缀展示 `💻 Shell (bash)` 绿色胶囊徽标；
   - 在 Shell 模式下空文本按 `Backspace` 或 `Esc` 退回 `chat`；
2. **`InteractiveController`（应用层）**：
   - 增加 `shell.execute` intent（或由 `input.submit` 路由至 direct shell 执行）；
   - 不调用模型，派发非交互式 Shell 执行；
3. **跨进程协议与 Python 侧**：
   - 协议在 `v3.json` 中提供直接 Shell 执行能力；
   - Python 端提供超时、取消与流式 stdout 回传。
