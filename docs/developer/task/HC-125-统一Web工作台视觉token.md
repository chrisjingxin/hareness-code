---
id: HC-125
title: 统一 Web 工作台视觉 token 与外围层级
feature_area: Web UI 工作台体验升级
parent_task: HC-124
decomposed_by: Codex
priority: P1
status: 进行中
owner: DeepSeek
branch: master
reviewed_at: 2026-08-17
review_due: 2026-08-31
scope: 将当前视觉收敛为暖中性色画布加绿色交互强调的扁平专业工作台风（2026-08-17 与用户确认，以参考设计稿的绿色主题为准），统一 light/dark token、控件尺寸、Topbar、Sidebar 与 Context Dock chrome；清理 d56baad 追加的样式覆盖层，使 styles.css 回到单一 token 来源，消除硬编码色、大圆角与卡片阴影堆叠，补齐 dark 主题覆盖；并消除新建 Thread 和外围面板抢占当前 Run 注意力的问题。
acceptance: styles.css 不存在多套互相覆盖的视觉层，组件颜色只经 semantic token 表达（白名单除外）；light/dark token 与 HC-124 设计表一致且 dark 主题无未覆盖的浅色硬编码；必要 12～14px 文字达到 WCAG AA；新建 Thread 为次级动作；Run 状态是 Topbar 最高状态层级；Context Dock 维持单 tab 单层 header；阴影只用于 overlay/menu/dialog；现有 Web 工作流和 intent 不变；focused DOM/CSS 测试、build、typecheck 通过。
user_docs: docs/user/Web界面.md
developer_docs: docs/developer/spec/HC-124-统筹WebUI工作台体验升级与.md
test_evidence: "focused: cd packages/cli && bun test --isolate tests/web/presentation/styles.test.ts tests/web/presentation/web-app.test.tsx tests/web/presentation/workspace-sidebar.test.tsx tests/web/presentation/context-dock.test.tsx（49 pass, 0 fail）；bun run typecheck（通过）；bun run build（通过）；bun run project:check（通过）；bun run docs:check（通过）；bun run tasks:check（通过）；git diff --check（通过）。bun test --isolate tests/web（193 pass, 6 fail）：5 个 loopback EADDRINUSE、1 个 bundle EISDIR。bun run test（CLI 533 pass, 1 skip, 7 fail）：另有 sidecar 集成失败；Python 未由聚合脚本执行，直接 bun run test:py 因 packages/agent/.venv/bin/python 不存在退出 127。"
references: docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md、docs/developer/task/HC-106-按蓝色工作台设计整改WebUI.md、docs/developer/task/HC-107-修复WebComposer、补.md
completed_at: -
---

## 背景

当前暖单色基础具有良好的长期阅读质感，但黑色被同时用于新建 Thread、发送、选中和 focus，无法明确区分“主要操作”和“当前状态”。旧 HC-106 的蓝色大方向与当前 CSS 也没有形成新的单一事实来源。

## 当前存在的问题

2026-08-17 复核确认：`d56baad`（feat(web): establish initial workbench UI baseline）在 HC-125 已收敛的 token 层之后，向 `packages/cli/src/web/presentation/styles.css` 尾部追加了约 950 行覆盖样式，并在文件尾部再引入 “Soft card alignment” 段；当前样式表实际由三套视觉语言叠加，后写规则靠源码顺序赢级联：

- 组件颜色大量绕过 semantic token 直写硬编码色：选中 Thread 绿色 `#eefaf4/#cfe9dc`、用户头像与发送按钮紫色渐变 `#6675e8/#8a78e8`、用户消息气泡 `#f9fbff`、工具卡 `#fbfcff`、推理卡 `#f6f8ff`、审批卡 `#f5fcf8`、代码行号 `#f4f6fb` 等。
- 追加层没有任何 `data-theme="dark"` 覆盖，dark 主题下浅色气泡、浅绿卡与渐变按钮直接失效，HC-125 原 dark 验收事实上被回滚。
- 圆角从 token 的 5/7px 漂移到 9～14px 任意值；消息、工具卡、侧栏分区全部卡片化并带阴影，违反“结构边界用 border，阴影只给 overlay/menu/dialog”。
- Sidebar 被改成透明容器内的“线程/文件”两块悬浮孤岛卡，空态时出现大片无信息空白；与 HC-124 信息架构图的常驻分区形态不一致。
- 浅色 muted 小字对比度不足；dark 的 subtle 也不适合必要 metadata。
- 新建 Thread 在追加层中改用 `accent-soft` 填充，仍偏强，需要回到 secondary/quiet。
- 26/32/36px 控件尺寸仍有任意混用，窄屏尺寸尚未由统一 token 表达。

## 目标设计

```text
semantic token
  → light/dark 映射
  → chrome / content / action / state 各用固定角色
  → 组件不再写临时 raw color 和任意尺寸
```

完整色值、字体、尺寸和组件层级以 [HC-124 设计](../spec/HC-124-统筹WebUI工作台体验升级与.md)“视觉系统”及“Topbar、Sidebar 与 Context Dock”为准。

