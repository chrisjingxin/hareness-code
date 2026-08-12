# HC-106：蓝色 Web 工作台与显式深浅主题

原始需求：[HC-106：按蓝色工作台设计整改 Web UI 并支持显式深浅主题](../task/HC-106-按蓝色工作台设计整改WebUI.md)

前置方案：[HC-104：Web Interactive Adapter 与 React 工作台](../task/HC-104-实现WebInteractiv.md)

后续验收：[HC-105：真实 Browser E2E](../task/HC-105-建立WebBrowserE2E.md)

## 通俗说明

`/web` 已经具备完整交互能力，但当前页面仍是暖灰/琥珀色的通用工作台，与用户提供的蓝色深浅设计稿和 TUI 的开发工具气质差异明显。本任务只更换 Web 的信息层级、组件外观和主题选择方式，不改变 Agent 如何运行，也不改变 TUI/Web 接管流程。

目标流程是：

```text
每次打开 /web
  → 固定以浅色 token 首次渲染
  → 用户可在页面菜单中切换深色或浅色
  → Web Adapter 保存本次页面的主题与菜单状态
  → React 继续用同一份 InteractiveSnapshot 渲染全部业务数据
  → 关闭或重新接管后重新从浅色开始
```

桌面布局采用三栏工作台：左侧 Thread，中间 Timeline 与 Composer，右侧按需打开 Model/Skills/MCP/Status。移动端只显示中间工作区，其他区域改为抽屉。视觉以用户设计稿为准，但不会复制稿件中的静态示例数据、虚构时间、无功能附件按钮或已确认存在裁切的移动端 CSS。

## 设计依据与覆盖关系

本方案根据以下事实制定：

- 用户提供的 `harness-web-ui-light.html` 与 `harness-web-ui-dark.html` 结构完全一致，仅颜色 token 不同；它们确认了页面信息架构和两套主题颜色。
- 1440×900 实际渲染确认三栏层级、54px 顶栏、260px Thread 栏、约 372px 工具栏和约 880px 阅读列成立。
- 390×844 实际渲染出现顶栏、Interaction 与 Composer 横向裁切，因此移动端只采用视觉意图，不直接复制稿件的响应式实现。
- 当前 `InteractiveSnapshot` 的 Message、Tool 与 Interaction 没有时间戳；实现不得为了还原稿件而显示虚构时间。
- 当前 Web 不支持附件上传；稿件中的回形针只能视为视觉占位，不得加入死按钮。

冲突时按以下顺序处理：

1. HC-104 的业务、安全和依赖 invariant；
2. 本方案确认的主题、布局、组件与响应式决策；
3. 用户 HTML 设计稿的视觉参考；
4. 当前 CSS 的历史样式。

本方案只覆盖 HC-104 的“页面信息架构与视觉规范”中暖灰/琥珀、跟随系统主题和旧布局的部分。HC-104 的 Controller/Adapter 分层、typed intent、Handoff、Markdown 安全、Interaction、capability 和关闭顺序继续有效。

## 已确认现状

### 状态与依赖

- `web/application/adapter.ts` 已集中保存 draft、面板、抽屉、Tool 展开、Interaction 草稿、通知和滚动请求；主题属于同一类 Web 表现状态。
- `web/presentation/web-app.tsx` 只订阅 `WebAdapterSnapshot` 并派发 `WebIntent`，适合作为主题属性和页面结构的唯一装配位置。
- `web/presentation/styles.css` 使用 `prefers-color-scheme: dark` 自动切换主题，不能满足“默认浅色且由用户显式切换”。
- `styles.css` 前半段仍保留 `thread-sidebar/message-item/tool-item/composer-wrap` 等早期规则，后半段才定义当前组件实际使用的 `sidebar/message-bubble/tool-card/composer-bar`；视觉正确性依赖覆盖顺序。
- `UtilityPanels` 已有 Model、Skills、MCP、Status、Help 内容和完整 intent，只缺少统一工作台外壳与主 tab。
- 当前窄屏断点在 React 与 CSS 中均为 `max-width: 899px`，应继续保持单一边界。

### TUI 可复用的视觉语言

