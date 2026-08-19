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
- 2026-08-17（无 Dock 时中栏右缘留白）：贴边布局后 Dock 收起时中栏右缘紧贴窗口边缘；新增 `--workspace-padding-end: 20px` token，`.has-context-dock` 时归零（Dock 自身贴右缘）；几何契约补 3 条断言，`Web界面.md` 同步。CLI focused 测试与静态检查通过。
- 2026-08-18（消息字号/Composer 聚焦/模型段 chevron 三处打磨，与用户逐项确认）：① 用户消息纯文本 div 由继承 15px/1.6 对齐到 AI markdown 正文规格 14px/1.7（token 统一遗留，非故意设计）；② Composer 聚焦错位绿框修复——全局 `textarea:focus-visible` outline 包在全宽 textarea 上渲染成 tab 下/输入区下两条错位绿线，改为 textarea 不画 outline、整卡 `.composer-box:focus-within` 描边变 `--accent`；③ 顶栏模型段删除 ChevronDown——点击是展开右侧 Dock 面板而非下拉菜单，chevron 属错误 affordance（审批模式真下拉保留）。TDD 先红后绿：styles +2 契约、web-app +1 契约（断言用 boolean 比较，避免 toBeNull 失败时序列化 SVG 节点超时）；presentation+application 211 pass、typecheck 通过。
- 2026-08-18（顶栏按钮与侧栏默认高度，用户点名小改）：① 「返回 TUI」从 button-secondary 灰边框药丸改为 ghost 形态——无边框透明底 + text-soft 文字，hover 浮现 accent-soft 浅绿底，与扁平顶栏一致；② Thread 分区默认高度修复截断——新增 `threadRatioCustomized` 快照字段（拖过分隔条才为 true），未拖动时面板 `max-height` 取比例上限、高度随内容自适应（短列表不再截成半行），拖动后回到显式比例固定高度；`Web界面.md` 同步。验证：presentation+application 212 pass、typecheck 通过（happy-dom 丢弃 height 上 calc 乘法值为测试环境限制，真实浏览器正常，分支用 data 属性断言）。
- 2026-08-18（主题切换提出菜单、菜单去掉审批模式入口，用户点名）：① 浅色/深色切换从「更多操作」菜单项改为顶栏单图标按钮——light 显示 lucide Moon、dark 显示 Sun，icon 与 aria-label/title 表达下一动作（通用网页深浅主题切换惯例）；② 菜单删除「选择审批模式」项（顶栏已有审批模式控件，属重复入口），菜单只保留帮助/退出 Harness；`Web界面.md` 主题节同步。验证：presentation+application 212 pass、typecheck 通过。
- 2026-08-18（顶栏信息层级重构：模型/审批下沉 Composer rail，与用户确认方向并参考 DSH rail 布局）：用户指出顶栏工作区/模型/审批三段视觉同级但行为完全不同（不可点/开 Dock/下拉），逐项确认后重构——① 工作区改为单行只读位置标识：脱离 chip 竖线段形态（去掉 `topbar-segment` 类与 `::before` 分隔线），Folder 图标（subtle）+ 项目名（muted）单行展示，悬停 title 显示完整路径，与 brand 同属「身份区」；② 模型与审批模式从顶栏删除，下沉为 Composer rail 两个同构下拉：左端审批模式（ShieldCheck + 当前值 + chevron，运行中/交互中锁定），旁边保留工作模式 Build chip（用户确认保留）；右端模型下拉紧邻发送键（Bot + 当前模型 + chevron，打开时经新增 `models-catalog-refresh` intent 刷新目录但不开 Dock，选项含 provider·model 副标题、不可用项禁用并提示原因，底部「管理模型…」开 Dock Models 面板）；菜单向上弹出（`bottom: calc(100% + 6px)`），目录数据直接复用 `interactive.catalogs.models`，无新快照形状；③ rail 清理：装饰图标（📎/@/\//）、「Enter 发送」提示文字、只读审批 mode-chip 删除（用户确认）；④ 顶栏死样式整套清除（`.topbar-segment/.topbar-meta/.meta-chip*/.topbar-model/.topbar-approval-mode*/.approval-mode-*` 及对应 media query 规则），契约测试补「历史 class 已删除」清单。web-app Escape 顺序相应移除两个菜单分支并加 `defaultPrevented` 守卫（rail 菜单自行关闭并 stopPropagation）。TDD 先红后绿：web-app/composer/styles/adapter 四文件契约先行，`Web界面.md` 顶栏与 Composer 节同步。验证：presentation+application 219 pass、typecheck 通过。
- 2026-08-18（rail 下拉裁剪修复 + 工作区标识再定位为文件分区标题，用户实测反馈后逐项确认）：① 用户实测发现 rail 审批/模型下拉点击不可见——用真实 CSS 静态复现页 + getBoundingClientRect 逐级 overflow 检查定位为基础层 `.composer-rail-left { overflow: hidden }`（键盘提示文字时代的遗留）把上弹菜单整体裁掉；删除该 overflow，新增「Rail 容器不声明 overflow」契约测试防回归，复现页验证两个菜单均正常弹出；② 工作区标识再定位：用户认为顶栏路径突兀，确认改为侧栏文件分区标题直接表达工作区——「文件」固定文案删除，标题变为 Folder 图标 + 工作区名（`file-explorer-title-text` 可省略，悬停 title 显示完整路径），顶栏中段彻底清空（`.topbar-project/.project-name` 规则与 media query 一并删除，并入 legacy 契约清单）；③ `Web界面.md` 顶栏节同步。验证：presentation+application 220 pass、typecheck 通过。
- 2026-08-18（rail chip 字号与灰度对齐 DSH 参考，用户点名）：审批/模型 chip 文字 12→14px、颜色 `--text-soft`→`--muted`（更灰一档），图标 13→15px、chevron 12→13px；hover/展开态仍加深为 `--text` 作交互反馈。新增 rail-chip 字号/灰度契约测试。验证：presentation+application 221 pass、typecheck 通过。
- 2026-08-18（文件分区标题去图标，用户提议并确认）：分区标题（工作区名）去掉 Folder 图标——线程分区标题无图标，两分区标题应保持同一纯文字规格（图标是顶栏位置标识时代的语义残留）；契约断言两分区标题同 14px/700 且无图标规则。验证：presentation+application 221 pass、typecheck 通过。
- 2026-08-18（工具调用结构化改造轮 1/3：展开态骨架 + 折叠态轻调，方向与数值与用户逐项确认）：用户反馈工具调用展示简陋、不便读取，确认三轮改造路线（① 展开态骨架 ② read_file/ls/glob/grep 结构化 ③ diff 视图 + execute 终端块），本轮落地轮 1——① 折叠态：展开 chevron 从 hover 才显现改为常驻（可发现性），失败状态在整行淡红底/红条之外新增「失败」文字徽章（完成/运行不占文字位）；② 展开态：输出区卡片化（1px `--line` border + `--radius-surface` + `--tool-output-bg` 底，无阴影），头部条含标题/行数统计/一键复制按钮（复制成功对勾反馈 1.2s），内容等宽 12.5px/1.65，JSON 对象/数组自动美化两空格缩进（解析失败回退原文），超过 24 行（`TOOL_OUTPUT_COLLAPSE_LINES`）钳制 280px 并给「展开全部/收起」就地切换（展开后 560px 内独立滚动）；③ 「参数」从次级 details 升级为与输出同级的卡片（默认折叠一行头，点开看美化 JSON）；④ 新增 `presentation-shared/tool-output-render.ts` 纯函数模块（渲染模型，解析失败不抛异常），`Web界面.md` 同步。TDD 先红后绿：新模块单测 6 条 + timeline 4 条 + styles 契约 3 条。验证：presentation+application+presentation-shared 316 pass、typecheck、build 通过；真实 styles.css 静态复现页（tmp/tool-card-preview.html）1440 宽 light/dark 截图实测。
- 2026-08-18（工具调用结构化改造轮 2/3：read_file/ls/glob/grep 分类型渲染，用户确认路线后连续执行）：先实锤四种输出格式（read_file = JSON 带 `shown_lines`/`content`（`行号\t内容`）；ls/glob = Python repr 字符串列表；grep 三种 output_mode 文本格式，来自 deepagents `format_grep_matches`），渲染模型按工具名分派——① `read_file` → file-content：元信息行（path · 第 start–end 行 / 共 N 行 · 已截断？）+ 行号 gutter（右对齐 subtle 不可选中）+ 内容行；② `ls/glob` → path-list：repr 迷你解析器（单双引号/转义，异常即回退），每行文件类型图标（复用侧栏 `FileTypeIcon` 与 `--file-*` token，目录尾斜杠用 Folder）+ 路径，meta 计「N 项」；③ `grep` → content 模式按文件分组（路径头加粗 + 行号 + 命中行）、count 模式「N 处匹配」、files_with_matches 模式（每行绝对路径）按 path-list 渲染、「No matches found」回退纯文本；④ 渲染模型改为判别联合（`kind: text/json/file-content/path-list/grep-matches`），头部 meta 文案由模型给出（N 行/N 项/N 个文件），复制文本结构化种类给原始 output；全部解析失败安全回退 text/json。TDD 先红后绿：模块单测重写 18 条 + timeline 3 条 + styles 契约 1 条。验证：presentation+application+presentation-shared 330 pass、typecheck、build 通过；静态复现页补三类结构化卡片后 1440 宽 light/dark 截图实测。剩余轮 3：edit/write diff 视图 + execute 终端块。
- 2026-08-18（工具调用结构化改造轮 3/3 完成：diff 视图 + 终端块，用户确认路线后连续执行）：先实锤格式——edit/write 输出只有变更后上下文窗口（无 diff），红绿行改从 **arguments**（`old_string`→`new_string` / `content`）经行级 LCS 计算（规模上限 25 万 cell，超出退化「旧全删+新全增」），且仅当输出 JSON `ok: true`（变更确已落盘）才渲染 diff，否则回退 file-content/json——不伪造未生效变更；execute 输出为合并 stdout/stderr 原文 + 尾部 `[Command succeeded|failed with exit code N]` / 截断标记（deepagents 约定）。渲染：① diff 视图 = 路径 meta 行 + context/add/remove 行（复用审批 Diff 的 `--diff-add/remove-bg/sign` token，未引新色），头部 meta 显示 `+N −M`；② 终端块 = `$ command` 加粗命令行 + 输出行，头部 meta 显示 `exit N`（无标记时计行数），截断显示暖色提示，尾部执行标记不进入渲染；③ `toolOutputView` 签名加 `argumentsText` 参数。TDD 先红后绿：模块单测 +7 条（顺带修正轮 1 两条以 execute 为名的通用回退用例，execute 现已恒为 terminal）+ timeline +2 条 + styles 契约 +1 条。验证：presentation+application+presentation-shared 340 pass、typecheck、build 通过；静态复现页补 diff/终端卡片后 1440 宽 light/dark 截图实测。三轮工具调用结构化改造全部完成，待用户验收后攒版提交。
- 2026-08-18（流式光标滞留修复，用户截图反馈）：运行中历史 Assistant 文本段（`appendAssistant` 标记 `streaming: true`，run 结束时才由 `finishAssistant` 统一清除）后面已经跟了工具卡，仍挂着闪烁光标——`StreamingAssistantBubble` 此前对任何 streaming 段都渲染 `.streaming-cursor`；刷新后历史恢复不走 streaming 标记，所以「刷新即消失」。修复：Timeline 计算最后一项的 `liveKey` 逐层透传（ActivityGroup/TimelineGroup/TimelineRow），光标只在 `timelineItemKey(item) === liveKey` 的段上渲染——生成位置随工具/新文本段后移，全程最多一个光标。TDD 先红后绿（timeline +1 条三分支断言：中段无光标/末段有光标/全局唯一）。验证：presentation+application+presentation-shared 341 pass、typecheck 通过。
- 2026-08-18（自动滚动跟随修复，用户点名期望「贴底才跟随」）：定位到滚动管理整体错挂——真正的滚动容器是父级 `.timeline-scroll`（`overflow-y: auto`），`.timeline` 是不滚动的内容层，但 `onScroll` 与 `scrollTop` 都挂在 `.timeline` 上：React onScroll 不冒泡父级滚动所以 near-bottom 判定从未更新，scrollTop 赋值对非滚动元素是 no-op 所以跟随从未真正生效。修复：`resolveScroller()`（`closest(".timeline-scroll")`，独立渲染回退自身），滚动监听改为滚动容器上的原生 passive listener，滚到底/回到底部按钮/scrollRequest 全部改作用于滚动容器。TDD 先红后绿（timeline +1 条全链路断言：贴底跟随→上滚保持位置并出现回到底部按钮→点击恢复跟随）。`Web界面.md` 滚动描述同步。验证：presentation+application+presentation-shared 342 pass、typecheck 通过。
- 2026-08-18（「有新输出」按钮语义修正为双态，用户反馈后确认方案）：原按钮可见性是纯位置判定（离底 >48px 即显示）但文案恒为「有新输出」，用户上滚回看历史时被谎称有新内容。改为双态（与用户确认）：手动上滚（无新内容）→ 中性「回到底部」（text-soft 文字 + line-strong 描边）；上滚期间 timeline 有新内容 → 升级 accent 强调「有新输出」（`data-new="true"`，accent-hover 文字 + accent-border-strong 描边）；回到贴底两态都消失。新增 `hasNewOutput` 状态：timeline 变化且非贴底时置位，贴底（滚回/按钮/scrollRequest）时清除。TDD 先红后绿（timeline 滚动测试改双态断言 + styles 契约 +1）。验证：presentation+application+presentation-shared 343 pass、typecheck 通过。
- 2026-08-18（Composer 停止按钮按用户设计稿改版）：原 lucide 描边 Square + 5px 圆角被用户点名「有点丑」，按设计稿改为——圆角升至 `--radius-surface`（7px，设计稿大圆角在 36px 按钮上的 token 内最近似），图标改为内联 SVG **实心圆角方块**（`<rect rx="2.5" fill="currentColor">`，15px ≈ 设计稿 43% 占比），描边/文字仍走 danger token、hover 8% 红底与中断语义不变；设计稿的柔和投影违反「阴影只给 overlay/menu/dialog」纪律未引入。发送按钮保持 5px 绿填充不变。TDD 先红后绿（composer +1 图标断言、styles 契约 +1）；静态复现页加发送/取消对照区 1440 宽 light/dark 截图实测。验证：presentation+application+presentation-shared 345 pass、typecheck 通过。
- 2026-08-18（停止按钮比例与颜色修正，用户实测纠正）：① 比例——上一版图标可见方块只有 6px（SVG 盒 15px × rect 10/24 viewBox，放大截图误判了比例）；修正为 rect 14×14 rx 3.5 + 盒 24px，getBoundingClientRect 实测可见 **14px / 34px 按钮 = 0.41**（设计稿 0.43），不再目测验收。② 颜色——`--danger`（light `#a43733`）是文字对比度取向的深红，设计稿为鲜红；新增 `--danger-vivid` token（light `#e5484d` / dark `#f07068`），只给停止按钮等图形化中断动作（文字继续用 --danger 保对比度），描边/图标/hover 底色全部切到 vivid。旧「danger 描边」契约相应修订。验证：presentation+application+presentation-shared 345 pass、typecheck 通过；复现页实测两主题图标/描边均为 `#e5484d`（dark `#f07068`）。

