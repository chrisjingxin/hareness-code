# HC-107：Web Composer、离线代码高亮与界面精修

原始需求：[HC-107：修复 Web Composer、补齐代码高亮并精修设计一致性](../task/HC-107-修复WebComposer、补.md)

前置方案：[HC-104：Web Interactive Adapter 与 React 工作台](../task/HC-104-实现WebInteractiv.md)、[HC-106：蓝色 Web 工作台与显式深浅主题](../task/HC-106-按蓝色工作台设计整改WebUI.md)

后续验收：[HC-105：建立 Web Browser E2E 与完成验收闭环](../task/HC-105-建立WebBrowserE2E.md)

## 通俗说明

HC-106 已经把 Web 页面的大框架和颜色改到正确方向，但当前页面仍有三个会直接影响使用体验的问题：输入框在真实浏览器里不能工作，代码块没有像 TUI 一样按语法着色，Timeline 和控件细节没有还原设计稿的紧凑层级。

本任务的处理顺序固定为：

```text
先让用户能输入和发送
  → 再让代码块安全、离线地高亮
  → 最后统一角色、间距、行长和按钮尺寸
  → 在最终页面上做真实浏览器与响应式验收
```

它不会重做 Web 架构。Thread、Run、Interaction、Model、Skill、MCP 和 Handoff 仍由 HC-104 的共享 `InteractiveController` 与 Web Adapter 驱动；HC-106 的显式 light/dark 主题、三栏骨架和移动端抽屉继续保留。

## 设计依据与证据边界

本方案比较了以下材料：

- 用户提供的 `harness-web-ui-light.html` 与 `harness-web-ui-dark.html`；两份 HTML 结构相同，颜色 token 不同。
- 用户提供的两张当前实现截图，分别展示连续 Tool 调用和包含 Markdown code/table 的长回答。
- 当前 HC-106 未提交实现：`web/application/adapter.ts`、`web/presentation/*.tsx`、`styles.css` 及对应测试。
- TUI 已有的 Tree-sitter parser、query、syntax theme 与生命周期实现。

本次没有启动浏览器，也没有修改生产代码。以下结论分为两类：

- **已确认事实**：可以从截图和源码直接观察，例如 avatar 溢出、空 Assistant 行、纯色代码块、受控 textarea 的双重 value 写入和测试绕过 DOM 事件。
- **待实施线程复现的根因**：真实浏览器不能输入的最终触发条件。方案已经锁定输入链路和最小修复方向，但实施者仍须先用 Tabbit DevTools/可重复 fixture 记录 focus、`disabled`、`input`、Adapter publish 和 Controller dispatch，不能只凭推断删改代码。

## 现状与设计稿差异

### 已经对齐、应保留的部分

- light/dark 蓝色 token、浅色默认和显式主题切换。
- 260px Thread 栏、54px 顶栏、最大 880px 中央列和可选 372px Utility workspace。
- Topbar 的 model / approval / activity、返回 TUI 和 overflow 入口。
- 固定 Composer、Tool 折叠、Interaction dock、Utility tabs 和 899px 移动端断点。
- 所有业务动作经 `WebIntent → WebInteractiveAdapter → InteractiveController` 的依赖方向。
- Markdown 不使用 `dangerouslySetInnerHTML`，raw HTML、图片和非法 URL 继续惰性化。

### 必须改进的部分