TUI 的可复用部分是“深色开发工具画布 + 蓝色主强调 + 扁平时间线 + 左轨 Composer/Tool/Interaction”，不是逐像素复刻终端：

- Assistant 内容直接位于主阅读流，不包大气泡。
- User、Tool 和 Interaction 以左侧色轨和轻量 surface 建立层级。
- 运行、成功、警告和失败使用稳定语义色。
- 组件以小圆角、细分隔线和紧凑排版为主。

Web 仍保留用户设计稿确认的浅色主题、Thread 导航、工具工作台、鼠标 hover、DOM 表单和移动端抽屉。

## 任务拆分决策

整改保留为一个 HC-106 实施任务，不再拆成“主题”“布局”“组件”三个可并行任务，原因是这些工作都会同时修改 `web-app.tsx`、`adapter.ts`、`styles.css` 和同一批 presentation tests；并行认领会产生高冲突，并且任一子任务单独交付都会形成半新半旧的不可验收页面。

任务内部按以下依赖顺序推进：

```text
主题状态与 token
  → 页面骨架与顶栏
  → Timeline / Tool / Interaction / Composer
  → Utility workspace 与移动端抽屉
  → focused tests 与真实浏览器抽查
  → HC-105 自动化视觉基线
```

HC-105 仍是独立任务，因为它引入 Playwright、真实 Host fixture 和跨进程 lifecycle 测试，不应与 presentation 改造混在同一个 diff 中。

## 关键 invariant

- 主题切换是纯 Web 表现动作：不得调用 `InteractiveController`、Agent RPC、Handoff port、Protocol 或持久化接口。
- 每个新 Web Adapter 实例的初始主题固定为 `light`，不读取 `prefers-color-scheme`、TOML、SQLite、localStorage 或之前的接管页面。
- React 组件继续只消费 `WebAdapterSnapshot` 并派发 `WebIntent`；组件不得直接修改 `document.documentElement` 或保存第二份主题状态。
- `InteractiveSnapshot` 是 Thread、Run、Timeline、Interaction、catalog、模型和权限的唯一业务事实来源。
- 不因视觉整改新增虚构数据。没有时间戳就不显示时间，没有附件能力就不显示附件入口，没有 duration 就不显示 Tool duration。
- active Run、pending Interaction、capability、连接只读和 Handoff leaving 的禁用语义不得由 CSS 或组件文案重新推断。
- 宽窄屏只改变排布，不创建第二份业务组件或业务状态；相同组件在 sidebar/drawer 中复用同一 catalog 与 intent。
- 用户输入、Tool 参数与输出、Markdown、路径和 MCP 错误继续作为不可信文本处理，不使用 `dangerouslySetInnerHTML`。
- 页面不得出现横向根滚动；需要保留原始格式的代码、表格和 Tool 输出只能在自己的局部容器滚动。

## Web Adapter 公开 interface

### 新增表现类型

```ts
export type WebTheme = "light" | "dark"

export type WebAdapterSnapshot = {
  // 现有字段保持不变
  readonly theme: WebTheme
  readonly headerMenuOpen: boolean
}

export type WebIntent =
  | /* 现有 intent */
  | { type: "theme-set"; theme: WebTheme }
  | { type: "header-menu-toggle"; open: boolean }
```

选择 `theme-set` 而不是无参数 `theme-toggle`，使 DOM 测试和重复点击具备确定结果，也允许菜单直接表达“使用浅色/使用深色”。Adapter 负责去重：设置为当前主题时不重复发布。

### 初始值与关闭规则

- `theme = "light"`。
- `headerMenuOpen = false`。
- 选择主题、打开 Help、开始返回 TUI 或退出 Harness 时先关闭 header menu。
- `close()` 后主题 intent 与菜单 intent和其他 intent 一样安全 no-op。
- 主题变化调用现有表现发布路径，不调用 frame 外的 DOM API。

### Escape 与焦点优先级

Escape 的关闭顺序固定为：

```text
确认 Dialog
  → Command menu
  → Header overflow menu
  → Utility drawer/panel
  → Thread drawer
  → active Run cancel
```