- 2026-08-19（Web Header 品牌对齐，用户截图反馈）：确认 `.brand` 已用 flex 几何居中，偏差来自 22px Harness SVG 曲线的可见重心低于标题文字；只对 `.brand-mark` 增加 `translateY(-1px)` 光学校正，不改顶栏高度、标题字号或在线状态布局。CSS 契约测试先红后绿；`styles.test.ts` 47 pass。真实页面复核未完成：当前 sandbox 禁止预览服务绑定 loopback，且浏览器安全策略拒绝 `file://` 复现页。
- 2026-08-19（Agent 正文与 Composer 宽度对齐，用户截图反馈）：外层 `.timeline-scroll` 与 `.composer-inner` 本已共用同一 `--conversation-content-width`，实际短一截是 Markdown 段落、列表项和引用又被 `max-inline-size: 72ch` 二次限宽。删除该阅读宽度上限，让 Agent 纯文本使用完整消息内容列；代码块/表格局部滚动和用户气泡 `fit-content` 保持不变。CSS 契约测试先红后绿；`styles.test.ts` 47 pass。

## 2026-08-09 执行证据

- 可观察变化：Run 状态成为 Topbar 最高状态层级；新建 Thread 使用 secondary chrome；当前 Thread 使用蓝色轻背景和左侧轨道；Context Dock 的标题、tab 与关闭入口收敛到同一层 header；light/dark 使用 HC-124 暖中性色与稀缺蓝色 token。
- 保持不变：未修改 Protocol、Python AgentHost、RunCoordinator、Handoff/ControlLease、InteractiveSnapshot、Web intent 或 HC-118 Timeline/reasoning/progress 语义。
- 对比度复核：必要的 light/dark 文本、状态文字、按钮和 focus 颜色均达到 WCAG AA；subtle 仅用于 disabled/decorative 场景。
- 版本影响：源码开发阶段不修改根 `VERSION`、包版本或 `CHANGELOG.md`；当前无提交、无 PR reference。
