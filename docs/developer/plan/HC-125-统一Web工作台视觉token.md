# HC-125：统一 Web 工作台视觉 token 与外围层级实施计划

> 原始需求：[HC-125](../task/HC-125-统一Web工作台视觉token.md)
> 规格说明：[HC-124 设计方案「视觉系统」「Topbar、Sidebar 与 Context Dock」](../spec/HC-124-统筹WebUI工作台体验升级与.md)

---

## 0. 通俗说明

**现在的问题**：`packages/cli/src/web/presentation/styles.css` 不是一套样式，而是三套视觉语言叠在一起。最底层是 HC-125 已经做完的"暖中性 + 克制蓝"扁平设计；之后 `d56baad` 又在文件尾部追加了两层覆盖样式，把浅色主题改成冷灰蓝、把消息和侧栏全部变成大圆角悬浮卡，并且这些新颜色是写死的色值，深色主题下直接失效。用户实际跑起来的界面（紫蓝色发送按钮、绿色选中线程、卡片孤岛）和设计稿对不上，根源就在这里。

**准备怎么解决**：把三层样式合并回一层。以 HC-124 设计方案为唯一目标，逐段裁决后追加的两层：符合方向的改进（例如取消 1280px 最小宽度、固定栏间距）保留并并入底层规则；不符合的（硬编码 pastel 色、紫色渐变、孤岛卡、大阴影）删除。所有颜色只能来自语义 token，深色主题必须完整。

**改完后的变化**：浅色主题回到暖中性画布 + 绿色强调（2026-08-17 与用户确认的绿色主题）；深色主题不再出现浅色气泡；消息流从"聊天软件气泡"变成扁平的阅读流；侧栏恢复上下分区而不是两座悬浮孤岛；选中、focus、运行中状态统一为绿色语义。

---

## 1. 现状样式地图（事实来源：`styles.css` 2248 行）

| 层 | 行区间 | 横幅注释 | 性质 |
| --- | --- | --- | --- |
| L1 基线层 | 1～1191 | 共享 reset 与尺寸 token / Semantic token：唯一 light/dark 映射 | HC-125 已验收的 spec 基线，含全文件唯一的 `data-theme="dark"` 块 |
| L2 Prototype fidelity layer | 1192～约 1485 | Prototype fidelity layer | **重定义了 light 主题 token 值**（`--bg: #f5f7fb`、`--accent: #6675e8` 等，把暖中性改成冷灰蓝紫），顶栏改 64px，组件直写硬编码 pastel 色；无 dark 覆盖 |
| L3 Soft card alignment | 1486～约 1692 | Soft card alignment | 侧栏改为透明容器 + 线程/文件两座 16px 圆角阴影孤岛卡；消息体改 16px 圆角阴影卡 |
| L4 Screenshot geometry contract | 1693～2248 | Screenshot geometry contract 及后续 | 布局几何 token（gap/padding/dock 宽度契约，被 `styles.test.ts` 断言）、Work Item 展示条、按截图写的消息卡规则、`@media (max-width: 1240px/900px)` |

关键事实：

- L2 在 token 层面就改写了浅色主题，所以"回到设计稿"必须连 token 值一起恢复，不能只删组件规则。
- `styles.test.ts` 目前同时断言 L1 token（取第一处出现）和 L4 几何契约，两层都"通过"，给出错误的绿色信号；新契约测试必须先写失败。
- L2～L4 中也混入了真实改进，清理时必须保留：`min-width: 1280px` 的移除、固定 16px 栏间距、Dock 开关不把中栏重新居中、`interaction-dock` 独立滚动。

## 2. 架构设计与依赖顺序

样式合并是单文件内的纵向工作，按"用户能立刻看到的变化"切三个工作包：

```text
WP1: 颜色归一（token 唯一来源 + dark 补齐）
  │  用户可见：浅色回到暖中性+蓝，紫色渐变/绿色选中消失；深色主题恢复可用
  ▼
WP2: 结构扁平化（卡片/圆角/阴影纪律 + 侧栏分区形态）
  │  用户可见：孤岛卡消失、消息流扁平、阴影只留在浮层
  ▼
WP3: chrome 尺寸收口 + 测试契约重建 + 文档
  │  用户可见：顶栏 52px、控件尺寸统一；focused 测试契约防回退
  ▼
Review & 验收
```

WP1 先行是因为颜色 token 是其余两包的前提：WP2 合并规则块时引用的必须是最终 token，WP3 的契约测试要断言"组件层无硬编码色"。

## 3. 工作包详情

### WP1：颜色归一（token 唯一来源 + dark 补齐）

- **目标**：删除 L2 的 `.web-shell[data-theme="light"]` token 重定义块，浅色恢复 HC-124 设计表（`--bg: #f7f6f3`、`--accent: #15803d` 绿色等（白底对比度 5.0，过 WCAG AA））；L2～L4 组件规则中的硬编码色全部映射到 semantic token；为所有保留规则补齐 dark 映射。
- **硬编码色映射裁决**（主要项）：
  - 选中线程 `#eefaf4/#cfe9dc` → `--accent-soft` + `--accent` 左轨（绿色即最终强调色）；
  - 头像/发送按钮紫色渐变 `#6675e8/#8a78e8` → 头像用 `--surface-2`/`--accent-soft` 平面色，发送按钮用 `--action`；
  - 用户消息气泡 `#f9fbff/#dce4fa` → `--accent-soft` / `--accent-border`；
  - 工具卡 `#fbfcff/#d9e0ee`、代码视图 `#fbfcff`、行号 `#f4f6fb` → `--surface` / `--line` / `--surface-2`；
  - 推理卡 `#f6f8ff/#8ba0f4`、审批卡 `#f5fcf8/#b9e1ce` → 推理用 `--surface-2` + `--accent-border-strong` 左边框，审批用 `--interaction-bg` 语义。