打开 header menu 后焦点进入第一项；关闭后焦点返回 overflow trigger。点击菜单外、选择菜单项、开始 leaving 或连接进入只读时均关闭菜单。移动端抽屉打开后必须限制焦点在抽屉内，并在关闭后返回触发按钮。

## 主题应用方式

`WebApp` 根节点是唯一主题边界：

```tsx
<div
  className="web-shell"
  data-theme={snapshot.theme}
  data-active={props.active ? "true" : "false"}
>
```

CSS 结构固定为：

```css
:root {
  color-scheme: light;
  /* 页面挂载前和 fatal/static view 的浅色安全默认 */
}

.web-shell[data-theme="light"] { /* light token */ }
.web-shell[data-theme="dark"] {
  color-scheme: dark;
  /* dark token */
}
```

删除 `@media (prefers-color-scheme: dark)` 的颜色覆盖。`prefers-reduced-motion` 继续保留，因为它控制动效可访问性，与主题选择无关。

主题属性放在 `.web-shell` 而不是 `html`，避免 React effect 异步修改全局节点产生闪烁或卸载残留。`.web-shell` 覆盖完整 `100dvh`，因此 body 的浅色默认不会在深色页面露出。

## Design token

### 基础色板

以下值来自用户设计稿，是本任务的 canonical palette：

| Token | Light | Dark | 用途 |
| --- | --- | --- | --- |
| `--bg` | `#f5f7fb` | `#07111f` | Timeline 主画布 |
| `--surface` | `#ffffff` | `#0d1826` | 输入、卡片、控件 |
| `--surface-2` | `#f7faff` | `#111f30` | hover/次级区域 |
| `--surface-3` | `#edf3fa` | `#18283b` | code、disabled、pressed |
| `--line` | `#dce4ef` | `#22334a` | 常规分隔线 |
| `--line-strong` | `#c2cfdf` | `#354b68` | 输入、重要边界 |
| `--text` | `#172033` | `#f2f6fb` | 主文字 |
| `--text-soft` | `#344054` | `#cbd6e4` | 正文、次标题 |
| `--muted` | `#667085` | `#91a0b4` | 辅助说明 |
| `--subtle` | `#98a2b3` | `#62728a` | 非文字装饰、disabled |
| `--accent` | `#1677ff` | `#2583ff` | 选中边界、状态点、强调填充 |
| `--accent-strong` | `#0b65d8` | `#58a6ff` | 可点击文字、focus、图标 |
| `--accent-soft` | `rgba(22,119,255,.08)` | `rgba(37,131,255,.12)` | 选中背景 |
| `--accent-soft-2` | `rgba(22,119,255,.16)` | `rgba(37,131,255,.22)` | pressed/toggle 背景 |
| `--accent-border` | `rgba(22,119,255,.26)` | `rgba(37,131,255,.34)` | 普通蓝色边界 |
| `--accent-border-strong` | `rgba(22,119,255,.52)` | `rgba(37,131,255,.62)` | Interaction/focus 边界 |
| `--success` | `#16a05d` | `#3fbf73` | 成功点、边界 |
| `--warning` | `#c47b16` | `#dca84b` | 警告点、边界 |
| `--danger` | `#d84654` | `#f26d78` | 失败点、边界 |
| `--chrome` | `#ffffff` | `#091421` | Brand、Topbar、Sidebar |
| `--tool-output-bg` | `#f7f9fc` | `#07111d` | Tool 原始输出 |
| `--tool-output-text` | `#475467` | `#b6c4d6` | Tool 输出文字 |
| `--interaction-bg` | `#ffffff` | `#0b1725` | 当前 Interaction |
| `--command-bg` | `#f6f8fb` | `#07111c` | 命令/审批预览 |
| `--command-text` | `#344054` | `#c8d5e5` | 命令预览文字 |
| `--composer-bg` | `rgba(245,247,251,.97)` | `rgba(7,17,31,.98)` | Composer 外层 |
| `--drawer-bg` | `#ffffff` | `#091421` | Utility workspace |
| `--inline-code-text` | `#0b63ce` | `#8fc7ff` | 行内代码 |
| `--code-key` | `#1677ff` | `#58a6ff` | 代码 key（如有结构化着色） |
| `--code-string` | `#087d62` | `#9bd5ff` | 代码 string（如有结构化着色） |

