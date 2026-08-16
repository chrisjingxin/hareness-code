# HC-148：统一 TUI 右上角气泡通知实施计划

> 原始需求：[HC-148](../task/HC-148-统一TUI右上角气泡通知.md)  
> 规格说明：[HC-148 规格](../spec/HC-148-统一TUI右上角气泡通知.md)

---

## 1. 架构设计与依赖顺序

整体实现按纵向切分分为两个阶段工作包（Work Packages）：

```text
WP1: TUI Adapter 气泡状态模型与生命周期管理（TDD）
  │  (定义 ToastItem/ToastVariant、showToast、队列上限 3 条、独立 3s 倒计时销毁、资源清理)
  ▼
WP2: OpenTUI ToastContainer 视觉组件与应用挂载
  │  (右上角绝对定位、zIndex: 120、多语义图标与主题配色、/btw 复制动作接入、展示验证)
  ▼
Review & 验收
```

---

## 2. 工作包详情

### WP1：TUI Adapter 状态模型与定时队列管理 (TDD)
- **目标**：在 `packages/cli/src/tui/application/adapter.ts` 建立 `ToastItem` 队列与 `showToast` API，支持多条目独立定时器与超出 3 条时的 FIFO 淘汰。
- **改动文件**：
  - `packages/cli/src/tui/application/adapter.ts`
  - `packages/cli/tests/tui/toast.test.ts` (新建测试)
- **验证方式**：
  - 运行 `bun test packages/cli/tests/tui/toast.test.ts`，验证 `showToast`、队列上限 3 条、定时器自动移出与 `close()` 清理。

### WP2：OpenTUI Toast 呈现组件与系统挂载
- **目标**：实现 `ToastContainer` 视觉组件，挂载在 `app.tsx` 顶层，接入 `/btw` 复制操作。
- **改动文件**：
  - `packages/cli/src/tui/presentation/toast.tsx` (新建组件)
  - `packages/cli/src/tui/presentation/index.ts`
  - `packages/cli/src/tui/app.tsx`
  - `packages/cli/src/tui/presentation/btw-modal.tsx` (移除浮层内多余局部复制状态，统一走右上角气泡)
  - `packages/cli/tests/tui/architecture.test.ts`
- **可演示停点 1（Checkpoint 1）**：
  - 运行 `bun run dev`，输入 `/btw 什么是 AST？` 回车；
  - 在弹窗中点击「复制」或按 `c` 键，终端右上角即时弹出绿色的 `✓ 已复制到剪贴板` 气泡并在 3 秒后自动淡出。

---

## 3. 风险与缓解

- **风险 1：定时器在组件卸载或 Adapter close 后触发导致内存泄漏**
  - *缓解*：在 `adapter.ts` 中维护活动的 `Set<Timer>` 或 `Map<string, Timer>`，在 `dismissToast` 或 `adapter.close()` 时统一 `clearTimeout`。
- **风险 2：气泡组件跨层引入破坏架构约束**
  - *缓解*：严格遵循 `presentation` 不反向依赖 `ipc`、`application` 不依赖 `presentation` 的原则，通过 `architecture.test.ts` 自动化防回退。
