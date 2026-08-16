---
id: HC-148
title: 统一TUI右上角气泡通知
feature_area: 终端交互与表现层
parent_task: -
decomposed_by: chrisjingxin
priority: P1
status: 待验收
owner: chrisjingxin
branch: feat/hc-148-tui-toast
reviewed_at: 2026-08-15
review_due: 2026-08-29
scope: 设计并实现 TUI 统一右上角气泡通知系统（Toast System）：在 TUI Adapter 建立多条目队列（最多 3 条）与定时自动淡出（默认 3 秒）状态模型；在 OpenTUI 表现层实现高层级（zIndex: 120）右上角气泡浮层组件 ToastContainer，支持 success / info / warning / error 四种语义样式与图标；将 /btw 复制等轻量操作迁移至气泡通知，同时保留 Timeline 末尾长系统日志。
acceptance: 1. 在 TUI 任何界面（包括 Home、Thread 以及打开 Modal 弹窗时）触发轻量提示（如 /btw 复制成功），右上角以气泡浮层形式展示提示文本与语义图标；2. 气泡支持 success（绿色 ✓）、info（蓝色 ℹ）、warning（黄色 ⚠）、error（红色 ✗）四种变体；3. 气泡支持队列展示（最多同时展示 3 条），每条各自在持续时间（默认 3000ms）后自动淡出消失；4. 气泡层级位于顶层（zIndex: 120），不被 Modal 遮挡，不破坏键盘焦点，不向 Timeline 追加多余系统消息；5. 编写完备的自动化测试并验证通过。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-148-统一TUI右上角气泡通知.md、docs/developer/plan/HC-148-统一TUI右上角气泡通知.md、docs/developer/todo/HC-148-统一TUI右上角气泡通知.md
test_evidence: bun test packages/cli/tests/tui/toast.test.ts packages/cli/tests/tui/btw-modal.test.ts packages/cli/tests/tui/architecture.test.ts (15 passed); bun run test (714 TS passed, 2174 py passed); bun run typecheck; bun run project:check
references: docs/developer/task/archive/HC-147-实现BTW临时问答.md
completed_at: -
---

# HC-148 统一 TUI 右上角气泡通知

## 背景

在终端交互中，用户进行诸如“复制到剪贴板”、“快捷键操作反馈”、“轻量状态提示”等高频轻量操作时，若直接向时间线（Timeline）底部插入消息，会破坏阅读连贯性并造成历史冗余；若仅在局部弹窗内展示，则弹窗关闭后无法被感知，且缺少统一的系统级反馈机制。

为此，本项目需要引入一套统一的 **TUI 右上角气泡通知系统（Toast / Bubble Notice）**，在终端右上角以半透明高层级卡片形式悬浮展示即时反馈，支持自动倒计时销毁与多通知堆叠队列，全面提升终端操作的即时感知与视觉质感。

需求已使用 `mattpocock:grill-me` 与用户确认：
- 适用范围：`/btw` 复制及高频轻量操作反馈（保留 Timeline 末尾的流程型系统 notice）；
- 视觉风格：支持 `success` / `info` / `warning` / `error` 四种语义与前缀图标，深色卡片背景与细边框，层级（zIndex: 120）覆盖在主界面及浮层弹窗之上；
- 生命周期：最多同时堆叠 3 条气泡，按触发时间先后垂直排列，各条目独立倒计时（默认 3 秒）后自动淡出销毁。

## 用户最终得到什么

1. **统一优雅的即时反馈**：在任何状态下（例如在 `/btw` 弹窗中按 `c` 或点击复制），终端右上角会立即弹出带有绿色勾选图标的 `✓ 已复制到剪贴板` 气泡；
2. **零焦点干扰与零历史污染**：气泡为纯视觉浮层，不抢占输入框光标焦点，不向底层的 Timeline 时间线或持久化数据库写入无用文本；
3. **多通知有序堆叠**：快速连续产生通知时，气泡以队列形式在右上角优雅堆叠（上限 3 条），并在 3 秒后各自独立销毁；
4. **多语义色彩**：支持成功（绿）、信息（蓝）、警告（黄）、错误（红）四种语义，直观表达操作结果。

## 范围

- **表现状态层 (`packages/cli/src/tui/application/adapter.ts`)**：
  - 定义 `ToastItem`（`id`, `message`, `variant`, `durationMs`）与 `ToastVariant` 类型；
  - 在 `TuiAdapterSnapshot` 中发布 `readonly toasts: readonly ToastItem[]`；
  - 在 `TuiAdapter` 内部实现 `showToast(message, variant?, durationMs?)`、`dismissToast(id)` 及定时器管理（队列上限 3 条，默认 3000ms）。
- **TUI 呈现层 (`packages/cli/src/tui/presentation/toast.tsx`)**：
  - 创建 `ToastContainer` 与 `ToastBubble` 组件；
  - 根据终端宽度与尺寸自适应计算右上角绝对定位，`zIndex: 120`；
  - 渲染语义图标与主题配色。
- **组合与挂载 (`packages/cli/src/tui/app.tsx`)**：
  - 在根渲染树末尾挂载 `<ToastContainer />`，传入 `snapshot.toasts`。
- **业务接入**：
  - 在 `/btw` 复制成功时触发 `showToast("已复制到系统剪贴板", "success")`；
  - 复制失败时触发 `showToast("复制到系统剪贴板失败", "error")`。

## 验收项

- [x] `TuiAdapter` 拥有完整的 `ToastItem` 队列状态与 `showToast` API，支持默认 3000ms 自动销毁与上限 3 条队列管理。
- [x] `ToastContainer` 正确在 TUI 根层右上角渲染，层级（zIndex: 120）高于 `BtwModal` 与普通 Dialog。
- [x] 支持 `success` / `info` / `warning` / `error` 四种语义样式与图标。
- [x] `/btw` 复制操作接入 `showToast`，按下 `c` 或点击按钮后右上角准确弹出成功气泡。
- [x] 编写 TUI Adapter 与 Toast 组件的单元测试，验证队列堆叠、自动销毁与渲染逻辑。

## 评审记录 (agent-skills:code-review-and-quality)

1. **正确性 (Correctness)**：`showToast` 正确生成 UUID 条目与定时器，超时后精准移除自身；队列达 3 条时以 FIFO 方式清理旧定时器并淘汰旧条目；`close()` 时清理所有定时器句柄，防止泄漏与报错。
2. **可读性 (Readability)**：代码命名语义清晰，TSDoc 完善，组件结构精简，语义图标（✓ / ℹ / ⚠ / ✗）与配色对比分明。
3. **架构分层 (Architecture)**：纯单向数据流驱动，`presentation` 仅通过 props 接收 `toasts`，不直接依赖 IPC 或可变状态；`adapter` 统一维护内存表现状态，与共享 Timeline 彻底解耦。
4. **安全性 (Security)**：无命令注入或不受信字符串执行风险，气泡作为纯展示层不争夺焦点，不破坏键盘交互流。
5. **性能 (Performance)**：无气泡时组件直接返回 `null`，零渲染与布局开销；队列严格有界。
