# HC-148：统一 TUI 右上角气泡通知执行清单

> 原始需求：[HC-148](../task/HC-148-统一TUI右上角气泡通知.md)  
> 实施计划：[HC-148 计划](../plan/HC-148-统一TUI右上角气泡通知.md)

---

## 阶段一：WP1 - Adapter 气泡状态模型与生命周期 (TDD)
 
- [x] 1.1 在 `packages/cli/tests/tui/toast.test.ts` 中编写测试用例：
  - 验证 `adapter.showToast(msg, variant)` 能够推入 `toasts` 状态；
  - 验证多条推送时队列上限截断（最多 3 条，超出时淘汰最旧条目）；
  - 验证单条经过指定 `durationMs` 后自动从 `toasts` 中移除；
  - 验证 `adapter.close()` 正确清理所有活动定时器。
- [x] 1.2 在 `packages/cli/src/tui/application/adapter.ts` 中定义 `ToastVariant`、`ToastItem`，并在 `TuiAdapterSnapshot` 中暴露 `toasts: readonly ToastItem[]`。
- [x] 1.3 在 `TuiAdapter` 内部实现 `showToast`、`dismissToast` 与定时器清理逻辑。
- [x] 1.4 运行 `bun test packages/cli/tests/tui/toast.test.ts`，确保测试全部通过。

---

## 阶段二：WP2 - OpenTUI 呈现组件与系统挂载

- [x] 2.1 创建 `packages/cli/src/tui/presentation/toast.tsx`：
  - 实现 `ToastContainer` 与 `ToastBubble` 组件；
  - 容器固定右上角绝对定位（`top: 1`, `right: 2`, `zIndex: 120`）；
  - 实现 `success`（✓ 绿）、`info`（ℹ 蓝）、`warning`（⚠ 黄）、`error`（✗ 红）四种语义样式与文本自动折行。
- [x] 2.2 在 `packages/cli/src/tui/presentation/toast.tsx` 导出并在 `app.tsx` 挂载。
- [x] 2.3 在 `packages/cli/src/tui/app.tsx` 根视图顶层挂载 `<ToastContainer toasts={snapshot.toasts} terminalWidth={terminal.width} />`。
- [x] 2.4 在 `packages/cli/src/tui/application/adapter.ts` 的 `copyBtwAnswer` 中调用 `this.showToast("已复制到系统剪贴板", "success")`（失败时调用 `this.showToast("复制到系统剪贴板失败", "error")`），清理 `BtwModal` 内部冗余的 copied 显示。
- [x] 2.5 运行 `bun test packages/cli/tests/tui/architecture.test.ts` 与 `bun run typecheck`，确保分层架构无跨层依赖。

> **可演示停点 1（Checkpoint 1）**：
> 运行 `bun run dev`，输入 `/btw 什么是 AST？` 回车，在弹窗中按 `c` 或点击「复制」按钮，观察终端右上角即时弹出绿色的 `✓ 已复制到系统剪贴板` 气泡并在 3 秒后优雅消失。

---

## 阶段三：WP3 - 文档、审查与归档

- [x] 3.1 更新用户文档 `docs/user/交互使用.md` 关于右上角气泡通知机制的说明。
- [x] 3.2 使用 `agent-skills:code-review-and-quality` 进行五轴审查并将结论记录回 Task。
- [x] 3.3 运行 `bun run project:check` 与全量测试。
- [ ] 3.4 运行 `bun run task:complete` 并同步看板（待用户验收）。
