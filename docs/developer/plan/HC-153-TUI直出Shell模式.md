# HC-153 实施计划：TUI 直出 Shell 模式

## 实施步骤

### 步骤 1：表现层输入模式状态机与视觉呈现（InputBar / Theme）
- 在 `tuiTheme` 中引入 `modeShell: "#56B6C2"` 专色；
- 在 `TuiAdapter` 与 `InputBar` 中支持 `inputMode: "chat" | "shell"`；
- 拦截输入框起始 `!` 键入：进入 Shell 模式并展示专色纯文本 `Shell` 标签与占位符；
- 拦截 `Esc` 与空内容 `Backspace`：即时退出 Shell 模式；
- **可演示停点 1**：在输入框空时按 `!` 看到纯文本 `Shell` 专色切换，按 `Esc` 或 `Backspace` 顺畅退出。

### 步骤 2：Interactive Core 与 Python 端直出 Shell 执行接入
- 在 `InteractiveController` 中接入直接 Shell 命令派发；
- 在 Python sidecar（`harness_agent`）中提供非交互式命令执行器（含工作区边界、超时与取消机制）；
- 在时间线上渲染 Shell 终端执行卡片与流式输出；
- **可演示停点 2**：在 Shell 模式下输入 `git status` 或 `ls`，命令直接秒级在时间线输出，不调用 LLM。

### 步骤 3：编写单测与工程一致性检查
- 覆盖输入模式切换、快捷键退出、命令派发与取消的单测；
- 运行 `bun run typecheck`、`bun run test:ts` 与 `pytest`。