- **白名单**：语法高亮 token（`--syntax-*`）允许保留具体色值，但同样需 dark 映射。
- **改动文件**：`packages/cli/src/web/presentation/styles.css`、`packages/cli/tests/web/presentation/styles.test.ts`。
- **验证方式**：先写新契约测试（每个 semantic token 在 light/dark 各只定义一次；组件层不出现 `#` 十六进制色，token 定义与白名单除外）→ 观察失败 → 改 CSS → `cd packages/cli && bun test tests/web/presentation/styles.test.ts` 转绿。
- **可演示停点 1**：`bun run dev` 后用 `/web` 打开工作台——浅色不再是冷灰蓝，发送按钮、选中态、focus 统一为设计绿（发送/批准为 `--action` 绿色填充）；切到深色主题，消息气泡、工具卡、代码视图不再出现浅色残留。

### WP2：结构扁平化（卡片/圆角/阴影纪律 + 侧栏分区形态）

- **目标**：合并 L3/L4 的组件规则到底层唯一规则块；侧栏从"透明容器 + 两座孤岛卡"恢复为常驻上下分区的单一侧栏（border-right 分隔，非悬浮卡）；消息从气泡卡恢复为扁平阅读流（author 行 + 内容，结构用 border 不用 shadow）；圆角回到 `--radius-control: 5px` / `--radius-surface: 7px`；阴影只保留在 overlay/menu/dialog。
- **保留项**：L4 的几何契约意图保留——固定 16px 栏间距、Dock 开关不改变中栏起点、会话内容最大宽度；`@media (max-width: 1240px/900px)` 的现状行为原样保留（窄屏最终响应式归 HC-126，本任务不改交互形态）。
- **改动文件**：`styles.css`；`styles.test.ts` 中断言孤岛卡/消息卡外观的用例同步重写为扁平契约；如 DOM 结构需要微调（例如侧栏多包的一层 panel div），涉及 `workspace-sidebar/`、`timeline.tsx`，同步更新 `workspace-sidebar.test.tsx`、`timeline.test.tsx`。
- **验证方式**：`cd packages/cli && bun test tests/web/presentation/`；`bun run typecheck`。
- **可演示停点 2**：`/web` 工作台中侧栏是一整块上下分区；消息流左对齐阅读、无气泡阴影；审批卡、工具卡只剩 1px border；拖动侧栏分隔条行为不变。

### WP3：chrome 尺寸收口 + 测试契约重建 + 文档

- **目标**：顶栏高度回到 `--topbar-height: 52px` token（L2/L4 的 64/62px 覆盖删除）；控件尺寸只引用 `--control-compact/standard/field`；新建 Thread 回到 secondary/quiet（`--surface` 底 + `--line-strong` 边）；Run 状态保持 Topbar 最高状态层级；更新 `docs/user/Web界面.md` 的视觉说明；补齐对比度自查（必要文字 ≥4.5:1）。
- **改动文件**：`styles.css`、`styles.test.ts`（尺寸 token 契约）、`docs/user/Web界面.md`。
- **验证方式**：`cd packages/cli && bun test tests/web/presentation tests/web/application`；`bun run build`；`bun run typecheck`；`bun run project:check`。
- **可演示停点 3**：顶栏明显变矮、与工作区比例接近设计稿；1440×900 截图 light/dark 各一张作为验收证据。

## 4. 风险与缓解

- **风险 1：现有 `styles.test.ts` 断言了被清理的外观，改 CSS 会大面积红**。
  - *缓解*：每个 WP 内按 TDD 先重写对应契约为新目标并确认失败原因正确，再改 CSS；不允许为保绿而保留旧断言。
- **风险 2：合并时误删 L2～L4 中的真实改进（1280px 解除、几何契约、interaction-dock 滚动）**。
  - *缓解*：第 1 节的保留清单逐项核对；几何契约相关测试改为"意图断言"（固定 gap、Dock 不重排中栏）而非断言旧实现字符串。
- **风险 3：token 值恢复后个别组件对比度回退**。
  - *缓解*：WP3 统一做必要文字 4.5:1 复核；必要时只调 token 定义，不在组件层打补丁。
- **风险 4：窄屏 media query 行为与 HC-126 设计冲突**。
  - *缓解*：本任务只原样保留并在代码注释标注 HC-126 归属，不改其交互。

## 5. 回滚思路

全部改动收敛在 `styles.css`、web presentation 测试与一份用户文档；如需回滚，`git revert` 对应提交即可，无 Protocol/业务状态变更，无数据迁移。

## 6. 完成标准

对照 Task 验收清单逐条勾选：单一视觉来源、无组件层硬编码色、dark 无浅色残留、圆角/阴影纪律、新建 Thread 次级化、侧栏常驻分区、Dock 单 tab 单层 header、focused tests + build + typecheck 通过；并产出 1440×900 light/dark 截图证据写入 Task。
