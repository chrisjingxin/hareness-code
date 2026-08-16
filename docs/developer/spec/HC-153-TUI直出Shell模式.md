# HC-153 规格说明：TUI 直出 Shell 模式

## 1. 领域模型与接口契约

### 1.1 输入模式状态（`InputMode`）

在 `packages/cli/src/tui/application/adapter.ts` 与 `input-bar.tsx` 中：

```ts
export type InputMode = "chat" | "shell"
```

- 默认状态：`"chat"`；
- 当 `draft.length === 0` 且用户键入 `!` 时，触发 `input-mode-change` -> `"shell"`，阻止 `!` 写入 draft；
- 当处于 `"shell"` 模式时：
  - 用户按下 `Escape` -> 退出到 `"chat"` 并清空 draft；
  - 当 `draft.length === 0` 用户按下 `Backspace` -> 退出到 `"chat"`；
- 当用户按 `Enter` 提交时：
  - 若 `inputMode === "shell"`，派发带有 shell 语义的提交（例如 `command = draft` 直出执行）。

### 1.2 表现层视觉规范（Theme & Presentation）

在 `packages/cli/src/tui/presentation/theme.ts` 中：

```ts
export const tuiTheme = {
  modeBuild: "#EAB308",    // 暖金黄
  modeCompose: "#A9A5D4",  // 柔和紫
  modeShell: "#56B6C2",    // 绿松石青（专属色彩，与 Build/Compose 明确区分）
  // ...
}
```

- **ThreadRuntimeLine / InputBar 头部标签**：
  - 模式文本：纯文本 **`Shell`**（不带图标，不带括号）；
  - 文字前景色：`tuiTheme.modeShell`；
  - 占位符文案：`输入 Shell 指令（如 git status, ls -la）...`；
  - 边框：保持统一的 `tuiTheme.border` / `tuiTheme.borderActive`，不变绿。

### 1.3 直出执行生命周期

```text
输入 Shell 命令 → Enter 提交 → InteractiveController 派发 direct_shell
                → Python sidecar 启动子进程 (stdio 管道)
                → 实时 stream 增量回传输出并在时间线上创建 Shell 卡片
                → 进程结束写入 Thread 持久化记录
```

- 支持 `Esc` 中断当前 Shell 执行；
- 超时保护：120 秒；
- 目录约束：当前工作区目录。