## 实施步骤

1. 合并 `styles.css` 的三套叠加层：以 HC-124 视觉系统为唯一目标，逐段裁决 baseline 追加层与 “Soft card alignment” 段，删除被否决的规则，保留的改进并入对应组件的唯一规则块，禁止依赖源码顺序的后置覆盖。
2. 建立唯一 light/dark semantic token 表；组件颜色只允许引用 token，硬编码色仅限 token 定义与明确白名单（如语法高亮）。
3. 将必要 muted 文本、focus、selected、running、primary action 分配到语义色；选中 Thread、头像、发送按钮回到 `--accent`/`--action` 语义，移除渐变。
4. 圆角、阴影回到 token 纪律：控件 5px、卡片 7px；阴影只用于 overlay/menu/dialog，消息与工具卡用 border 表达结构。
5. 为全部保留规则补齐 `data-theme="dark"` 映射，dark 下不允许残留浅色硬编码。
6. 调整 Topbar：Run status 提升、Model/Approval 保持次级、返回/overflow 保持可达。
7. 调整 Sidebar：恢复常驻上下分区形态（非孤岛卡），新建 Thread 使用 secondary/quiet；active Thread 使用 accent rail/surface。
8. Context Dock 维持单 tab 单层 header（2026-08-17 与用户确认：不采用设计稿的右栏多段常驻堆叠，状态/模型等不需要时刻展示）。
9. 增加 token、class 唯一来源、无硬编码色、dark 覆盖完整性和对比度的 focused tests；更新 Web 用户视觉说明。

## 范围

- `packages/cli/src/web/presentation/styles.css`。
- `web-app.tsx`、`workspace-sidebar/`、`context-dock/` 的 chrome 结构。
- 对应 Web presentation tests 和用户文档。

## 非范围

- 不实现响应式抽屉或 viewport state（HC-126）。
- 不改 Timeline/Tool 内容结构（HC-127）。
- 不改变 Protocol、Controller、Handoff 或主题持久化策略。
- 不引入字体、动画或 UI framework 依赖。

## 验收清单

- [ ] warm neutral + green accent 在 light/dark 中一致生效，selected/focus/running 可区分（running 叠加文字/动画形态差异）。
- [ ] styles.css 单一视觉来源：无互相覆盖的多套层，组件规则块唯一，不依赖源码顺序覆盖。
- [ ] 组件层无硬编码色（token 定义与白名单除外），dark 主题无未覆盖的浅色残留。
- [ ] 圆角回到 5/7px token；阴影只出现在 overlay/menu/dialog。
- [ ] 所有必要小字和控件图标满足对比度要求，disabled/decorative 与必要文字 token 分离。
- [ ] 新建 Thread 为 secondary/quiet，Send/Interaction primary 保持唯一主动作。
- [ ] Sidebar 为常驻上下分区；Context Dock 只保留单 tab 单层 header，现有 tab 与 Help 行为不回归。
- [ ] focused tests、`bun run build`、`bun run typecheck` 通过。

## 定期复核记录

