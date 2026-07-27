# Tasks

- [x] Task 1: 创建滚动加速工具模块
  - [x] SubTask 1.1: 新建 `packages/cli/src/tui/scroll.ts`，实现 `FixedSpeedScroll` 类（实现 `@opentui/core` 的 `ScrollAcceleration` 接口，`tick()` 返回固定速度值，`reset()` 为空操作）
  - [x] SubTask 1.2: 导出 `DEFAULT_SCROLL_SPEED = 3` 常量和 `createScrollAcceleration()` 工厂函数，默认返回 `new FixedSpeedScroll(DEFAULT_SCROLL_SPEED)`；同时从 `@opentui/core` 重导出 `MacOSScrollAccel` 供触控板用户备选
- [x] Task 2: 为 scrollbox 添加 scrollAcceleration 属性
  - [x] SubTask 2.1: 在 `components.tsx` 的 `ConversationTimeline` scrollbox 上添加 `scrollAcceleration={createScrollAcceleration()}`
  - [x] SubTask 2.2: 在 `overlays.tsx` 的 SearchPicker scrollbox 上添加 `scrollAcceleration={createScrollAcceleration()}`
- [x] Task 3: 实现提交后和 thread 切换后自动滚底
  - [x] SubTask 3.1: 在 `app.tsx` 中新增 `scrollToBottom()` 回调，通过 `conversationScrollRef` 调用 `scroll.scrollTo(scroll.scrollHeight)`，使用 `setTimeout(50)` 延迟确保布局完成
  - [x] SubTask 3.2: 在 `handleSubmit` 中普通消息提交路径（`sendAgentMessage` 调用前）调用 `scrollToBottom()`
  - [x] SubTask 3.3: 在 `selectThread` 中 `commit(() => restoreThread(...))` 之后调用 `scrollToBottom()`
- [x] Task 4: 增强 RunSummary 完成元数据行
  - [x] SubTask 4.1: 在 `app.tsx` 中将当前模型名（`actualModelProfile?.model` 或 `threadModelSelection`）作为 prop 传递给 `ThreadView` → `ConversationTimeline` → `RunSummary`
  - [x] SubTask 4.2: 修改 `components.tsx` 的 `RunSummary` 组件，在 parts 数组中插入模型名（位于 outcome 之后、duration 之前），模型名为空时省略
- [x] Task 5: 补充单元测试
  - [x] SubTask 5.1: 创建 `packages/cli/tests/tui/scroll.test.ts`，测试 `FixedSpeedScroll` 的 `tick()` 返回值和 `reset()` 行为，测试 `createScrollAcceleration()` 返回 `FixedSpeedScroll` 实例且默认速度为 3

# Task Dependencies

- Task 2 依赖 Task 1
- Task 3 独立（仅修改 app.tsx）
- Task 4 独立（修改 components.tsx 和 app.tsx 的 props 传递）
- Task 5 依赖 Task 1