| 优先级 | 位置 | 当前表现 | 目标表现 | 设计依据 |
| --- | --- | --- | --- | --- |
| P0 | `composer.tsx` / `adapter.ts` | 真实页面不能输入或发送；React controlled value 与命令式 `.value` 并存，草稿发布被 rAF 延迟 | React 是 value 唯一 owner；草稿变化立即发布；提交读取 Adapter 当前草稿；失败保留输入 | 表单必须有单一事实来源，核心操作不能依赖一帧后的状态追赶 |
| P0 | Web presentation tests | `changeValue()` 直接调用 React handler，未覆盖原生 DOM/IME/form 链路 | 增加真实 DOM/浏览器输入证据；现有 handler 单测只保留为局部逻辑测试 | 当前测试无法发现用户现场故障 |
| P1 | `markdown.tsx` | fenced code 只有统一文本颜色 | 本地 Tree-sitter scope → React span；未知/失败仍显示纯文本 | 与 TUI 能力一致，同时保持离线和安全 |
| P1 | Assistant message | 27px avatar 内放完整 `Harness`，文字溢出 | avatar 只放 16px 图标或单个 `H`；`Harness` 是正文上方独立 author label | shared edge、光学对齐、图标与文字职责分离 |
| P1 | 连续 Tool Timeline | 空的已结束 Assistant message 仍渲染，Tool 之间出现重复 `Harness` 和大留白 | 空 final Assistant row 不渲染；Tool 连续排列在统一缩进轨上 | 组内间距小、组间间距至少 2 倍，避免伪内容 |
| P1 | Sidebar | Brand 下再次出现 `THREADS / Thread` 两级标题 | 桌面直接从“新建 Thread”开始；抽屉 header 才显示 `Thread` | 入口不过载，首屏空间给核心动作和列表 |
| P1 | User message | 短消息的 accent surface 横跨整个正文列 | surface 随内容收敛，最大 72ch；长消息正常换行 | 蓝色不应成为整行装饰；短内容不制造大色块 |
| P1 | Assistant Markdown | 正文可用完整 880px，截图中长行超过舒适阅读长度 | prose 最大 72ch；code/table/tool 可在正文轨内 full-width | 长文 60–75 字符/行，二维内容独立滚动 |
| P1 | Composer action | 72×34px “发送”按钮过重，和 HTML 稿 icon action 差异大 | 桌面 36×36 icon-only，移动端 44×44；取消复用同一位置与尺寸 | 一处主色 action、稳定几何、可访问名称 |
| P2 | Brand / Topbar | brand mark 实心蓝；chip 使用文字前缀占宽 | brand mark 使用 accent-soft + border；chip 使用 16px 图标 + 值，label 视觉隐藏 | 保留蓝色给当前状态与主动作，降低 chrome 噪声 |
| P2 | 通用控件 | 32/34/36px 混用，没有明确层级 | compact 32、standard 36、touch 44 三档；图标保持 16px | 视觉尺寸与 hit area 分开定义，避免任意宽高 |

## 关键 invariant

- `InteractiveSnapshot` 继续是 Thread、Run、Timeline、Interaction、catalog、selection、capability 和连接状态的唯一业务事实来源。
- Adapter 可以拥有 draft、submit pending、panel、theme、Tool 展开和 focus 恢复等表现状态，但不能复制 Timeline reducer 或解释 Protocol。
- textarea 的 `value` 只能由 React controlled prop 写入；不得再用 effect、ref、DOM setter 或第三方组件写第二份 value。
- draft 的即时发布只影响本地输入；模型 streaming 与高频 Event 仍按 frame 合并，不能为修输入取消全局 batching。
- submit 必须使用 Adapter 内当前 draft，而不是信任组件携带的另一个字符串副本。
- 未被 Controller 接受的提交不清空草稿；重复点击、Enter 连发或 pointer/keyboard 同时触发只能产生一次 `input.submit`。
- 代码高亮只处理 fenced code 的纯文本和已校验 language；高亮结果只能是 offset/scope 数据，不得包含 HTML。
- 高亮失败不能让 Markdown Error Boundary 接管全页；原始代码始终是可用降级。
- Tree-sitter core、language WASM 和 query 全部来自随 CLI 分发的固定资产；运行时零网络下载。
- syntax 静态路由由编译期 catalog 精确映射，不把 URL path 拼接为文件系统路径。
- 空 Assistant message 只在“非 streaming 且 content 为空”时省略；streaming 空消息仍显示角色与 cursor，不能让运行看起来停住。
- 宽窄屏只改变排布与 hit area，不创建第二套 Composer、Markdown 或 Timeline 业务组件。
- light/dark 使用同一 DOM、scope 和状态结构，只替换 semantic token。

## Composer 设计

### 问题链路

当前链路是：

```text
浏览器修改 textarea.value
  → React onChange
  → async Adapter.dispatch(draft-change)
  → Adapter 更新 draft，但 listener 延迟到 rAF
  → React controlled prop 在本次事件结束时仍可能是旧值
  → useLayoutEffect 又命令式覆盖 textarea.value
```

同时，测试工具 `changeValue()` 因 Happy DOM 兼容问题直接取得 React 私有 props 并调用 `onChange`，跳过了浏览器真正发生问题的前半段。

### 目标状态机

```text
editable
  ├─ input/composition → draft 立即发布 → editable
  ├─ submit(valid) → submitting
  ├─ active Run → editable-with-cancel（可继续准备 draft，但不能提交）
  └─ readonly/leaving/closed → locked

submitting
  ├─ accepted → 清空提交时那一版 draft → active Run / editable
  ├─ present/command result → 按 Controller 结果处理 → editable
  └─ rejected/error → 保留 draft + notice → editable
```