### 可访问性别名

基础色板保留设计稿外观，但不能把所有色值直接用于小号文字：浅色 `--subtle` 对背景对比不足，白字直接放在两套 `--accent` 上也不能稳定达到普通文字 4.5:1。实现必须增加角色别名，而不是随处硬编码颜色：

| 角色 | Light | Dark | 规则 |
| --- | --- | --- | --- |
| `--action-bg` | `#0b65d8` | `#2583ff` | 主按钮背景 |
| `--action-text` | `#ffffff` | `#07111f` | 主按钮文字 |
| `--link-text` | `#0b65d8` | `#58a6ff` | 链接与可点击蓝色小字 |
| `--success-text` | `#087d62` | `#3fbf73` | 成功文字 |
| `--warning-text` | `#925b0a` | `#dca84b` | 警告文字 |
| `--danger-text` | `#b42318` | `#f26d78` | 错误文字 |

- `--subtle` 只用于分隔、装饰、placeholder 或 disabled；必要信息与 10–12px metadata 至少使用 `--muted`。
- 彩色点、图标和边界可以使用基础 `success/warning/danger`；携带语义的文字使用对应 `*-text`。
- focus ring 使用 `--accent-strong`，保持 2px outline + 2px offset。
- 实施时必须用真实浏览器或对比度工具复核；不得仅因颜色来自设计稿就假定可访问。

### 字体、尺寸与动效

- UI：`"Avenir Next", "Segoe UI Variable", "PingFang SC", "Microsoft YaHei", sans-serif`。
- Mono：`"SFMono-Regular", "Cascadia Code", "JetBrains Mono", Consolas, monospace`。
- 根字号 14px；正文 13–14px；辅助文字不得小于 11px；仅非必要的计数/标签可使用 10px。
- 圆角：内控件 4px，按钮/行项目 5px，Composer/Interaction/Dialog 6px；不引入大圆角卡片。
- 阴影只用于浮层、抽屉和命令菜单；静态层级主要靠分隔线与 surface。
- hover/focus/drawer 使用 120–180ms 的 `color/background/border/transform/opacity` 过渡；pressed 使用轻微 `translateY(1px)`，不动画宽高。
- `prefers-reduced-motion: reduce` 下取消非必要 transition、spinner 之外的循环动画和 smooth scroll。

## 桌面页面结构（1440×900）

```text
┌──────── 260px ────────┬──────────── 弹性中央区 ────────────┬── 372px 可选 ──┐
│ H  Harness Code  vX   │ project / branch   meta chips     │                │ 54px
├───────────────────────┼────────────────────────────────────┼────────────────┤
│ + 新建 Thread         │ THREAD · N 项                      │ 工作台     ×   │
│ 搜索                  │                                    │ Model Skills…  │
│ 最近 Thread           │ User / Assistant                  │                │
│                       │ Tool / Interaction                 │ 当前 panel     │
│                       │                                    │                │
│ 本地工作区 · 连接状态 ├────────────────────────────────────┤ capability     │
│                       │ Composer / Skill / Send-or-cancel │ summary        │
└───────────────────────┴────────────────────────────────────┴────────────────┘
```

### 根布局

- 顶栏固定 54px；主区域使用 `minmax(0, 1fr)`，页面本身不滚动。
- Thread 栏固定 260px。
- Utility workspace 打开时固定 372px；关闭时不保留空列，中央区自然扩展。
- 中央 Timeline 与 Composer 内容宽度 `min(880px, calc(100% - 48px))`。
- 主区域、Thread list、Timeline、Utility body 各自拥有独立的 `min-height: 0` 与滚动边界。

### Brand 与 Topbar

Brand 区：

- 25×25 的 `H` brand mark、`Harness Code`、真实 `runtime.cliVersion`。
- 与 Thread 栏同宽，并通过右/下分隔线连接主网格。

主 Topbar：

