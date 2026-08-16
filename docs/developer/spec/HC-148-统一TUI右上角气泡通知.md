# HC-148：统一 TUI 右上角气泡通知规格说明

> 原始需求：[HC-148](../task/HC-148-统一TUI右上角气泡通知.md)  

---

## 1. 目标与概述

设计并实现终端统一的右上角气泡通知系统（Toast / Bubble Notice System），用于承载操作反馈、快捷键触发反馈等高频轻量通知，提供与 Timeline 日志分离的高层级即时交互体验。

### 核心用户体验流
```text
触发源（如 /btw 复制或轻量操作）
  → 调用 TuiAdapter.showToast(message, variant?, durationMs?)
  → TUI Adapter 生成 ToastItem（含 UUID、创建时间、自动销毁定时器）并推入队列（上限 3 条）
  → TuiAdapterSnapshot 发布 toasts 状态
  → OpenTUI 根组件渲染 <ToastContainer />（右上角绝对定位，zIndex: 120）
  → 用户在终端右上角看到深色卡片与对应语义颜色图标（如 ✓ / ℹ / ⚠ / ✗）
  → 定时器到期（默认 3 秒）后条目自动从队列中移出并更新渲染
```

---

## 2. 状态模型与公共 Interface

### 2.1 状态类型 (`adapter.ts`)

```typescript
/** 气泡通知语义类型。 */
export type ToastVariant = "info" | "success" | "warning" | "error"

/** 单条气泡通知条目。 */
export type ToastItem = {
  readonly id: string
  readonly message: string
  readonly variant: ToastVariant
  readonly createdAtMs: number
  readonly durationMs: number
}

export type TuiAdapterSnapshot = {
  // ... 其他既有字段
  readonly toasts: readonly ToastItem[]
}
```

### 2.2 TUI Adapter 方法

```typescript
export interface TuiAdapter {
  getSnapshot(): TuiAdapterSnapshot
  subscribe(listener: (snapshot: TuiAdapterSnapshot) => void): () => void
  dispatch(intent: TuiIntent): Promise<void>
  close(): Promise<void>
  /** 在右上角展示轻量气泡通知（默认 3000ms 自动淡出）。 */
  showToast?(message: string, variant?: ToastVariant, durationMs?: number): void
}
```

### 2.3 语义颜色与图标规范 (`theme.ts` / `toast.tsx`)

| 语义 | 边框/图标颜色 | 图标前缀 | 默认用途 |
|---|---|---|---|
| `success` | `tuiTheme.success` (`#7FA37A` 绿) | `✓` | 复制成功、操作成功 |
| `info` | `tuiTheme.thinking` / `modeCompose` (`#7EB6C9` 蓝) | `ℹ` | 状态说明、提示信息 |
| `warning` | `tuiTheme.warning` (`#C88758` 黄) | `⚠` | 拦截警告、软性限制 |
| `error` | `tuiTheme.danger` (`#C56F6F` 红) | `✗` | 复制失败、网络异常 |

---

## 3. 布局与层级规则

1. **定位与层级**：
   - 挂载在 `app.tsx` 最顶层，采用绝对定位悬浮在终端右上角：`top={1}`, `right={2}`；
   - `zIndex: 120`（高于 `BtwModal` 的 105、`SearchPicker` 的 100、`DialogShell` 的 101）；
   - 无论在主界面（Home/Thread）还是在任何弹窗打开时，气泡均可见且不被遮挡。
2. **尺寸与换行**：
   - 最大宽度为终端宽度的 40% 或 36 字符；
   - 内部文本支持 `wrapMode="word"` 优雅折行。
3. **队列策略**：
   - 最多同时展示 3 条气泡；
   - 超出 3 条时，最旧的条目立即销毁，新条目追加到队列末尾；
   - 每条气泡独立运行定时器，定时器触发时精确移出自身。

---

## 4. 关键 Invariants

1. **零焦点夺取**：气泡通知仅为纯展示层，不包含获取键盘焦点的表单或输入框，绝不抢占输入框光标或导致快捷键失效。
2. **零数据与时间线污染**：`showToast` 纯粹为表现层内存状态，绝不向 `TimelineItem` 追加消息，不写任何持久化。
3. **资源清理**：Adapter `close()` 时必须清理所有未完成的 `setTimeout` 定时器，防止内存泄漏。

---

## 5. 非范围

- 不替换 Timeline 上的长日志、编译命令输出和多步骤进度流；
- 不提供通知中心历史抽屉或持久化历史记录。