### Adapter interface

内部 API 尚未发布，直接迁移到单一 canonical 形状，不保留旧 `submit { value }` alias：

```ts
export type WebAdapterSnapshot = {
  // 现有字段
  readonly draft: string
  readonly composerSubmitting: boolean
  readonly composerError: string | null
}

export type WebIntent =
  | { type: "draft-change"; value: string }
  | { type: "submit" }
  | { type: "composer-error-dismiss" }
  | /* 现有 intent */
```

处理规则：

- `draft-change` 同步更新 `draft`，清除过期 `composerError`，重新计算命令菜单，并调用输入专用的 `publishNow()`。
- `submit` 从 `this.draft` 截取一次 `submittedDraft`；空白、readonly、active Run 或 `composerSubmitting=true` 时不调用 Controller。
- 受理前设置 `composerSubmitting=true` 并立即发布；button 保留“发送”可访问名称并显示 spinner。
- Controller 成功返回后，仅当当前 draft 仍等于 `submittedDraft` 时清空。若用户在等待期间继续修改，不删除新输入。
- Controller 抛错或明确拒绝时，保留 draft，写入脱敏 `composerError`，恢复 `composerSubmitting=false` 并聚焦 textarea。
- Controller 返回 `present/request-exit` 时继续走现有 `handleInteractiveResult`，不在 Composer 复制命令语义。
- Adapter `close()` 后所有输入/提交 intent no-op；未决提交只允许现有 Controller/Handoff 生命周期收敛，不产生第二次结果发布。

### React component

- 删除给 `textareaRef.current.value` 赋值的 `useLayoutEffect`。
- textarea 保持 `value={snapshot.draft}`；`onChange` 只派发 `draft-change`。
- 使用原生 `<form>` 包裹 Composer；button 为 `type="submit"`，`onSubmit` 是发送的统一入口。点击与键盘不能各写一套提交逻辑。
- 保留产品现有键位：Enter 发送、Shift+Enter 换行；额外支持 Cmd/Ctrl+Enter 发送。
- composition start/end 由 ref 记录；`event.nativeEvent.isComposing`、本地 composing ref 或 `keyCode=229` 任一成立时，Enter 都只交给 IME。
- 自动增长只修改 textarea 的 `style.height`，不修改 value：先 `height:auto`，再把 `scrollHeight` clamp 到 1–8 行；宽度变化后重新测量。
- 不阻止 paste，不过滤输入字符，不在 typing 时 trim；只在提交有效性判断时使用 `trim()`。
- active Run 期间 textarea 可保留/编辑下一条草稿，但 action 位显示取消且 form submit 被 Adapter gate；若后续产品决定运行时完全锁定，需另立语义任务。
- `aria-describedby` 连接键盘提示、readonly reason 或错误；错误使用稳定 polite/alert 区域，不能只给输入框红边。

### 输入验证

必须覆盖：

- 单字、多字快速输入，连续输入跨多个 animation frame。
- 中文拼音/五笔 composition、候选确认 Enter、日文/韩文 composition。
- 粘贴、多行、首尾空格、emoji、超长 draft。
- Enter、Shift+Enter、Cmd/Ctrl+Enter、button click、双击和键盘连发。
- typing 期间 Timeline streaming、Tool 展开、theme、panel、Thread catalog publish。
- submit 成功、Controller 抛错、未知 Slash、command present、active Run、connection closed、leaving。

## Web 离线语法高亮

### 选择 Tree-sitter 的原因

- TUI 已经使用同一套语言 parser 和 `highlights.scm`，用户期望两个表现层对同一 fenced language 有一致能力。
- 仓库已锁定 `web-tree-sitter@0.25.10`；不需要引入 Prism、Shiki、highlight.js 或第二套语言资产。
- Tree-sitter 输出语法范围，React 可以从原文和 offset 创建 text/span，不需要信任高亮器 HTML。

### 模块结构