- 左侧显示 `workspaceLabel(runtime.workspace)` 与真实 `gitBranch`；无分支时不显示占位 slash。
- 右侧依次显示当前模型 chip、审批模式 chip、活动/连接状态 chip、返回 TUI、overflow。
- Model chip 可打开 Model panel；状态 chip 可打开 Status panel。审批模式在本任务中只展示，不新增点击行为；审批切换仍走已有共享命令。
- `返回 TUI` 的禁用条件与现有实现完全一致。
- overflow menu 包含“使用深色/使用浅色”“帮助”“退出 Harness”；移动端额外承载“返回 TUI”。
- 所有 chip 必须允许文本截断，Topbar 不因长模型名或长 workspace 产生水平滚动。

## Thread 导航

- 顶部是全宽蓝色“新建 Thread”主按钮，其 busy/readonly 状态继续由现有逻辑决定。
- 搜索框紧随按钮；刷新使用有可访问名称的紧凑 icon button，不与搜索输入重叠。
- 列表按当前服务端顺序展示，不在本任务新增日期分组业务。区段标签使用“最近 Thread”，避免把旧记录错误标成 Today。
- 每项显示首条/最新消息生成的标题、最新摘要、相对更新时间和消息数；不显示 `thread_id`。
- 当前项使用 `--accent-soft` + `--accent-border`；hover 只使用 `--surface-2`，disabled 不可点击且不只依靠 opacity 传达原因。
- footer 只显示本地工作区与连接状态；真实路径仍只在 Status panel 中显示。

## Timeline

### 顶部摘要

- 有 Thread 时显示 `THREAD · N 项记录`。`N` 来自当前 `timeline.length`，不把 Tool/Interaction 错称为消息。
- 空首页不显示伪造的 Thread header，继续使用可直接输入的空状态。
- 当前 DTO 没有时间戳，因此不显示稿件中的“今天 19:11”和逐条时间。后续若 Protocol 提供可信时间，应另立任务统一 TUI/Web 历史语义。

### Message

- User 与 Assistant 均使用左对齐阅读流和 27px 圆角矩形 avatar，显示“你”或“Harness”角色标签。
- User avatar/左轨使用蓝色，正文 surface 可使用轻微 `--accent-soft`；Assistant 正文直接位于画布，不包大卡片。
- System 使用单行 muted notice，不显示 avatar 大块。
- Assistant Markdown 继续复用安全 renderer；正文行高约 1.7，段落宽度受 880px 阅读列约束。
- 不显示虚构时间、模型名或 run ID。

### Tool

- Tool 相对消息正文左缩进 43px；移动端缩进收敛到 35px 或更小。
- 折叠头至少显示真实 Tool 名称和 running/completed/failed；参数摘要只有在现有 `arguments` 可安全提取为单行文本时显示。
- 不显示稿件中的虚构 path、duration 或完成时间。
- running、completed、failed 分别使用 accent、success、danger；不能只用颜色，必须同时有 icon/文字状态。
- 展开区使用 `--tool-output-bg`、mono 11–12px、最大高度和局部滚动；长 token 可换行，保留格式的代码允许横向局部滚动。

### Interaction

- 当前待处理 Interaction 固定在 Composer 上方，视觉上与 Timeline 末尾对齐；历史 Interaction 留在原事件位置。
- 使用 `--accent-border-strong`、`--interaction-bg` 和 3px 内侧蓝轨建立优先级。
- Approval 的 request 预览放入 `--command-bg` mono 区域；Question 继续一次展示全部问题。
- 桌面 actions 右对齐；390px 下允许换行或等宽两列，任何按钮不得超出 viewport。
- positive/negative 状态同时使用图标、文字和语义色；pending、submitting、expired、stale、disabled 均需可见。

## Composer

- Composer 固定在中央列底部，外层使用 `--composer-bg` 和顶部分隔线，内部最大宽度 880px。
- 输入主体与底部 action rail 分层：textarea 在上，Skill、键盘提示和 send/cancel 在下。
- textarea 自动增长 1–8 行；不允许用户 resize 破坏固定布局，超过上限后内部滚动。
- armed Skill 以紧凑 chip 展示并可清除；没有 Skill 时不保留空位。
- idle 时发送按钮使用 action token；active Run 时同一位置改为取消按钮，不同时展示两个主动作。
- 命令菜单锚定 Composer 上方，使用同一 880px 边界；disabled reason 来自共享 Core。
- 不显示附件按钮。若提供显式命令按钮，它只能派发现有 `command-menu-open` 语义并有可访问名称，不能成为静态图标。
- Enter/Shift+Enter、IME composing、Escape 与 Cmd/Ctrl+K 行为保持现有测试语义。

