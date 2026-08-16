# HC-153 执行清单：TUI 直出 Shell 模式

## 阶段一：TUI 输入状态机与视觉指示（停点 1）

- [x] 在 `packages/cli/src/tui/presentation/theme.ts` 中新增 `modeShell: "#56B6C2"` 独立专色
- [x] 在 `packages/cli/src/tui/application/adapter.ts` 中管理 `inputMode` 状态与 `input-mode-change` Intent
- [x] 在 `packages/cli/src/tui/presentation/input-bar.tsx` 与 `app.tsx` 中拦截空输入 `!` 触发 Shell 模式、`Esc`/`Backspace` 退出模式，并渲染专色纯文本 `Shell` 标签
- [x] 编写输入状态机与快捷键单元测试，运行 `bun test` 保证通过

### 🎯 可演示停点 1：TUI Shell 模式纯文本指示与零摩擦切换
> **如何验证**：
> 1. 运行 `bun run dev`；
> 2. 在底部输入框为空时按 `!`，输入框左上方模式标签瞬间切换为专属青色的纯文本 **`Shell`**，提示符变为 `$ `，`!` 字符不写入文本；
> 3. 按 `Esc` 或在空文本按 `Backspace`，瞬间回到常规对话模式。

---

## 阶段二：免模型直出执行与时间线呈现（停点 2）

- [x] 在 `InteractiveController` 与 `agent-gateway` 中增加直接 Shell 执行接口
- [x] 在 Python sidecar（`harness_agent`）中实现安全受控的直接 Shell 执行后端（工作区边界、120s 超时、支持取消）
- [x] 在时间线渲染 Shell 执行卡片（实时 stdout/stderr 输出流、耗时与退出码）
- [x] 运行全套 Python 与 TypeScript 单元测试

### 🎯 可演示停点 2：本地 Shell 秒级直出执行与流式回显
> **如何验证**：
> 1. 运行 `bun run dev`，按 `!` 进入 Shell 模式；
> 2. 输入 `git status` 并按 `Enter`；
> 3. 观察命令秒级在时间线输出，不触发 LLM 推理与 Token 计费。