```text
packages/cli/src/tui/platform/assets/syntax/       canonical parser/query 文件
packages/cli/scripts/vendor-syntax-assets.ts       校验 manifest，并生成 TUI + Web catalog

packages/cli/src/web/syntax/catalog.generated.ts   language/alias/asset ID/query 的只读表
packages/cli/src/web/syntax/protocol.ts            main ↔ Worker typed message
packages/cli/src/web/syntax/worker.ts              web-tree-sitter 初始化、缓存、parse/query
packages/cli/src/web/syntax/client.ts              request ID、超时、缓存、close
packages/cli/src/web/presentation/code-block.tsx   loading/plain/highlighted/copy UI
packages/cli/src/web/presentation/markdown.tsx     fenced code 交给 CodeBlock
```

生成文件只描述固定 catalog，不复制业务逻辑。TUI 继续消费 `generated-syntax-parsers.ts`；Web catalog 从同一 manifest 生成 aliases 与 asset IDs，避免语言清单漂移。

### Worker protocol

```ts
export type SyntaxWorkerRequest =
  | { type: "highlight"; requestId: number; language: string; code: string }
  | { type: "dispose" }

export type SyntaxSpan = {
  readonly startByte: number
  readonly endByte: number
  readonly scope: SyntaxScope
}

export type SyntaxWorkerResponse =
  | { type: "highlighted"; requestId: number; language: string; spans: readonly SyntaxSpan[] }
  | { type: "plain"; requestId: number; reason: "unknown-language" | "too-large" | "load-failed" | "parse-failed" | "timeout" }
```

`SyntaxScope` 是有限集合：`comment`、`keyword`、`function`、`variable`、`string`、`number`、`type`、`operator`、`punctuation`、`tag`、`attribute`、`constant`、`plain`。query capture 名先按完整名匹配，再按前缀归一；未知 capture 不着色。

Worker 返回 UTF-8 byte offsets。Main thread 在同一原文上建立 byte → UTF-16 index 映射，再输出不重叠、按位置排序的 React span。范围非法、越界或重叠无法决议时整块降级为 plain，不能猜测切割。

### 资产与静态路由

现有 server 只允许 page、lifecycle、`/web/app.js` 和 `/web/app.css`。本任务新增的路由仍必须是固定映射：

```text
/web/syntax-worker.js
/web/syntax/tree-sitter.wasm
/web/syntax/lang/<asset-id>.wasm
```

- `asset-id` 来自生成 catalog，例如 `python-v0_23_6`，不是用户提供的 language 字符串。
- server 持有 `ReadonlyMap<path, { contentType, body }>`；请求 path 完整命中 map 才返回，绝不 `resolve()` 或读取任意磁盘路径。
- 只允许 `GET`、非 upgrade、正确 loopback Host；沿用 `no-store`、`nosniff`、`same-origin` 和 `no-referrer`。
- query 文本体积小，编译进 Worker bundle；language WASM 按需从固定路由加载，避免把约 6.6 MiB 全部塞进首屏 JS。
- `browserBundle()` 和生产 `bun run build` 必须同时产出 app、worker、core WASM 和语言 WASM；source dev 与 dist 运行使用同一 `WebAssets` 形状。
- bundle test 必须证明所有 catalog asset 都有输出、hash/size 与 manifest 一致、没有额外可访问路径。

WebAssembly 在目标浏览器 CSP 下需要显式授权时，只在 `script-src` 增加 `'wasm-unsafe-eval'`；不得使用更宽的 `'unsafe-eval'`、`blob:`、`data:` 或远端源。CSP 单测必须锁定完整 directive。

### 加载、缓存与资源上限

- 首个受支持 fenced code 出现时才创建一个 Worker；无代码的对话不承担 WASM 成本。
- Worker 内 `Parser.init` single-flight；每种 language 的 `Language`、`Query` 和 `Parser` promise single-flight 缓存。
- 同一 `{language, codeHash}` 在 main thread 使用有界 LRU 缓存；默认最多 128 块或 4 MiB 原文，以先达到者为准。
- 单块代码默认上限 64 KiB UTF-8 或 2,000 行；超过后直接 plain。该值是 UI 防卡顿边界，不改变原文展示。
- 单次 highlight 设置 1,500ms client timeout；超时只忽略迟到结果，Worker 继续收敛。连续三次 worker fatal 后打开本页面熔断器，后续全部 plain。
- Component unmount 或 code/language 变化时用 request ID 丢弃 stale response；不需要取消已经进入 Worker 的 parse。
- Worker 关闭时删除 Query/Parser/Tree/Language（API 允许的对象）并 terminate；页面 close 只能执行一次。

### CodeBlock 渲染

```text
原始 code + language
  → 立即渲染 plain code（无空白闪烁）
  → Worker 成功后替换为同文本的 scoped spans
  → 失败时 DOM 文本保持不变
```

