# Checklist

- [x] `packages/cli/src/tui/scroll.ts` 存在，导出 `FixedSpeedScroll` 类和 `createScrollAcceleration` 工厂函数
- [x] `createScrollAcceleration()` 默认返回 `FixedSpeedScroll(3)` 实例
- [x] `FixedSpeedScroll` 正确实现 `ScrollAcceleration` 接口（`tick()` 返回固定速度，`reset()` 为空操作）
- [x] `components.tsx` 的 ConversationTimeline scrollbox 已添加 `scrollAcceleration` 属性
- [x] `overlays.tsx` 的 SearchPicker scrollbox 已添加 `scrollAcceleration` 属性
- [x] `app.tsx` 中 `handleSubmit` 提交普通消息后调用 `scrollToBottom()`
- [x] `app.tsx` 中 `selectThread` 恢复历史 thread 后调用 `scrollToBottom()`
- [x] `RunSummary` 组件显示模型名（位于 outcome 之后），模型名缺失时省略
- [x] `terminal-win32.ts` 存在，通过 `bun:ffi` 调用 `kernel32.dll` 的 `SetConsoleMode` 设置 `ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_MOUSE_INPUT`
- [x] `app.tsx` 的 `runTui` 在 `createCliRenderer` 之前调用 `win32EnableVtInput()`
- [x] `index.ts` 在 Windows conhost 下启动时输出 stderr 提示作为兜底（检测 `process.platform === "win32" && !process.env.WT_SESSION`）
- [x] `packages/cli/tests/tui/scroll.test.ts` 存在且测试通过
- [x] `bun run typecheck` 通过
- [x] `cd packages/cli && bun test` 通过（2 个失败为已有问题，与本次改动无关）
