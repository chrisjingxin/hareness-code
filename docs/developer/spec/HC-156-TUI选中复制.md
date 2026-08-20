# HC-156：TUI 选中复制规格说明

> 原始需求：[HC-156 Task](../task/archive/HC-156-TUI选中复制.md)  
> 竞品依据：[MiMo Code 选中复制调研](../research/156-TUI选中复制.md)

## 1. 目标

用户阅读 TUI 中的时间线、弹窗或侧栏文本时，可以直接选中内容并复制到系统剪贴板，同时在右上角看到成功或失败 Toast。该能力只属于 CLI TUI 表现层，不进入 Agent、Protocol、Thread 或 Timeline 数据。

```text
选区完成 / Windows 复制动作
  → 判断本平台是否允许该触发方式
  → 读取 OpenTUI renderer 的非空选区
  → 同步清除本次选区
  → 调用现有系统剪贴板工具
  → success / error Toast
```

OpenTUI 0.4.3 已在当前 renderer 中启用 `useMouse: true`，且提供 `useRenderer()`、`getSelection()` 与 `clearSelection()`；本功能不修改 renderer 初始化参数。

## 2. 已确认行为

| 平台 | 触发 | 有非空选区 | 无选区 |
| --- | --- | --- | --- |
| macOS / Linux / 其他非 Windows | 左键拖选后松开 | 自动复制选区 | 不写剪贴板、不提示 |
| Windows | `Ctrl+C` | 复制选区，优先于既有快捷键 | 保持既有清空输入、取消运行或退出 |
| Windows | 右键松开 | 复制选区 | 不拦截现有行为 |

- 自动复制只处理左键：OpenTUI 只由左键开始文本选区，避免普通右键和中键操作意外触发。
- 复制成功显示 `success` Toast：`已复制到剪贴板`；复制返回 `false` 或抛异常时显示 `error` Toast：`复制到系统剪贴板失败`。
- 非空选区一经接收即清除，再等待异步剪贴板结果；因此同一选区不会被同一轮事件重复提交。
- 仅 renderer 可选择的文本参与；明确标为 `selectable={false}` 的装饰内容不在范围内。

## 3. 模块与 Interface

### 3.1 Selection Copy Module

新增 `packages/cli/src/tui/presentation/selection-copy.ts`。这是唯一处理“取选区 → 清选区 → 写剪贴板 → Toast”的 module，根组件、侧栏抽屉只调用它，不能各自复制这段流程。

```ts
export type SelectionCopyInput =
  | { readonly type: "mouse-up"; readonly button: number }
  | { readonly type: "key-down"; readonly name: string; readonly ctrl: boolean }

export type SelectionCopyDependencies = {
  getSelectedText(): string | undefined
  clearSelection(): void
  writeClipboard(text: string): Promise<boolean>
  showToast(message: string, variant: "success" | "error"): void
}

/** 判断一次 TUI 输入是否允许尝试复制当前选区。 */
export function shouldAttemptSelectionCopy(
  platform: NodeJS.Platform,
  input: SelectionCopyInput,
): boolean

/** 开始复制当前非空选区；无选区返回 undefined，其余情况返回结果 Promise。 */
export function copyCurrentSelection(
  dependencies: SelectionCopyDependencies,
): Promise<boolean> | undefined
```

这个 Interface 保持窄小：module 不依赖 React、完整 `CliRenderer`、`TuiAdapter` implementation 或 IPC。调用者把 renderer 的 `getSelection()?.getSelectedText()` / `clearSelection()`、现有 `copyToClipboard()` 和 `adapter.showToast()` 注入即可。这样测试能使用最小 fake，不需要启动原生剪贴板进程。

### 3.2 触发规则

`shouldAttemptSelectionCopy()` 只表达平台/输入决策，不读取状态：

```text
非 Windows + 左键 mouse-up       → true
Windows + Ctrl+C                 → true
Windows + 右键 mouse-up          → true
其他输入                         → false
```

`copyCurrentSelection()` 只表达选区与异步副作用：

```text
getSelectedText() 为 undefined 或 ""
  → 返回 undefined，不 clear、不写剪贴板、不 Toast

非空文本
  → clearSelection()
  → await writeClipboard(text)
  → true：success Toast，返回 true
  → false 或 throw：error Toast，返回 false
```

异常必须在 module 内转为失败结果，不能形成未处理 Promise rejection 或使 TUI 错误边界退出。

## 4. 表现层接入

### 根 TUI

`packages/cli/src/tui/app.tsx` 使用 `useRenderer()` 获取 renderer，并构造共享的 `copyCurrentSelection()` dependencies。