- 顶栏显示规范化 language；无 language 时显示“文本”。
- 复制按钮使用原生 Clipboard API，只复制原始 code；成功/失败写入稳定 polite status，并保留可访问名称“复制代码”。
- 行号默认不显示，避免小屏挤压和复制污染；未来若需要另立功能。
- 不渲染 token tooltip、可点击 symbol、代码执行按钮或外链。
- `white-space: pre`，水平滚动只发生在 code container；代码块不得撑大 `.web-shell`。
- 加载过程不使用 spinner 占位覆盖代码；可在语言标签旁显示低干扰“高亮中”，避免布局跳动。

### 高亮主题 token

在 HC-106 semantic token 上增加 scope 角色，不在 JSX 写 raw color：

| Scope token | Light 语义 | Dark 语义 |
| --- | --- | --- |
| `--syntax-comment` | 低强调 neutral，italic | 低强调 neutral，italic |
| `--syntax-keyword` | 可辨识蓝紫 | 较亮蓝紫 |
| `--syntax-function` | 深蓝 | 亮蓝 |
| `--syntax-variable` | 洋红/红紫 | 亮红紫 |
| `--syntax-string` | 深绿 | 浅绿 |
| `--syntax-number` | 棕橙 | 金色 |
| `--syntax-type` | 紫色 | 浅紫 |
| `--syntax-operator` | 青色 | 浅青 |
| `--syntax-punctuation` | text-soft | muted/high contrast neutral |

具体值在实施时从 TUI theme 的语义关系出发，用当前 Web token 记法定义，并逐对测量 code surface 上的 APCA/WCAG 对比。不能直接复制只适用于 TUI 深色背景的 hex。

## 界面精修规范

### 布局与共享边

桌面骨架保持：

```text
260px Thread | minmax(0, central) | 372px optional utility
54px topbar   | Timeline          | Utility tabs
              | Interaction dock  |
              | Composer          |
```

- Timeline 外列仍最大 880px；prose 内层最大 `72ch`，以消息正文 leading edge 对齐。
- code、table、Tool、Interaction 可以使用完整消息正文轨，但不能越过 880px 外列。
- Message avatar、author header、正文、Tool 和 Interaction 只使用两条 leading edge：avatar edge 与 content edge。
- Message 内部 gap 6–8px；相邻同类 item 10–12px；不同语义组 20–24px。连续 Tool 属于同一组，不插空 Assistant 行。
- 分隔线只保留 topbar、sidebar、Composer、table 和真正的结构边界；角色与消息组主要靠 space 和 surface 分组。

### Brand 与 Topbar

- Brand mark 25×25，accent-soft 背景、accent border、accent-strong `H`；不使用实心主按钮外观。
- Brand name 13px/650；version 11px muted，使用 tabular numbers。
- Topbar project 和 branch 保持截断；branch 只在有值时显示。
- Model/approval chip：16px Lucide outline icon + value；“模型”“审批”作为 `sr-only` label，不再持续占用视觉宽度。
- Activity chip：状态点 + label，状态同时有文字，不只依赖颜色。
- Desktop compact icon button 32×32；图标 16px、regular label 邻接时 stroke 1.5px，semibold 邻接时 2px。一条 toolbar 不混用其它 icon library。
- Overflow、返回和 chip 保持 6–8px 组内间距；project 与 meta/action group 之间由 flex 空间分隔。

### Thread sidebar

- 桌面不渲染 `sidebar-heading`；Brand 下 14px 顶部间距后直接显示 36px “新建 Thread”。
- 搜索与刷新为同一 control group：search 36px 高，刷新可见 box 32px、目标 32px；移动端目标扩到 44px。
- Thread item padding 9–10px；title 13px/600，summary 12px/1.4，meta 11px tabular。三者统一 leading edge。
- active 使用 accent-soft + accent-border；hover 仅 surface-2；disabled 同时有 reason/title 与文字变化。
- 没有匹配项时只显示一次空态，不保留伪列表高度或额外 divider。

### Timeline message

