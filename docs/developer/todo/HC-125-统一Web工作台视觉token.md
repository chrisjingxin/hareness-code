# HC-125：统一 Web 工作台视觉 token 与外围层级执行清单

> 原始需求：[HC-125](../task/HC-125-统一Web工作台视觉token.md)
> 实施计划：[HC-125 计划](../plan/HC-125-统一Web工作台视觉token.md)

---

## 阶段一：WP1 - 颜色归一（token 唯一来源 + dark 补齐）

- [x] 1.1 在 `packages/cli/tests/web/presentation/styles.test.ts` 先写新契约测试（TDD，确认按旧 CSS 失败）：
  - 每个 semantic token（`--bg`/`--surface`/`--accent` 等既有清单）在 light 块和 dark 块中各只定义一次（防止再次出现尾部 token 重定义块赢级联）；
  - 组件规则层不出现 `#` 十六进制色值（token 定义块与 `--syntax-*` 白名单除外）；
  - light token 值与 HC-124 设计表一致（`--bg: #f7f6f3`、`--accent: #15803d`、`--action: #15803d` 等绿色值，2026-08-17 确认）。
- [x] 1.2 删除 `styles.css` L2「Prototype fidelity layer」中的 `.web-shell[data-theme="light"]` token 重定义块与 `.web-shell { --topbar-height: 64px }` 覆盖，浅色恢复 HC-124 设计表。
- [x] 1.3 把 L2～L4 组件规则中的硬编码色逐项映射到 semantic token：选中线程绿 `#eefaf4/#cfe9dc` → `--accent-soft`+`--accent` 左轨；头像/发送按钮紫渐变 `#6675e8/#8a78e8` → 平面 token / `--action`；用户气泡 `#f9fbff/#dce4fa` → `--accent-soft`/`--accent-border`；工具卡/代码视图/行号 `#fbfcff/#f4f6fb/#d9e0ee` → `--surface`/`--surface-2`/`--line`；推理卡 `#f6f8ff/#8ba0f4` → `--surface-2`+`--accent-border-strong`；审批卡 `#f5fcf8/#b9e1ce` → `--interaction-bg` 语义。
- [x] 1.4 为 1.3 所有保留规则补齐 `.web-shell[data-theme="dark"]` 下的等价表达（依赖 token 的规则自然生效，直写色一律消灭）。
- [x] 1.5 运行 `cd packages/cli && bun test tests/web/presentation/styles.test.ts`，确认全部转绿。

> **可演示停点 1**：`bun run dev` 后输入 `/web` 打开工作台——浅色回到暖中性画布+绿色强调，紫色渐变发送按钮消失，选中线程/发送/批准统一为绿色语义；在顶栏菜单切到深色主题，消息气泡、工具卡、代码视图无浅色残留。请用户看过后再进入阶段二。

---

## 阶段二：WP2 - 结构扁平化（卡片/圆角/阴影纪律 + 侧栏分区形态）

- [x] 2.1 在 `styles.test.ts` 先写扁平化契约（TDD）：
  - `.message-body`、`.tool-card`、`.thread-item` 等结构组件不含 `box-shadow`（overlay/menu/dialog 选择器除外）；
  - 组件层 `border-radius` 只引用 `var(--radius-control)` / `var(--radius-surface)`；
  - 侧栏为单一容器（`border-right` 分隔），不存在孤岛卡规则（`workspace-sidebar-thread-panel` 的 16px 圆角阴影卡）。
- [x] 2.2 合并 L3「Soft card alignment」：侧栏恢复常驻上下分区单容器；若 DOM 需要去掉多包的 panel 层，同步改 `workspace-sidebar/` 与 `workspace-sidebar.test.tsx`。
- [x] 2.3 合并 L4 消息卡规则：消息流恢复扁平阅读流（author 行 + 内容、结构用 border）；`timeline.test.tsx` 同步更新。
- [x] 2.4 圆角回归 token（5px/7px）；阴影只保留 overlay/menu/dialog；保留 L4 几何契约意图（固定 16px 栏间距、Dock 开关不改变中栏起点、会话内容最大宽度、interaction-dock 独立滚动），并把对应测试改为意图断言。
- [x] 2.5 `@media (max-width: 1240px/900px)` 行为原样保留，注释标注窄屏最终响应式归 HC-126。
- [x] 2.6 运行 `cd packages/cli && bun test tests/web/presentation/` 与 `bun run typecheck`，确认通过。

> **可演示停点 2**：`/web` 工作台中侧栏是一整块上下分区（非两座悬浮卡）；消息流无气泡阴影、工具卡/审批卡只剩 1px border；圆角明显变小；拖动侧栏分隔条行为不变。请用户看过后再进入阶段三。

---

## 阶段三：WP3 - chrome 尺寸收口 + 测试契约重建 + 文档

- [x] 3.1 顶栏高度回到 `--topbar-height: 52px`；控件尺寸只引用 `--control-compact/standard/field`（30/34/36px）；`styles.test.ts` 尺寸 token 契约同步更新。
- [x] 3.2 新建 Thread 回到 secondary/quiet（`--surface` 底 + `--line-strong` 边）；Run 状态保持 Topbar 最高状态层级；断言更新。
- [x] 3.3 必要文字对比度复核（light/dark 均 ≥4.5:1），不达标只调 token 定义。
- [x] 3.4 更新 `docs/user/Web界面.md` 的视觉说明（单一 token 来源、扁平结构、dark 完整性）。
- [x] 3.5 运行 `cd packages/cli && bun test tests/web/presentation tests/web/application`、`bun run build`、`bun run typecheck`、`bun run project:check`，全部通过。

> **可演示停点 3**：顶栏高度回到 52px、与工作区比例接近设计稿；截取 1440×900 light/dark 各一张作为 Task 验收证据。

---

## 阶段四：Review 与闭环

- [ ] 4.1 使用 `agent-skills:code-review-and-quality` 复核 diff、测试与文档，结论写回 Task。
- [ ] 4.2 把测试证据与 1440×900 light/dark 截图写入 Task `test_evidence`，勾选验收清单。
- [ ] 4.3 运行 `bun run task:complete -- HC-125 --evidence "<命令与结果>"` 并同步看板；检视 `docs/developer/architecture/架构总览.md` 是否需要增量更新。