## Utility workspace

### 外壳与 tab

- 桌面右栏标题固定为“工作台”，显示关闭按钮。
- 主 tab 固定为 Model、Skills、MCP、Status；只有 capability 允许的 tab 可见，不显示后再由内容报错。
- tab 点击仍派发 `panel-open`，Adapter 继续负责 catalog refresh；不得在 panel 中直接请求 RPC。
- 当前 tab 使用 2px 蓝色底边，tablist 使用 `role="tablist"`，tab 使用 `aria-selected`/`aria-controls`，panel 使用 `role="tabpanel"`。
- `Help` 不挤入四个主 tab。从 overflow 打开 Help 时复用同一 372px drawer，标题改为“帮助”、隐藏主 tab，关闭后回到主工作区；不保留第二个并列 drawer。

### Panel 内容

- Model：Profile 名、provider/model、availability、当前选择；选择态用左蓝轨。
- Skills：搜索、说明、name/description/source/armed、启停开关；开关必须使用原生 checkbox 语义或等价 `role="switch"` + `aria-checked`。
- MCP：连接状态、工具数、脱敏错误、添加/删除；表单字段与管理按钮继续受 capability/busy 控制。
- Status：Web 接管、Agent connection、当前活动、模型、审批模式、执行模式、版本和脱敏 workspace；不显示 token、attachment ID、原始 frame。
- 所有 panel 都必须覆盖 loading、ready、empty、error、disabled 和 submitting，错误只影响当前 panel。

## Header overflow menu

- trigger 使用设计稿的 ellipsis icon，具备 `aria-haspopup="menu"`、`aria-expanded` 和可访问名称。
- menu 使用 `role="menu"`，每项使用 `menuitem`；主题项文案显示下一动作，例如当前浅色时为“使用深色主题”。
- 当前主题同时通过菜单文案和选中标记可见，不只显示太阳/月亮图标。
- 桌面保留直接“返回 TUI”，移动端将返回动作移入 menu，避免顶栏裁切。
- active Run/Interaction 时移动端返回项保持可见但 disabled 并显示与桌面一致的 reason。
- 点击“退出 Harness”仍走现有 `exit-harness` intent；menu 不直接关闭窗口或进程。

## 移动端（390×844）

断点继续使用 `max-width: 899px`，React `matchMedia` 与 CSS 必须共享同一常量/注释，禁止一个使用 899、另一个使用 900。

### 顶栏

- 高度 50px；只显示 Thread menu、截断后的 project、模型短标签和 overflow。
- 隐藏 branch、审批 chip、详细连接 chip和直接返回按钮；这些内容仍可从 Status/overflow 访问。
- 任意 390px 长模型和 workspace fixture 下均不得产生根横向滚动。

### 抽屉

- Thread 从左侧进入，Utility 从右侧进入；宽度 `min(336px, calc(100vw - 32px))`。
- 两者互斥：打开一个时关闭另一个。
- 抽屉带 scrim，点击 scrim 或 Escape 关闭；scroll lock 只作用于页面根，抽屉 body 自己可滚动。
- 使用 `role="dialog" aria-modal="true"`，打开后 focus trap，关闭后恢复触发器焦点。
- safe area 通过 `env(safe-area-inset-top/bottom)` 进入 header/footer padding。

### Timeline 与 Composer

- Timeline 横向 padding 14px；Message avatar 24px；Tool/Interaction 最大左缩进 35px。
- Interaction action 可换行，单个主要触控目标不小于 44×44px。
- Composer 左右 10px，底部叠加 `safe-area-inset-bottom`；textarea、Skill chip、send/cancel 必须在 370px 可用宽度内收敛。
- 键盘提示可隐藏，但发送/取消和 pending Interaction 主动作不能隐藏。
- code/table/tool output 使用局部滚动或断行，不允许撑大 `.web-shell`。