- Avatar：27×27 桌面、24×24 移动；只放图标或单字符，禁止放角色全名。
- 每条 User/Assistant 内容区先渲染 `message-head`：author 13px/650；当前无可信 timestamp，不留时间占位。
- Assistant 正文无大卡片，prose 14px、line-height 1.65–1.7、最大 72ch。
- User 正文使用 accent-soft 小 surface，`inline-size: fit-content`、`max-inline-size: min(72ch, 100%)`；短消息不铺满整行。
- System 继续使用无 avatar 的 muted notice。
- 非 streaming 且 content 为空的 Assistant message 不产生 DOM row；streaming 为空时显示 avatar、author 和 cursor。
- Markdown heading 按 h2→h6 递减；正文 14px，caption/metadata 不低于 11px，非必要计数才允许 10px。

### Tool 与 Interaction

- Tool 位于 content edge，连续 Tool gap 8px，整组与前后 Message gap 16–20px。
- Tool header 40px，名称 mono 12px/600，参数摘要 11px，状态 11px；状态保留 icon + text。
- disclosure button 整个 header 可点击，`aria-expanded` 与 focus-visible 保持；chevron 只做状态图标。
- details code/output 11–12px mono、最大 260px 高，按内容选择 pre-wrap 或局部水平滚动。
- Interaction 维持 3px accent 轨；桌面 action 36px 高，移动端 44px 高；同一决策上下文只有一个 filled primary。
- 按钮 label 决定宽度，不用固定英文宽度；长中文/英文允许 action row 换行。

### Composer

- 外层与 Timeline 使用同一 880px shared edge；桌面 vertical padding 10/12，移动端 8/`safe-area`。
- textarea 正文 14px；移动端输入字号至少 16px，避免系统自动缩放；桌面可回到 14px。
- Composer rail 内左侧 Skill chip，右侧单一 send/cancel action；没有 Skill 时 hint 直接占左侧，不保留空 slot。
- 发送/取消桌面 36×36 icon-only；移动端 44×44。tooltip/title、`aria-label` 和静态颜色/图标共同表达动作。
- submitting 保持发送图标区域和 accessible label，使用小 spinner 或 progress state，不替换成无名称的 spinner。
- `:focus-within` 只增强 border；textarea 保留独立 `:focus-visible` 可感知焦点，不使用 `outline:none` 后无替代。

### Utility workspace

- 保留 HC-106 的 372px、四个 tab 与 Help 模式，不因截图中未打开就强制默认展开。
- icon button、search、panel item、switch 使用与 Sidebar 相同尺寸层级。
- tablist 是一个复合控件：Tab 进入/离开，ArrowLeft/ArrowRight、Home/End 在 tab 内移动；active tab 使用 `aria-selected` 和 2px underline。
- desktop aside 不错误声明 modal；移动端抽屉才使用 `role="dialog" aria-modal="true"`、focus trap 和 scrim。

## 尺寸与交互 token

不再为每个组件临时写高度。CSS 增加用途明确的尺寸 token：

| Token | Desktop | Narrow | 用途 |
| --- | --- | --- | --- |
| `--control-compact` | 32px | 44px | icon button、overflow、refresh、copy |
| `--control-standard` | 36px | 44px | new thread、send/cancel、普通按钮、search |
| `--control-field` | 36px | 44px minimum | input/select |
| `--icon-sm` | 16px | 18px | toolbar/control icon |
| `--radius-control` | 5px | 5px | button/input/item |
| `--radius-surface` | 6px | 6px | Composer/Interaction/Dialog |

规则：

- WCAG AA 硬下限 24×24px；桌面 compact professional chrome 可用 32px，但相邻目标的 hit area 不得重叠。
- 390px 下主要 action 和抽屉控件必须达到 44×44px；可见 glyph 仍保持 16–18px，不随 hit area 变成大图标。
- bordered/filled controls 组内 gap 从 8px 起，互不相关 group 至少 16px；borderless icon control 通过自身 box 明确边界。
- active press 使用 `scale(.96)` 或 1px translate 二选一并全局统一。本任务选择 `scale(.96)`；只 transition `transform/background/color/border/opacity`，不使用 `transition: all`。
- 动效放在 `prefers-reduced-motion: no-preference`；reduced motion 下保留即时颜色、focus 和状态文案。

## 响应式、缩放与双主题

- 断点继续唯一使用 `max-width: 899px`；React 常量与 CSS 注释/测试必须一致。
- 390×844：Topbar 只保留 Thread trigger、project、model value 和 overflow；直接返回、branch、approval/activity 细节放入 menu/Status。
- Sidebar/Utility 抽屉宽度 `min(336px, calc(100vw - 32px))`，互斥、带 scrim、Escape、focus restore 和 safe area。
- 320px reflow 和 200% zoom 时页面只允许垂直主滚动；table/code 是唯一允许局部双向滚动的二维内容。
- fixed `height` 只用于纯图标 box；包含文字的控件使用 `min-height`，按钮宽度由 label + inline padding 决定。
- 所有关键前景/背景 pair 在 light/dark 分别测量；syntax scope、placeholder、metadata、disabled 和 focus ring 不能因主题切换失去可读性。
- accent 继续只表示 primary action、selected、running 和 interactive；静态 heading、brand name、普通 metadata 使用 neutral。

