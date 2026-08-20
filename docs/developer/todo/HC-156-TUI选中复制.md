# HC-156 执行清单：TUI 选中复制

> Plan：[HC-156 实施计划](../plan/HC-156-TUI选中复制.md)  
> Spec：[HC-156 TUI 选中复制规格说明](../spec/HC-156-TUI选中复制.md)

## 阶段一：选区复制 module（TDD）

- [x] 在 `packages/cli/tests/tui/presentation/selection-copy.test.ts` 先写失败测试：验证非 Windows 左键 mouse-up、Windows `Ctrl+C` / 右键 mouse-up 的决策为 true，其余输入为 false。
  - 完成信号：focused test 能明确显示尚未实现的 `shouldAttemptSelectionCopy()` 行为。
- [x] 在同一测试文件先写失败测试：空选区无副作用；非空选区先清除后写剪贴板；`true`、`false`、rejection 分别产生 success/error Toast 且返回稳定 boolean。
  - 完成信号：测试记录能确认 clipboard、`clearSelection()`、Toast 的调用顺序与次数。
- [x] 新增 `packages/cli/src/tui/presentation/selection-copy.ts`，按 Spec 定义的窄 interface 实现判定和复制流程，不依赖 React、CliRenderer、Adapter 或 IPC。
  - 完成信号：`cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts` 通过，且 rejection 不产生未处理 Promise。

## 阶段二：根 TUI 核心链路

- [x] 修改 `packages/cli/src/tui/app.tsx`：使用 `useRenderer()` 从当前 renderer 读取/清除选区，注入 `copyToClipboard()` 和 `adapter.showToast()`，为根 `<box>` 绑定平台过滤后的 mouse-up handler。
  - 完成信号：macOS/Linux 左键 mouse-up 与 Windows 右键 mouse-up 只在非空选区时调用一次 shared helper。
- [x] 修改同一全局 `useKeyboard()`：在侧栏处理和 `resolveShortcut()` 前处理 Windows 有选区的 `Ctrl+C`；无选区不阻止默认行为。
  - 完成信号：选区 `Ctrl+C` 只走复制；无选区仍沿用现有 draft → active run → exit 快捷键路径。
- [x] 扩展 `packages/cli/tests/tui/app-interaction.test.ts`，覆盖根层触发路由和 Windows `Ctrl+C` 无选区回退；平台判断继续在 pure module test 中覆盖，不修改全局 `process.platform`。
  - 完成信号：`cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts tests/tui/app-interaction.test.ts` 通过。

### 🎯 可演示停点 1：主时间线选中复制

**如何查看：**

1. 运行 `bun run dev`，进入一个含可选择文本的会话；
2. 在 macOS/Linux 的主时间线拖选文本后松开左键，观察选区清除且右上角出现一次“已复制到剪贴板”或失败 Toast；
3. 在 Windows 拖选后按 `Ctrl+C` 或松开右键，观察相同结果；不选中文本按 `Ctrl+C`，观察原清空输入、取消运行或退出语义未变。

- [x] 到达停点后勾选已完成项，更新 `tmp/handoff.md`，写明实际 focused test 结果、手工观察方式和未完成的覆盖层边界；停止等待用户确认。
  - 完成信号：用户能在本机复现主时间线选中复制，且未收到确认前不进入阶段三。

---

## 阶段三：覆盖层与事件冒泡边界

- [x] 修改 `packages/cli/src/tui/presentation/sidebar.tsx`：增加根层 mouse-up 处理器 prop，抽屉内容在 `stopPropagation()` 前转发它，保持 backdrop 点击关闭与原有文件树操作不变。
  - 完成信号：抽屉/代码预览中的非空选区只触发一次复制，事件不会再次抵达根层。
- [x] 修改 `packages/cli/src/tui/presentation/btw-modal.tsx`：普通“复制”按钮点击继续调用 `onCopy()`；以 renderer 选区的 `isStart === false` 判断实际文本拖选（不使用普通点击也会为真的 `event.isDragging`），实际拖选时不调用按钮动作，让根层处理选区。
  - 完成信号：拖选结束经过按钮不双重复制，普通点击仍复制 `/btw` 回答。
- [x] 扩展 `packages/cli/tests/tui/btw-modal.test.ts`，覆盖按钮拖选保护、普通点击只复制回答及真实 OpenTUI 鼠标点击；将现有 `/btw` Toast 测试的系统剪贴板替换为 fake，避免依赖宿主 pasteboard。
  - 完成信号：`cd packages/cli && bun test tests/tui/btw-modal.test.ts` 通过。
- [x] 扩展 `packages/cli/tests/tui/presentation/sidebar-drawer-preview.test.ts`，覆盖抽屉转发与一次调用约束。
  - 完成信号：完成 Sidebar 转发后，与 selection-copy focused test 一并通过。

## 阶段四：文档、质量检查与 review

- [x] 更新 `docs/user/交互使用.md` 的快捷键/交互说明，写明 macOS/Linux 自动复制、Windows `Ctrl+C`/右键复制、Toast 文案和无选区 `Ctrl+C` 优先级。
  - 完成信号：用户只看此文档即可判断所在平台的复制触发方式。
- [x] 增量更新 `docs/developer/architecture/TUI表现层.md`，写明 selection copy 由 renderer 选区、本地 clipboard helper 与 Adapter Toast 组成，不越过 TUI 表现层边界。
  - 完成信号：架构入口能定位该能力及其跨层边界。
- [x] 运行 `bun run typecheck`、`bun run test`、`bun run project:check`，记录实际成功输出；受 sandbox 限制而不能运行的检查按项目规范说明原因。
  - 完成信号：所有可运行检查通过，或明确记录不可运行的宿主限制及影响。
- [x] 使用 `agent-skills:code-review-and-quality` 对照 HC-156 Task/Spec review diff，修复发现的问题，并把 review 与测试证据回写 Task；完成后按任务脚本归档和同步看板。
  - 完成信号：HC-156 满足任务完成定义，Task 已归档且活动看板已同步。

### 🎯 可演示停点 2：完整交付验收

**如何查看：**

1. 运行 `bun run dev`；
2. 在主时间线、侧边栏代码预览和 `/btw` 浮层各拖选一次文本，观察每次只显示一次结果 Toast；
3. 正常点击 `/btw` 的“复制”按钮，确认仍只复制回答；
4. 按平台规则复核无选区时不提示、Windows 无选区 `Ctrl+C` 不改变原快捷键语义。