- 外层根 `<box>` 注册 `onMouseUp`。先用 `shouldAttemptSelectionCopy(process.platform, ...)` 过滤，再尝试复制；只在 helper 返回 Promise 时标记该事件已处理。
- 全局 `useKeyboard()` 在现有侧栏和 `resolveShortcut()` 之前，先处理 Windows 的“有选区 Ctrl+C”。helper 返回 `undefined` 时不 `preventDefault()`，原有 Ctrl+C 路径完全继续执行。
- 非 Windows 的 Ctrl+C 不进入选区复制路径，维持当前快捷键语义。

### 覆盖层与事件冒泡

一个鼠标事件只能由一个 Selection Copy Module 调用，避免重复写剪贴板或重复 Toast：

- 普通 Dialog、Picker 和时间线不自行复制；事件冒泡到根 TUI 后只尝试一次。
- `Sidebar` 抽屉内容当前为关闭 backdrop 而调用 `stopPropagation()`。它必须在停止冒泡前将同一 `onMouseUp` 交给根组件传入的选择处理器，确保抽屉与预览代码也可复制且不会再到根层第二次调用。
- `BtwModal` 的“复制”按钮在普通点击时阻止冒泡，继续复制 `/btw` 回答。OpenTUI 会把普通点击的 mouse-up 也标记为 `isDragging`，因此组件以当前 renderer 选区的 `isStart === false` 判断是否发生了实际文本拖选；只有实际拖选时不执行按钮动作，让根层仅复制用户实际选中的文本。这样拖选经过按钮不会同时复制回答与选区。

选区复制不会更新 `TuiAdapterSnapshot`、`InteractiveSnapshot`、Timeline 或持久化存储；Toast 仍使用现有 Adapter 内存队列。

## 5. Invariants 与错误语义

1. **无选区无副作用**：空选区不会调用 clipboard、`clearSelection()` 或 Toast。
2. **一次事件一次尝试**：同一 mouse-up / Ctrl+C 最多启动一次异步复制；抽屉转发后必须停止向根层冒泡。
3. **原有 Ctrl+C 优先级可回退**：Windows 只有检测到非空选区才拦截 Ctrl+C；否则仍按 draft → active run → exit 的既有规则执行。
4. **成功不伪报**：仅当现有 `copyToClipboard()` 返回 `true` 才显示 success；`false` 与 rejection 都是 error。
5. **选区文本不越界**：文本只在 TUI 进程内传给 clipboard helper，不进入 Controller、IPC、日志或数据库。
6. **不新增跨层契约**：不修改 `packages/protocol/`、`packages/agent/`、SQLite schema 或公开配置。

## 6. 预期文件与验证

| 文件 | 变更职责 |
| --- | --- |
| `packages/cli/src/tui/presentation/selection-copy.ts` | 新增可测试的触发判断与选区复制 module。 |
| `packages/cli/src/tui/app.tsx` | 注入 renderer/clipboard/Toast，注册根 mouse-up 与 Windows Ctrl+C。 |
| `packages/cli/src/tui/presentation/sidebar.tsx` | 转发抽屉内 mouse-up 后保持 backdrop 的 stopPropagation。 |
| `packages/cli/src/tui/presentation/btw-modal.tsx` | 防止拖选结束触发 BTW 显式复制并造成双重复制。 |
| `packages/cli/tests/tui/presentation/selection-copy.test.ts` | 覆盖平台触发、空选区、同步清选区、成功、`false` 和 rejection。 |
| `packages/cli/tests/tui/app-interaction.test.ts` | 使用 OpenTUI mock mouse / keyboard 验证根层路由与 Ctrl+C 回退。 |
| `docs/user/交互使用.md` | 记录各平台的选中复制触发方式与 Ctrl+C 优先级。 |

实施采用 TDD：先让 `selection-copy` 的空选区、成功、失败和平台决策测试失败，再实现 module；之后接入根/侧栏/BTW 并补充交互测试。

建议验证命令：

```bash
cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts tests/tui/app-interaction.test.ts tests/tui/btw-modal.test.ts tests/tui/presentation/sidebar-drawer-preview.test.ts
bun run typecheck
bun run test
bun run project:check
```

手工验收：运行 `bun run dev`，在时间线、`/btw` 弹窗和侧栏代码预览中拖选文本；macOS/Linux 松开左键应提示复制。Windows 先拖选，再按 Ctrl+C 或右键，应提示复制；未选中文本时 Ctrl+C 仍按原行为执行。

## 7. 非范围与风险

- 不引入 OSC 52、SSH 远程剪贴板、复制历史、通知中心或单独复制按钮。
- 不改变现有 `/btw` 的 `c` 键和点击复制能力。
- OpenTUI 的选区由 renderer 管理，跨越不可选择 renderable 的具体内容以 `getSelectedText()` 实际返回为准；本任务不重写其 selection engine。
- Windows 右键在已有选区时优先复制；未选区时不拦截，保留终端或 renderer 的现有默认行为。