## 错误与降级语义

### Composer

- `active=false`：保留 handoff banner，textarea disabled，说明“等待 CLI 确认”。
- connection 非 open：draft 保留，输入和 submit disabled；恢复连接后不清空。
- submit error：draft 保留，显示脱敏 inline error/polite status，焦点回到 textarea。
- active Run：submit gate，action 变取消；取消失败沿用 Controller notice，不清 draft。
- leaving/close：全部动作 no-op，不显示成功假象。

### Syntax

- unknown/no language：plain code，语言标签为原始安全文本或“文本”。
- too large：plain code，可显示“内容较大，未高亮”的非阻塞说明。
- core/language/query load failure：记录脱敏 console diagnostic，当前块 plain；不显示本地路径或 stack 给用户。
- parse timeout/fatal：当前块 plain；达到熔断阈值后本页面其它块直接 plain。
- copy unavailable/denied：代码不变，状态区提示“复制失败，请手动选择”。

### Presentation

- icon 加载失败时可见文字或 accessible name 仍表达动作。
- Markdown parse 失败继续显示原始文本；高亮不得改变现有 raw HTML/link/image 安全行为。
- CSS token 未命中时 `:root` 浅色默认保持可读；未知 `data-theme` 不产生透明文字。

## 测试方案

### Composer unit / integration

- Adapter：`draft-change` 立即通知，不等待 manual scheduler tick；stream publish 仍 batching。
- Adapter：`submit` 不携带 value，读取当前 draft；并发 submit 去重；用户在等待时修改 draft 不被旧结果清空。
- Adapter：成功、throw、present、request-exit、active Run、readonly、close 的状态与错误。
- Component：没有命令式 `.value =`；form submit 是 click/keyboard 统一入口；IME 不误发；auto-grow 只改 height。
- 集成：真实 adapter + WebApp，原生 setter + `input`/composition/submit event 更新 DOM、snapshot 和 mock Controller；若 Happy DOM 仍无法承载，不能再次用 React 私有 props 替代该层证据，应把此用例放入 HC-105 Browser smoke 并在 HC-107 保留 Tabbit 手工证据。

### Syntax tests

- catalog 生成：语言、alias、asset ID 与 manifest 一致；不存在路径穿越字符或重复 alias。
- Worker：Python/TS/JSON/Bash 等代表语言返回预期 scope；UTF-8 中文前后的 byte offset 转换正确。
- React：高亮与 plain 的 `textContent` 完全等于原始 code；无 `innerHTML`、style 属性或可执行 DOM。
- fallback：unknown、too-large、load failure、timeout、stale response、worker fatal/熔断。
- cache/lifecycle：single-flight、LRU 上限、component unmount、client close、worker terminate。
- server：精确 worker/WASM route、GET only、Host/CSP/content-type/no-store、非 catalog path 404、encoded traversal 404。
- bundle：source/dist 都包含 app/worker/core/language assets；不隐式联网；构建输出大小在任务证据中记录。

### Presentation DOM / CSS

- 空 final Assistant 不渲染，空 streaming Assistant 仍渲染 cursor。
- avatar 内没有 `Harness` 长文本；author label 独立存在。
- Sidebar desktop 无重复 heading，drawer 有 heading。
- User short/long fixture、连续 5 个 Tool、长 argument、Markdown prose/code/table 的 class 与状态。
- CSS contract 锁定 72ch、三档 control、44px narrow、syntax token、local overflow、focus-visible、reduced motion。
- Utility desktop 不是 modal；narrow drawer 是 modal，tab keyboard pattern 正确。

### 真实 Tabbit 抽查

实施完成后至少检查：

| Viewport | Theme | 场景 |
| --- | --- | --- |
| 1440×900 | light | 中文/英文输入、短 User、连续 Tool、Python code、Utility closed/open |
| 1440×900 | dark | JSON/Bash code、focus、copy、Tool failed、submit error |
| 390×844 | light | IME、软键盘、drawer、Interaction actions、44px targets |
| 390×844 | dark | 长 project/model、code 横向滚动、safe area、200% zoom |