## 状态矩阵

| 状态 | Topbar | Timeline | Composer | Sidebar/Utility |
| --- | --- | --- | --- | --- |
| handoff opening | “正在接管” | 已恢复内容可读 | disabled | 可读，不可操作 |
| idle/home | 已接管/连接正常 | 空状态或历史 | 可输入发送 | 正常操作 |
| starting/running | 运行 chip | 流式内容/Tool running | 发送改为取消 | busy 项显示 reason |
| waiting interaction | 等待审批/回答 | 历史保留 | Interaction 固定在上方 | Thread 切换/返回禁用 |
| cancelling | 正在取消 | 保留当前输出 | 取消按钮 submitting | 禁止重复动作 |
| completed | 成功状态 | Run summary | 恢复发送 | 正常操作 |
| failed | 失败状态 | 错误在 Timeline | 可继续输入 | catalog 不被全局清空 |
| connection closed/error | 错误 banner | 历史只读 | disabled | 只读 |
| leaving | 归还/退出中 | 保留 | disabled | 全部关闭/disabled |

浅色和深色必须使用同一状态结构和同一文案，仅 token 不同。

## CSS 清理边界

`styles.css` 最终按以下顺序组织：

```text
文件说明
→ light/dark token 与 reset
→ shell / brand / topbar / banners
→ sidebar / workspace / utility
→ timeline / message / tool / interaction
→ composer / command menu
→ panel / dialog / static states
→ interaction states（hover/focus/disabled/pressed）
→ reduced motion
→ max-width: 899px
```

删除未被当前 TSX 使用的早期 class，不保留 alias 或“旧 class + 新 class”双轨。实现线程应先通过 `rg` 建立 TSX class 清单，再删除规则；不能为了清理而改动 Markdown token renderer 或无关测试命名。

CSS 不使用：

- `!important` 修复普通优先级；
- arbitrary `z-index: 9999`；
- 远端字体、图片、渐变装饰或纹理；
- 依赖 `:has()` 才能完成的主流程；
- 会触发布局重排的 width/height 动画；
- 系统主题媒体查询覆盖用户显式选择。

## 错误与降级

- 主题状态无错误分支；非法主题值不能进入 typed intent。若 DOM 被外部修改为未知值，CSS 浅色默认仍保证页面可读。
- header menu 或抽屉关闭不应取消 Run、清空 draft 或影响 Interaction 草稿。
- catalog 错误继续局部显示；视觉整改不得把某个 panel 的错误升级为全页 fatal view。
- presentation Error Boundary、Agent connection、lifecycle shutdown 和 bootstrap fatal view 继续使用 HC-104 的脱敏规则。
- CSS `color-mix()` 不是必要条件；所有关键颜色使用明确 token，避免旧浏览器因不支持 color-mix 丢失状态。
- Lucide 图标加载失败时文字标签仍表达关键动作；icon-only 控件必须有 `aria-label`/`title`。

## 测试方案

### Web Adapter focused tests

- 初始 snapshot 的 `theme` 固定为 `light`，模拟深色 `matchMedia` 也不改变。
- `theme-set dark/light` 更新 snapshot 并发布一次；重复设置当前值不重复发布。
- theme intent 不调用 mock Controller、Handoff port 或任何 catalog intent。
- header menu open/close、选择主题、Help、return、exit 和 leaving 的关闭规则可观察。
- `close()` 后 theme/header intent no-op。

### Presentation DOM tests

- `.web-shell` 正确设置 `data-theme="light|dark"`，不存在由系统主题决定的 class。
- Brand 使用真实版本；Topbar 使用真实 workspace、branch、model、approval 和状态数据，长文本节点具备截断 class。
- overflow trigger 的 menu/expanded/focus/Escape 语义正确；主题菜单文案随当前主题变化。
- Utility 主 tab 的 `role/aria-selected/aria-controls` 和 `panel-open` intent 正确；Help 模式不渲染错误的选中 tab。
- Message 不渲染虚构时间；Tool 不渲染不存在的 path/duration；Composer 不渲染死附件按钮。
- active Run/Interaction/readonly/capability 的禁用行为与原有测试保持一致。
- CSS contract test 确认不存在颜色型 `prefers-color-scheme: dark`，light/dark/action token 均存在，重复历史 class 已删除。

