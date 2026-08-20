# HC-156 实施计划：TUI 选中复制

> 规格：[HC-156 TUI 选中复制规格说明](../spec/HC-156-TUI选中复制.md)  
> 任务：[HC-156 TUI 选中复制](../task/archive/HC-156-TUI选中复制.md)

## 1. 实施边界与依赖顺序

本任务只在 `packages/cli/` 的 TUI 表现层落地，不修改 Protocol、Python Agent、Thread、SQLite 或 Timeline 数据。实现按一条纵向路径推进：先建立可独立测试的选区复制规则，再接到根 TUI，最后补齐会阻断事件冒泡的覆盖层和文档。

```text
OpenTUI renderer selection
  → selection-copy 纯 module（决策、清选区、clipboard、Toast）
  → Za38Tui 根层 mouse-up / Windows Ctrl+C
  → Sidebar 抽屉转发 + BTW 拖选保护
  → 用户文档、架构记录、全量验证与 review
```

关键实现约束：

- `selection-copy.ts` 只接收窄 dependencies；不把 renderer、Adapter 或 React component 传入 module，测试通过 fake clipboard 和 Toast 覆盖成功、`false` 与 rejection。
- 对非空选区，先 `clearSelection()` 再等待 clipboard Promise；空选区绝不产生副作用。
- Windows 的 `Ctrl+C` 必须在现有侧栏/`resolveShortcut()` 前尝试选区复制，但只有真的获得非空选区时才拦截。否则继续既有 draft → active run → exit 路径。
- 根节点是普通内容与 Dialog/Picker 的唯一 mouse-up 复制入口；Sidebar 因为主动停止冒泡，需要显式转发同一个处理器；BTW 的显式复制按钮仅在非拖选点击时执行。

## 2. 纵向实施步骤与可演示停点

### 步骤 1：选区复制 module 与最小失败测试

- 新增 `packages/cli/src/tui/presentation/selection-copy.ts`，实现平台/输入决策与“读取选区 → 清选区 → 写剪贴板 → Toast”的纯流程。
- 先新增 `packages/cli/tests/tui/presentation/selection-copy.test.ts`，让平台判定、空选区、同步清选区、复制成功、返回 `false` 和 rejection 的测试失败；再写最小实现。
- 复制 helper 吞掉写剪贴板异常并返回 `false`，避免异步 rejection 进入 TUI 错误边界。

**验证：**

```bash
cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts
```

### 步骤 2：根 TUI 接入与核心平台路径

- 在 `packages/cli/src/tui/app.tsx` 用 `useRenderer()` 建立共享选区 dependencies，并接入既有 `copyToClipboard()` 与 `adapter.showToast()`。
- 给根 `<box>` 注册 mouse-up：非 Windows 只接受左键，Windows 只接受右键；两者都仅在 helper 获得非空选区时触发。
- 在 `useKeyboard()` 的侧栏处理和 `resolveShortcut()` 前接入 Windows `Ctrl+C`，仅当 helper 实际启动复制时调用 `preventDefault()`。
- 扩展 `packages/cli/tests/tui/app-interaction.test.ts`，验证根层路由不会在空选区吞掉既有快捷键，并以纯 module 测试覆盖平台选择，避免测试中修改全局 `process.platform`。

**验证：**

```bash
cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts tests/tui/app-interaction.test.ts
```

### 🎯 可演示停点 1：主时间线选中复制

用户可运行 `bun run dev`，在主时间线拖选文本后松开鼠标：macOS/Linux 应立刻看到“已复制到剪贴板”或失败 Toast；Windows 在拖选后按 `Ctrl+C` 或右键应得到同样反馈。未选中文字时，原 `Ctrl+C` 行为保持不变。

到达此停点后，执行 Agent 必须更新对应 Todo 与 `tmp/handoff.md`，说明已跑的 focused tests 和如何观察，并等待用户确认后才进入步骤 3。

### 步骤 3：侧栏与 BTW 的事件边界

- 扩展 `packages/cli/src/tui/presentation/sidebar.tsx` 的 props，让抽屉内容在 `stopPropagation()` 前转发根层选择处理器；确保该事件不会再冒泡到根层第二次复制。
- 在 `packages/cli/src/tui/presentation/btw-modal.tsx` 通过 renderer 选区的 `isStart === false` 区分实际文本拖选与普通点击（不能依赖 OpenTUI 的 `event.isDragging`，后者在普通点击时也为真）：普通点击仍执行原有 `/btw` 回答复制；实际拖选结束不触发该按钮动作，让根层只处理当前选区。
- 为上述转发和拖选保护补充 focused interaction/component tests；同时保留现有 backdrop 关闭、文件树和 `/btw` 显式复制语义。

**验证：**

```bash
cd packages/cli && bun test tests/tui/btw-modal.test.ts tests/tui/presentation/sidebar-drawer-preview.test.ts tests/tui/presentation/selection-copy.test.ts
```

### 步骤 4：用户文档、架构记录与交付校验

- 更新 `docs/user/交互使用.md`，在快捷键说明附近写明各平台选中复制触发方式、成功/失败 Toast，以及 Windows 无选区 `Ctrl+C` 的原有优先级。
- 增量更新 `docs/developer/architecture/TUI表现层.md`，记录 selection copy 是 renderer 选区到本地 clipboard/Toast 的 presentation-only 链路。
- 完成所有 focused tests、类型检查、工作区测试和项目校验；使用 `agent-skills:code-review-and-quality` 对照 Task/Spec 检查 diff、测试与文档，把结论和证据回写 Task，然后走任务完成/归档流程。

**验证：**

```bash
bun run typecheck
bun run test
bun run project:check
```

### 🎯 可演示停点 2：完整交付验收

用户在 `bun run dev` 中分别在主时间线、侧栏代码预览和 `/btw` 浮层内拖选文本，确认每次只出现一条复制结果 Toast；在 BTW 的“复制”按钮上正常点击仍只复制回答。Windows 无选区按 `Ctrl+C` 仍执行原有清空输入、取消运行或退出语义。

## 3. 风险与缓解

| 风险 | 影响 | 缓解方式 |
| --- | --- | --- |
| mouse-up 冒泡导致同一选区重复复制 | 一次操作出现多个 Toast 或多次写剪贴板 | 根层作为默认唯一入口；Sidebar 转发后立即停止冒泡；针对转发路径做调用次数测试。 |
| 拖选结束落在 BTW 按钮上 | 同时复制 BTW 回答和实际选区 | 仅 renderer 选区 `isStart !== false` 时执行按钮复制；普通点击与真实 OpenTUI 鼠标事件均有回归测试。 |
| Windows `Ctrl+C` 拦截过早 | 破坏清空输入、取消运行或退出 | helper 无非空选区时返回 `undefined`，调用方不 `preventDefault()`；回归测试现有快捷键回退。 |
| 本机缺少 clipboard 命令 | 误报复制成功或测试依赖宿主环境 | helper 以 `copyToClipboard()` 的 boolean/rejection 为唯一结果；测试注入 fake，不调用系统进程。 |
| OpenTUI selection mock 覆盖有限 | 根层事件难以稳定回归 | 将状态/异步语义收敛到纯 module；根层测试只验证事件路由与快捷键回退，渲染器具体选择引擎不重写。 |

## 4. 完成判据

- 两个可演示停点均按上述方式由用户或自动化验证；未经停点确认不连续推进后续步骤。
- Task/Spec 的验收项均有对应测试或手工验收记录，且不会出现未处理 rejection、重复 Toast 或跨层数据变更。
- 用户文档、架构文档、Task 证据和 review 结论齐全后，才可归档 HC-156。