每组记录：根 `scrollWidth <= clientWidth`、主要控件 bounding box、不重叠 hit area、输入/发送实际结果、代码 plain/highlighted text 一致。截图只作为 HC-107 实施证据；HC-105 再将这些场景自动化。

### 项目级验证

```bash
cd packages/cli && bun test tests/web/application/adapter.test.ts tests/web/presentation tests/web/bundle.test.ts tests/web/server.test.ts
bun run build
bun run typecheck
bun run test
bun run project:check
```

任务完成前按仓库规范运行 `open-code-review`，以 HC-107 task、本文和实际 diff 为 background；所有高/中优先级问题修复后复检。

## 按依赖排序的实施步骤

1. **固定复现证据**：在 Tabbit 记录 textarea 的 focus/disabled/value、原生 input/composition、Adapter publish 和 Controller dispatch；新增失败测试。验证是测试能在修复前稳定失败或现场日志明确停在哪一段。
2. **收敛 Composer 单一 owner**：迁移 WebIntent、移除命令式 value 写入、增加 immediate draft publish 与 submit 状态。验证快速输入、IME、错误保留和重复提交。
3. **定义语法 catalog 和 Worker protocol**：修改资产生成脚本，建立受限语言/alias/scope。验证生成器和 UTF-8 offset 单测。
4. **接通 bundle/server/CSP**：产出 worker/core/language 资产和精确路由，不改变 Handoff/Agent WebSocket。验证 bundle/server/security tests。
5. **实现安全 CodeBlock**：异步 plain-first、高亮 span、copy、cache、timeout、fallback。验证 textContent invariant 和失败矩阵。
6. **修复 Timeline 内容模型的表现漏洞**：省略空 final Assistant、分离 avatar/author、增加 prose measure、连续 Tool 分组间距。验证截图对应 fixture。
7. **统一组件尺寸与布局**：按 Brand/Topbar/Sidebar/Composer/Utility 顺序使用三档 control token；完成 light/dark 和 narrow override。验证 CSS contract、键盘和 hit area。
8. **文档与真实验收**：更新用户说明和架构静态路由/CSP/Worker说明，在 Tabbit 完成四组抽查，运行项目级检查和 OCR。
9. **交接 HC-105**：把输入、IME、代码高亮 fallback、连续 Tool 和四组布局加入 Browser E2E/截图基线，避免 HC-105 只验证旧场景。

## 可观察验收

- 用户进入已有或新 Thread 后可以立即聚焦 Composer，连续输入和发送，不再出现文字回退、光标丢失或按钮无效。
- 发送失败时原文本仍在；发送成功后只清空已提交的那版文本；运行中准备的新草稿不会被旧 submit 结果删除。
- Python、TypeScript/JavaScript、JSON、Bash、C/C++、Go、Java、HTML、CSS、YAML 等 catalog 语言按需高亮，且不发出互联网请求。
- 禁用 Worker 或让语言资产失败时，同一代码完整显示为 plain text，页面其它消息继续 streaming。
- 截图中的 `Harness` 溢出和 Tool 前空角色行消失；连续 Tool 形成紧凑组，Assistant 正文和短 User 消息不再横跨过长行。
- Sidebar 首屏更接近 HTML 稿；Brand/chip/send action 的层级和尺寸一致；desktop 保持紧凑，narrow 提供 44px 触控目标。
- light/dark、1440×900/390×844、200% zoom 下无根横向滚动、按钮裁切、focus 丢失或颜色唯一状态提示。
- 所有业务动作仍由共享 Controller 执行；Protocol、Host、ControlLease 和 Handoff 测试无行为变化。

## 非范围

- 不改变 Message/Tool Protocol 字段，不新增时间戳、duration、path 或虚构 metadata。
- 不做代码编辑器、行号、diff、symbol navigation、代码执行或下载。
- 不持久化 draft、theme 或高亮缓存到 SQLite/localStorage。
- 不把 syntax Worker 暴露为通用 Worker 平台，也不允许 Plugin 注入 parser/query。
- 不引入远端 asset、第二个 icon set、第二套 Markdown parser 或 UI framework。
- 不重构 TUI renderer，不要求 Web/TUI 共用 CSS，只共享 canonical syntax asset manifest。
- 不在本任务完成 HC-105 的完整 Playwright/Host fixture、生命周期竞争和最终基线管理。