### HC-106 浏览器抽查

实现任务交付前使用本地构建和真实 Chrome 至少检查：

| Viewport | Theme | 必查内容 |
| --- | --- | --- |
| 1440×900 | light | 三栏、工作台 tab、Interaction、Composer |
| 1440×900 | dark | token、Tool 输出、focus、状态色 |
| 390×844 | light | 两个抽屉、Topbar、软键盘前布局、操作换行 |
| 390×844 | dark | 长文本、safe area、Composer、无根横向滚动 |

抽查截图只作为 HC-106 实施证据，不在本任务引入 Playwright 或提交最终基线。HC-105 使用真实 Host fixture 对同一四组场景建立可重复自动化截图、几何和键盘断言。

### 项目级验证

```bash
cd packages/cli && bun test tests/web/application/adapter.test.ts tests/web/presentation
bun run build
bun run typecheck
bun run test
bun run project:check
```

若仓库当时仍存在与任务无关的基线失败，必须按仓库规范记录命令、失败项和基线复验，不能把失败写成通过。

## 按依赖排序的实施步骤

1. **建立真实 class/token 清单**：读取当前 TSX 与测试，标记正在使用和历史遗留 class；验证方式是 `rg` 清单与 focused tests，不改业务代码。
2. **增加主题与 header menu 状态**：扩展 `WebAdapterSnapshot/WebIntent`，实现 light 初始值、显式 set、menu 生命周期；用纯 Adapter tests 证明无 RPC/Handoff effect。
3. **重建 CSS token 层**：落地两套基础色板和可访问性别名，删除系统主题覆盖；用 CSS contract 和对比度检查验证。
4. **整改 WebApp 骨架**：实现 Brand、54px Topbar、meta chip、return 与 overflow，保持真实 runtime/capability/busy 数据；用 DOM tests 验证。
5. **整改主要工作区**：依次处理 Thread、Timeline Message、Tool、Interaction、Composer；每一类都覆盖 running/empty/error/readonly 与长文本。
6. **整理 Utility workspace**：增加四个主 tab 和 Help 模式，复用既有 panel 内容与 typed intent，不修改共享 Core。
7. **收敛移动端**：实现互斥抽屉、scrim、focus、safe area 和 390px 几何；复用桌面组件，不创建移动端业务副本。
8. **删除历史 CSS 双轨**：只在所有 canonical class 覆盖完成后删除旧规则，运行 focused tests 防止误删。
9. **补文档和实施证据**：更新 `/web` 主题说明，记录四组 Chrome 抽查、测试、OCR 范围与结果；通过后交给 HC-105。

## 可观察验收

- 新 `/web` 页面不论操作系统主题都先显示浅色；菜单可切换到深色并立即切回。
- 主题切换期间当前 Thread、draft、滚动位置、Tool 展开、Interaction 草稿和 panel 保持不变。
- 桌面三栏尺寸、蓝色层级和组件密度与设计稿一致；关闭 Utility 后中央区扩展，无遗留空白列。
- 390×844 下无根横向滚动，两个抽屉、Interaction 和发送/取消动作可通过键盘与触控到达。
- 页面不显示虚构时间、Tool duration/path、静态模型/Thread 或无功能附件按钮。
- 所有业务动作仍走 HC-104 的 Adapter/Core，Handoff 与安全测试不因视觉整改退化。
- light/dark、hover/focus/pressed/disabled、loading/empty/error、running/completed/failed 均具备清晰且不只依赖颜色的表现。

## 非范围

- 不持久化主题，不增加“跟随系统”第三种模式。
- 不改变 TUI 主题，也不抽取 TUI/Web 跨渲染器共享 CSS token。
- 不为 Message/Tool 新增时间戳、duration 或 path Protocol 字段。
- 不新增附件、上传、拖放、远端图片或语法高亮依赖。
- 不改变 Model、Skill、MCP、approval mode 或 Slash Command 的业务语义。
- 不建立 Browser E2E fixture 或最终截图基线；由 HC-105 完成。