- 2026-08-09（Codex）：已认领并完成 HC-125 范围内实现；暖中性色/蓝色 semantic token、Topbar/Sidebar/Dock chrome、focused DOM/CSS 测试和用户文档已落地。focused 测试、typecheck、build、project/docs/tasks check 通过；全量 Web 测试仍有 6 个环境相关失败，Python worktree 虚拟环境缺失，因此任务保持“进行中”，下一次复核 2026-08-23。
- 2026-08-17（与用户复核）：对照实际运行截图确认 `d56baad` baseline 追加层回滚了本任务的 token 纪律（三套样式叠加、硬编码 pastel 色、dark 无覆盖、圆角/阴影漂移、Sidebar 孤岛卡），原验收项不再成立，任务重新打开为“待认领”并扩大 scope 到样式层清理。用户同时确认两个方向决策：视觉回到 HC-124 扁平专业工作台风；Context Dock 维持单 tab，不采用右栏状态/模型多段常驻堆叠。2026-08-09 的执行证据仅对追加层之前的状态有效。下一次复核 2026-08-31。
- 2026-08-17（DeepSeek 认领后）：用户进一步明确视觉方向以参考设计稿的**绿色主题**为准（画布保持暖中性，发送/批准等唯一主动作统一绿色填充），HC-124 spec「视觉系统」已同步改色。WP1 颜色归一完成：L2 token 重定义块删除，约 40 处硬编码色映射到 semantic token，L1 失效 token 名（`var(--border/--panel/--primary, …)`）与 Compose chips 直写色修复；取消按钮从联合填充规则拆出恢复 danger 描边。新契约测试先红后绿（20 pass），typecheck 通过；浏览器实测 1440×900 light/dark 验证 dark 无浅色残留。
- 2026-08-17（WP2 结构扁平化完成）：`styles.css` 尾部 L2「Prototype fidelity layer」/L3「Soft card alignment」/L4「Screenshot geometry contract」三段合并为单一「工作台几何与密度层」（文件 2248→1591 行），该层只表达布局/间距/几何并带注释禁令。侧栏恢复单一平面容器上下分区；消息与 Agent 组从气泡卡改为扁平阅读流（24px 平面 avatar、内容 34px 对齐）；工具/推理/审批卡与 Composer、Context Dock 只留 1px border，圆角全部回到 5/7px token；阴影只留在 menu/dialog；Work Item 横幅模式指示按用户要求去 pill 化为轻量文本（位置重设计另议）。保留改进：min-width 1280 解除、16px 栏间距几何契约、Dock 不重排中栏、窄屏 media query（标注归 HC-126）。新增扁平化契约测试先红后绿；presentation 132 pass、typecheck 通过；浏览器实测 light/dark 运行态（含绿色发送/danger 取消）。剩余：WP3 chrome 尺寸收口与文档。
- 2026-08-17（贴边布局与选中态简化，与用户确认）：① 选中 Thread 去掉左侧 inset 2px 色轨，只由 accent-soft 浅绿背景表达（`Web界面.md` 同步）；② 三栏改为贴边布局——侧栏贴窗口左缘、Dock 贴右缘，两栏从顶栏下缘全高到底，去圆角去全边框、只留面向中栏的 1px 分隔线；容器外边距归零（`--workspace-padding-inline: 0`），呼吸感移到中栏上下内边距（新增 `--conversation-padding-block-start/end` token）；几何契约测试重写防回退。styles 28 pass、presentation+application 208 pass、typecheck 通过。
- 2026-08-17（视觉细节打磨，与用户逐项确认）：① 空态文案「发送第一条消息后…」从 13vh 顶压改为 `margin: auto` 双居中；② run-status-live 在 home/idle 时不再显示「就绪」（就绪即沉默），已完成/失败/取消/压缩等瞬态标签保留；③ Thread 列表图标由 lucide MessagesSquare 换为内联 antd CommentOutlined SVG 源码（@ant-design/icons-svg 4.4.2，MIT，不引入依赖，新增 presentation/icons/ 目录）；④ 文件树按类型着色图标体系：8 类映射（ts/js/code/style/doc/config/image/default，特殊文件名优先），新增 6 个 `--file-*` token（light/dark 各一套，色值取自语法色板），选中行保留类型色；⑤ 文件行图标 1px 光学下沉（`top: 1px`），与 15px/1.6 文字光学中心对齐，经等参数对照页 0/1/2px 截图实测选定。TDD 先红后绿：新增 file-type-icon 测试文件与 3 条 styles/timeline 契约；presentation+application 207 pass、typecheck 通过。
- 2026-08-17（模式指示收口完成）：与用户确认横幅收口方案——无 Work Item 时 `WorkItemBanner` 整体不渲染（空态「当前无进行中的工作项」与「Tab 切换工作模式」文本删除），有 Work Item 时对话列顶部只保留工作项卡；工作模式指示并入 Composer rail 的 `.mode-chip` 作为界面唯一模式展示，未锁定提示 Tab 切换、锁定（threadMode 非空）显示锁图标与冻结模式；审批模式 chip 保留（避免 Compose 被误解为自动授权）。TDD 先红后绿：composer/work-item-view/styles 三个契约测试文件 41 pass，presentation+application 199 pass，typecheck 通过。剩余：review 与用户验收后 task:complete。
- 2026-08-17（WP3 完成）：顶栏高度回到 52px token（删除 62px 覆盖）；发送/取消按钮、侧栏搜索、新建 Thread 图标、文件更多按钮尺寸全部回到 control token；Thread/Files 分隔槽改为 9px 透明命中区 + 居中 1px 分隔线（hover/拖动变强调色），分区标题规格统一。对比度复核（自算 WCAG 比值）：light 下原 `#16a34a` 绿 accent 在白底仅 3.30 不达标，token 加深为 `#15803d`（5.02，AA 通过），`--accent-hover` 同步为 `#166534`；dark 全部通过。`docs/user/Web界面.md` 视觉说明重写为新设计（并修正了其中不存在的分支/活动状态/Coding Agent 等原型稿漂移描述）。验证：presentation+application 196 pass、typecheck、build、project:check 全通过；1440×900 light/dark 截图实测。剩余：review 与用户验收后 task:complete。

## 2026-08-09 执行证据

- 可观察变化：Run 状态成为 Topbar 最高状态层级；新建 Thread 使用 secondary chrome；当前 Thread 使用蓝色轻背景和左侧轨道；Context Dock 的标题、tab 与关闭入口收敛到同一层 header；light/dark 使用 HC-124 暖中性色与稀缺蓝色 token。
- 保持不变：未修改 Protocol、Python AgentHost、RunCoordinator、Handoff/ControlLease、InteractiveSnapshot、Web intent 或 HC-118 Timeline/reasoning/progress 语义。
- 对比度复核：必要的 light/dark 文本、状态文字、按钮和 focus 颜色均达到 WCAG AA；subtle 仅用于 disabled/decorative 场景。
- 版本影响：源码开发阶段不修改根 `VERSION`、包版本或 `CHANGELOG.md`；当前无提交、无 PR reference。
