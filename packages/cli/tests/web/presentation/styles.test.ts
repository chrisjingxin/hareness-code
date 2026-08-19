/** CSS contract test：主题 token 完备、无系统主题覆盖、无历史双轨 class。 */

import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"

const css = readFileSync(new URL("../../../src/web/presentation/styles.css", import.meta.url), "utf8")

test("styles.css 不包含颜色型系统主题覆盖（prefers-color-scheme）", () => {
  expect(css).not.toContain("prefers-color-scheme")
})

test("semantic token 在 light/dark 各只定义一次，禁止尾部重定义块赢级联", () => {
  expect(css.split('.web-shell[data-theme="light"]').length - 1).toBe(1)
  expect(css.split('.web-shell[data-theme="dark"]').length - 1).toBe(1)
  // 浅色 token 值以 HC-124 设计表为准；prototype 调色板不得残留。
  expect(css).toContain("--bg: #f7f6f3")
  expect(css).toContain("--accent: #15803d")
  expect(css).not.toContain("#6675e8")
  expect(css).not.toContain("#f5f7fb")
})

test("组件层无硬编码色：颜色只出现在 light/dark token 定义块与 var() 回退中", () => {
  // 摘除两个主题 token 定义块（从 light 选择器到 dark 块结束）。
  const lightStart = css.indexOf('.web-shell[data-theme="light"]')
  const darkStart = css.indexOf('.web-shell[data-theme="dark"]')
  const darkEnd = css.indexOf("}", darkStart)
  const componentLayer = css.slice(0, lightStart) + css.slice(darkEnd + 1)
  // var() 回退色（如 var(--bg, #f7f6f3)）是 boot 期合法用法，先摘除再断言。
  const withoutVarFallbacks = componentLayer.replace(/var\([^)]*\)/g, "")
  expect(withoutVarFallbacks).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
})

test("头像与按钮不使用 linear-gradient，强调色走 --accent/--action 平面语义", () => {
  expect(css).not.toContain("linear-gradient")
})

test("取消按钮不使用主行动填充：danger 描边 + 透明底（绿色填充是「执行」语义）", () => {
  expect(css).toContain(".cancel-button { background: transparent; border-color: var(--danger-vivid); color: var(--danger-vivid); }")
  const afterSendFill = css.slice(css.lastIndexOf(".send-button { background: var(--action)"))
  expect(afterSendFill).not.toMatch(/\.send-button, \.cancel-button[^}]*background: var\(--action\)/)
})

test("扁平化：原型层的卡片阴影与漂移圆角已清除", () => {
  // 原型层签名阴影色（rgba(39, 53, 87, …)）不得残留；阴影只经 --shadow/--shadow-float token。
  expect(css).not.toMatch(/rgba\(39, 53, 87/)
  expect(css).not.toMatch(/rgba\(80, 102, 180/)
  expect(css).not.toMatch(/rgba\(102, 117, 232/)
  // 组件圆角只允许 token、4px 小圆角、50% 或 999px 胶囊；8~16px 的漂移值禁止出现。
  expect(css).not.toMatch(/border-radius:\s*(8|9|10|11|12|13|14|15|16)px/)
})

test("扁平化：消息与 Agent 分组不是卡片（无 border/background/box-shadow 卡片三件套）", () => {
  expect(css).not.toMatch(/\.timeline-message\s*\{[^}]*box-shadow/)
  expect(css).not.toMatch(/\.timeline-agent-group\s*\{[^}]*box-shadow/)
  expect(css).not.toMatch(/\.timeline-agent-group\s*\{[^}]*background/)
})

test("空态文案在 timeline 可视区垂直水平双居中（容器 flex column，margin auto），向下 48px 偏移且用 subtle 淡色", () => {
  expect(css).toContain(".timeline-empty { margin: auto;")
  expect(css).not.toContain("13vh")
  // 2026-08-18 用户实测：居中位置偏上、颜色偏重——向下 48px、muted 改 subtle。
  expect(css).toContain("transform: translateY(48px)")
  expect(css).toContain(".timeline-empty { margin: auto; max-width: 520px; color: var(--subtle);")
})

test("文件类型图标颜色只经 --file-* token，light/dark 双主题定义；选中行不强制变绿", () => {
  const lightBlock = css.slice(css.indexOf('.web-shell[data-theme="light"]'), css.indexOf('.web-shell[data-theme="dark"]'))
  const darkBlock = css.slice(css.indexOf('.web-shell[data-theme="dark"]'))
  for (const token of ["--file-ts", "--file-js", "--file-code", "--file-style", "--file-config", "--file-image"]) {
    expect(lightBlock).toContain(token)
    expect(darkBlock).toContain(token)
  }
  for (const type of ["ts", "js", "code", "style", "doc", "config", "image", "default"]) {
    expect(css).toContain(`.file-row-icon-${type} { color: var(`)
  }
  // 选中行的图标不再强制 accent 色：类型色在选中态保留
  expect(css).not.toContain(".file-row.is-selected .file-row-icon")
})

test("文件行箭头/图标/文字同轴 flex 居中：无手动 top 偏移", () => {
  const iconBlock = css.slice(css.indexOf(".file-row-icon {"), css.indexOf(".file-row-icon-ts"))
  expect(iconBlock).not.toContain("top:")
  expect(iconBlock).not.toContain("position: relative")
  // 箭头槽是 flex 居中的 lucide chevron 容器，不再是文本字形。
  expect(css).toContain(".file-row-arrow { width: 14px; flex: 0 0 14px; display: inline-flex; align-items: center; justify-content: center;")
  // 行高独立于全局 1.6，三个槽位共用同一轴线。
  const rowBlock = css.slice(css.indexOf(".file-row {"), css.indexOf(".file-row:hover"))
  expect(rowBlock).toContain("line-height: 1.4")
})

test("选中 Thread 只用浅绿背景表达：无左侧 inset 色轨", () => {
  expect(css).not.toContain("inset 2px 0 0 var(--accent)")
})

test("Thread 图标槽与标题行等高对齐首行中轴：无 margin-top 手调", () => {
  const iconBlock = css.slice(css.indexOf(".thread-item-icon {"), css.indexOf("/* active Thread"))
  expect(iconBlock).toContain("height: calc(13px * 1.35)")
  expect(iconBlock).toContain("align-self: start")
  expect(iconBlock).not.toContain("margin-top")
})

test("扁平化：侧栏是单一容器，无线程/文件孤岛卡", () => {
  expect(css).not.toContain("Soft card alignment")
  expect(css).not.toMatch(/\.workspace-sidebar-thread-panel,\s*\n?\.workspace-sidebar-files\s*\{[^}]*box-shadow/)
  // Thread/Files 分隔槽：透明命中区 + 居中 1px 分隔线，hover/拖动走强调色
  expect(css).toContain(".vertical-resize-handle { flex: 0 0 9px")
  expect(css).toContain(".vertical-resize-handle::after")
  expect(css).toContain(".vertical-resize-handle:hover::after, .vertical-resize-handle.is-dragging::after { background: var(--accent-border-strong); }")
})

test("Work Item 横幅只保留工作项卡：模式指示与空态已并入 Composer rail", () => {
  expect(css).not.toContain(".work-item-mode")
  expect(css).not.toContain(".work-item-empty")
})

test("light/dark 主题通过 .web-shell[data-theme] 选择器挂载，且使用 ZC-124 semantic token", () => {
  expect(css).toContain('.web-shell[data-theme="light"]')
  expect(css).toContain('.web-shell[data-theme="dark"]')
  const lightBlock = css.slice(css.indexOf('.web-shell[data-theme="light"]'), css.indexOf('.web-shell[data-theme="dark"]'))
  const darkBlock = css.slice(css.indexOf('.web-shell[data-theme="dark"]'))
  const semanticTokens = ["--bg", "--surface", "--surface-2", "--surface-3", "--line", "--line-strong", "--text", "--text-soft", "--muted", "--subtle", "--accent", "--accent-hover", "--accent-soft", "--accent-border", "--accent-border-strong", "--action", "--success", "--warning", "--danger", "--chrome", "--tool-output-bg", "--tool-output-text", "--interaction-bg", "--command-bg", "--command-text", "--composer-bg", "--drawer-bg"]
  for (const token of semanticTokens) {
    expect(lightBlock).toContain(token)
    expect(darkBlock).toContain(token)
  }
  expect(lightBlock).toContain("--surface-2: #f2f1ec")
  expect(lightBlock).toContain("--line: #e1e0da")
  expect(lightBlock).toContain("--accent: #15803d")
  expect(lightBlock).toContain("--action: #15803d")
  expect(darkBlock).toContain("--surface-2: #23211d")
  expect(darkBlock).toContain("--line: #38352f")
  expect(darkBlock).toContain("--accent: #4ade80")
  expect(darkBlock).toContain("--action: #4ade80")
  const rootBlock = css.slice(css.indexOf(":root {"), css.indexOf("* {"))
  expect(rootBlock).not.toContain("--bg")
  expect(css).not.toContain("--accent-strong")
})

test("可访问性辅助 token 存在：action/link/success/warning/danger 文字与按钮色", () => {
  for (const token of ["--action-text", "--link-text", "--success-text", "--warning-text", "--danger-text"]) {
    expect(css).toContain(token)
  }
})

test("紧凑/标准/字段三档尺寸与桌面命中目标保持为 token contract", () => {
  for (const token of ["--control-compact", "--control-standard", "--control-field", "--radius-control", "--radius-surface"]) {
    expect(css).toContain(token)
  }
  expect(css).toContain("--topbar-height: 52px")
  expect(css).toContain("--control-compact: 30px")
  expect(css).toContain("--control-standard: 34px")
  expect(css).toContain("--control-field: 36px")
  expect(css).toContain("--radius-control: 5px")
  expect(css).toContain("--radius-surface: 7px")
  expect(css).toContain(".composer-textarea {")
  expect(css).toContain(".send-button, .cancel-button { width: var(--control-standard); height: var(--control-standard);")
})

test("CodeBlock 独占一层 surface，prose/表格/代码各自维持局部滚动", () => {
  expect(css).toContain(".code-block {")
  expect(css).toContain(".code-block-body {")
  expect(css).toContain("overflow-x: auto")
  expect(css).not.toContain(".markdown-code {")
  // 72ch 阅读宽度只约束纯文本段落；代码块/表格不受其收窄，与工具卡同宽。
  expect(css).toContain(".markdown p,")
  expect(css).toContain("max-inline-size: 72ch")
  expect(css).not.toContain("max-inline-size: min(72ch, 100%)")
})

test("桌面三栏布局与文件预览样式存在：desktop-workspace / context-dock / file-code-view", () => {
  expect(css).toContain(".desktop-workspace {")
  expect(css).toContain("grid-template-columns: var(--sidebar-width) minmax(560px, 1fr)")
  expect(css).toContain(".desktop-workspace.has-context-dock")
  expect(css).toContain("--sidebar-width: 280px")
  expect(css).toContain(".context-dock {")
  expect(css).toContain(".context-dock-tabs")
  expect(css).toContain(".dock-tab")
  expect(css).toContain(".file-tabs")
  expect(css).toContain(".file-tab.is-active")
  expect(css).toContain(".file-code-view")
  expect(css).toContain(".line-numbers")
  expect(css).toContain(".workspace-sidebar {")
  expect(css).toContain(".sidebar-resize-handle")
  expect(css).toContain(".vertical-resize-handle")
  expect(css).toContain(".file-tree")
})

test("截图几何契约：内容列固定居中（≤880px）；关闭态按 Dock 默认宽预留，打开态按实际宽", () => {
  const geometry = css.slice(css.indexOf("Screenshot geometry contract"))
  expect(geometry).toContain("--workspace-gap: 16px")
  expect(geometry).toContain("--conversation-padding-block-start: 16px")
  expect(geometry).toContain("--conversation-padding-block-end: 20px")
  // 内容列宽度 = min(880px, 视口 − 侧栏 − 预留 Dock 宽 − 间距)，随视口与侧栏/Dock 宽度联动
  expect(geometry).toContain("--conversation-content-max-width: 880px")
  expect(geometry).toContain("--conversation-content-width: min(var(--conversation-content-max-width), calc(100vw - var(--sidebar-width) - var(--dock-width-reserved) - 2 * var(--workspace-gap)))")
  // 预留宽度：Dock 关闭时用默认宽（拖宽后关闭，内容列恢复默认）；打开时用实际宽
  expect(geometry).toContain("--dock-width-default: 354px")
  expect(geometry).toContain("--dock-width-reserved: var(--dock-width-default);")
  expect(geometry).toContain(".desktop-workspace.has-context-dock {\n  --dock-width-reserved: var(--dock-width);\n  grid-template-columns: var(--sidebar-width) minmax(0, 1fr) var(--dock-width);\n}")
  // 第三轨常驻：关闭时 0px、打开时 var(--dock-width)；轨数一致才能做轨宽过渡（平移）
  expect(geometry).toContain("grid-template-columns: var(--sidebar-width) minmax(0, 1fr) 0px")
  expect(geometry).toContain("grid-template-columns: var(--sidebar-width) minmax(0, 1fr) var(--dock-width)")
  // 横幅/消息流/审批卡/输入框共用同一居中内容列
  expect(geometry).toContain(".work-item-banner,\n.timeline-scroll,\n.interaction-dock,\n.composer-inner {")
  expect(geometry).toContain("max-width: var(--conversation-content-width);\n  margin-inline: auto;")
  // Dock 关闭时本体不可见但保持挂载（平移过渡的载体）；inert/aria-hidden 由 TSX 表达
  expect(geometry).toContain(".desktop-workspace:not(.has-context-dock) .context-dock { visibility: hidden; }")
  // 旧契约已废弃：不再有 20px 右外边距 token，不再撑满内容列
  expect(geometry).not.toContain("--workspace-padding-end")
  expect(geometry).not.toContain("max-width: none;\n  margin-inline: 0;")
  // 保留：贴边面板无圆角，只留面向中栏的 1px 分隔线
  expect(geometry).toContain("border: 0;\n  border-right: 1px solid var(--line);\n  border-radius: 0;")
  expect(geometry).toContain("border: 0;\n  border-left: 1px solid var(--line);\n  border-radius: 0;")
  expect(geometry).toContain("overflow: clip")
  expect(geometry).toContain(".workspace-sidebar { gap: 0; }")
  expect(geometry).toContain("padding: 12px 14px 14px")
  expect(geometry).toContain(".context-dock-code-scroll")
  expect(geometry).toContain("overflow: hidden")
})

test("Dock 开关平移过渡：轨宽 200ms 过渡 + 拖动分隔条豁免 + Dock 本体延迟隐藏", () => {
  const motion = css.slice(css.indexOf("prefers-reduced-motion: no-preference"))
  expect(motion).toContain(".desktop-workspace { transition: grid-template-columns 200ms ease; }")
  // 拖动侧栏/Dock 分隔条持续改写轨宽，必须禁用过渡，否则拖拽被惯性拖慢
  expect(motion).toContain(".desktop-workspace:has(.is-dragging) { transition: none; }")
  // 关闭方向保持可见到平移结束，打开方向立即可见
  expect(motion).toContain(".context-dock { transition: visibility 200ms; }")
  // 内容列宽度随预留宽度同步过渡（Dock 拖宽后开关时不跳变）；拖动分隔条时同样豁免
  expect(motion).toContain(".work-item-banner, .timeline-scroll, .interaction-dock, .composer-inner { transition: max-width 200ms ease; }")
  expect(motion).toContain(".desktop-workspace:has(.is-dragging) .work-item-banner,\n  .desktop-workspace:has(.is-dragging) .timeline-scroll,\n  .desktop-workspace:has(.is-dragging) .interaction-dock,\n  .desktop-workspace:has(.is-dragging) .composer-inner { transition: none; }")
})

test("时间线滚动条隐藏：滚动行为保留，内容列右缘不出现可见滚动条", () => {
  const base = css.slice(css.indexOf(".timeline-scroll {"), css.indexOf(".timeline {"))
  expect(base).toContain("scrollbar-width: none")
  expect(base).not.toContain("scrollbar-width: thin")
  expect(css).toContain(".timeline-scroll::-webkit-scrollbar { display: none; }")
  // 不再参与共享 webkit 滚动条样式分组
  expect(css).not.toContain(".timeline-scroll::-webkit-scrollbar-thumb")
  expect(css).not.toContain(".timeline-scroll::-webkit-scrollbar-track")
})

test("窄屏（≤900px）内容列恢复满宽；审批卡独立宽度已并入统一内容列", () => {
  const narrow = css.slice(css.indexOf("@media (max-width: 900px)"))
  // 内容列宽度公式在窄屏会算出极小值，必须重置
  expect(narrow).toContain(".work-item-banner, .timeline-scroll, .interaction-dock, .composer-inner { max-width: none; }")
  // 紧凑审批卡/Diff 放宽的独立宽度规则已删除，几何由内容列统一表达
  expect(css).not.toContain(".interaction-dock:has(.file-diff-approval)")
  expect(css).not.toContain("width: min(560px, calc(100% - 48px))")
  expect(css).not.toContain("width: min(760px, calc(100% - 48px))")
  expect(css).not.toContain("width: min(880px, calc(100% - 48px))")
})

test("待处理审批 Dock 在视口内独立纵向滚动，不遮挡 Composer 和审批操作", () => {
  const block = css.slice(css.indexOf(".interaction-dock {"), css.indexOf(".interaction-dock .interaction-card"))
  expect(block).toContain("min-height: 0")
  expect(block).toContain("max-height: calc(100% - var(--control-field) - 32px)")
  expect(block).toContain("overflow-y: auto")
  expect(block).toContain("scrollbar-gutter: stable")
})

test("桌面化清理：移动端抽屉、workspace-header 与窄屏断点样式已删除", () => {
  expect(css).not.toContain("sidebar-drawer")
  expect(css).not.toContain("drawer-scrim")
  expect(css).not.toContain("utility-drawer")
  expect(css).not.toContain("workspace-grid")
  expect(css).not.toContain("workspace-header")
  expect(css).not.toContain("NARROW_QUERY")
  expect(css).not.toContain("max-width: 899px")
  expect(css).not.toContain("mobile-only")
})

test("历史双轨 class 已删除：同一组件不再保留旧 class 规则", () => {
  for (const legacy of [".message-bubble", ".composer-wrap", ".status-pill", ".thread-sidebar", ".utility-panel", ".topbar-status", ".mobile-thread-bar", ".sidebar-action", ".sidebar-disabled-reason", ".topbar-meta", ".topbar-model", ".topbar-approval", ".topbar-segment", ".topbar-project", ".project-name", ".meta-chip", ".approval-mode-menu", ".approval-mode-option", ".composer-decoration-icons", ".composer-hint"]) {
    expect(css).not.toContain(legacy)
  }
})

test("状态圆点语义色：就绪/完成=绿，运行中=蓝，思考/等待/取消中=黄，失败=红，已取消=灰", () => {
  expect(css).toContain(".status-dot-idle, .status-dot-home { background: var(--success); }")
  expect(css).toContain(".status-dot-running { background: var(--accent); }")
  expect(css).toContain(".status-dot-compacting, .status-dot-starting, .status-dot-waiting-interaction, .status-dot-cancelling { background: var(--warning); }")
  expect(css).toContain(".status-dot-failed { background: var(--danger); }")
  expect(css).toContain(".status-dot-cancelled { background: var(--muted); }")
})

test("外围 chrome 层级：新建 Thread 为 secondary，Run status 使用 accent 层级，focus ring 使用 accent", () => {
  const newThreadBlock = css.slice(css.indexOf(".new-thread-button {"), css.indexOf(".sidebar-toolbar"))
  expect(newThreadBlock).toContain("background: var(--surface)")
  expect(newThreadBlock).not.toContain("var(--action)")
  // Run status 顶栏 chip 已随顶栏精简移除（HC-125）；accent-soft 层级由选中态等保留。
  expect(css).not.toContain(".meta-chip-run")
  expect(css).toContain("background: var(--accent-soft)")
  expect(css).toContain("outline: 2px solid var(--accent); outline-offset: 2px")
})

test("reasoning block 只引用 semantic token，保留 ZC-118 的可见状态样式", () => {
  const reasoningBlock = css.slice(css.indexOf(".reasoning {"), css.indexOf(".reasoning-header"))
  expect(reasoningBlock).toContain("background: var(--surface-2)")
  expect(reasoningBlock).toContain("border-left: 2px solid var(--accent-border-strong)")
  expect(css).not.toContain("--surface-raised")
  expect(css).not.toContain("--border-control")
})

test("保留 prefers-reduced-motion 可访问性规则", () => {
  expect(css).toContain("prefers-reduced-motion")
})

test("运行进度具备独立样式，并在 reduced-motion 下停用动画", () => {
  expect(css).toContain(".run-progress {")
  expect(css).toContain(".run-progress-spinner")
  expect(css).toContain(".run-progress-spinner, .spinning")
})

test("行号列保持逐行垂直排列：.line-numbers 必须 white-space: pre（否则 \\n 被折叠为一行）", () => {
  const block = css.slice(css.indexOf(".line-numbers {"), css.indexOf(".file-code-pre"))
  expect(block).toContain("white-space: pre")
  // 与代码侧同字号同行高，保证行号与代码行一一对齐。
  expect(block).toContain("line-height: 1.6")
})

test("侧栏文件分区标题即工作区名：纯文字无图标，与线程分区标题同规格，名称可省略", () => {
  // 无图标规则（与线程分区标题形态一致）。
  expect(css).not.toMatch(/\.file-explorer-title\s+svg/)
  // 与线程分区标题同字号字重（14px/700）。
  expect(css).toMatch(/\.file-explorer-title\s*\{[^}]*font-size:\s*14px;\s*font-weight:\s*700/)
  expect(css).toMatch(/\.thread-panel-title\s*\{[^}]*font-size:\s*14px;\s*font-weight:\s*700/)
  expect(css).toMatch(/\.file-explorer-title-text\s*\{[^}]*text-overflow:\s*ellipsis/)
})

test("Rail 容器不裁剪下拉菜单：rail 三段均不声明 overflow", () => {
  for (const sel of [".composer-rail-left", ".composer-rail-right"]) {
    const block = css.match(new RegExp(`${sel.replace(".", "\\.")}\\s*\\{[^}]*\\}`, "g"))
    expect(block).not.toBeNull()
    for (const rule of block ?? []) expect(rule).not.toContain("overflow")
  }
  // rail 本体只允许 padding 几何覆盖，不得引入 overflow。
  const railBlocks = css.match(/\.composer-rail\s*\{[^}]*\}/g) ?? []
  for (const rule of railBlocks) expect(rule).not.toContain("overflow")
})

test("Rail chip 字号与灰度对齐 DSH 参考：14px + muted，hover/展开才加深", () => {
  expect(css).toMatch(/\.rail-chip\s*\{[^}]*font-size:\s*14px/)
  expect(css).toMatch(/\.rail-chip\s*\{[^}]*color:\s*var\(--muted\)/)
  expect(css).toMatch(/\.rail-chip:hover:not\(:disabled\),\s*\.rail-chip\[aria-expanded="true"\]\s*\{[^}]*color:\s*var\(--text\)/)
})

test("Rail 下拉菜单向上弹出且守 overlay 纪律：shadow + line-strong 描边 + surface 圆角", () => {
  expect(css).toMatch(/\.composer-menu\s*\{[^}]*bottom:\s*calc\(100% \+ 6px\)/)
  expect(css).toMatch(/\.composer-menu\s*\{[^}]*box-shadow:\s*var\(--shadow\)/)
  expect(css).toMatch(/\.composer-menu\s*\{[^}]*border:\s*1px solid var\(--line-strong\)/)
  expect(css).toMatch(/\.composer-menu\s*\{[^}]*border-radius:\s*var\(--radius-surface\)/)
})

test("用户消息与 AI 正文同字号同行高：14px/1.7（对齐 markdown 正文规格，2026-08-17 用户确认）", () => {
  // 用户消息是纯文本 div，曾继承基础 15px/1.6；AI 消息走 .markdown（14px/1.7），两者不一致是 token 统一遗留。
  expect(css).toContain(".message-user .message-content { font-size: 14px; line-height: 1.7; }")
})

test("Composer 聚焦反馈走整卡描边：textarea 不挂全局 focus outline，整卡 focus-within 变 accent", () => {
  // 全局 textarea:focus-visible 绿框曾包在全宽 textarea 上，渲染成 tab 下/输入区下两条错位绿线（用户截图反馈）。
  expect(css).toContain(".composer-textarea:focus-visible { outline: 0; }")
  expect(css).toContain(".composer-box:focus-within { border-color: var(--accent); }")
})

test("Tool 折叠态 chevron 常驻可见：不再 hover 才显现（2026-08-18 与用户确认可发现性）", () => {
  expect(css).not.toMatch(/\.tool-row-chevron\s*\{[^}]*opacity:\s*0/)
  expect(css).not.toContain(".tool-row-header:hover .tool-row-chevron")
  expect(css).toContain(".tool-row-chevron.expanded { transform: rotate(180deg); }")
})

test("Tool 失败状态带文字徽章样式（完成/运行不占文字位）", () => {
  expect(css).toMatch(/\.tool-status-failed\s*\{[^}]*display:\s*inline-flex/)
  expect(css).toContain(".tool-status-text {")
})

test("Tool 展开详情卡片化：输出/参数同级卡片 + 头部条 + 复制与展开全部（2026-08-18 设计确认）", () => {
  // 卡片：1px line 边框 + surface 圆角 + tool-output-bg 底，阴影纪律不破（无 box-shadow）。
  expect(css).toMatch(/\.tool-detail-card\s*\{[^}]*border:\s*1px solid var\(--line\)/)
  expect(css).toMatch(/\.tool-detail-card\s*\{[^}]*border-radius:\s*var\(--radius-surface\)/)
  expect(css).toMatch(/\.tool-detail-card\s*\{[^}]*background:\s*var\(--tool-output-bg\)/)
  expect(css).not.toMatch(/\.tool-detail-card\s*\{[^}]*box-shadow/)
  // 头部条：标题 + 行数统计 + 复制按钮。
  expect(css).toContain(".tool-detail-header {")
  expect(css).toContain(".tool-detail-title {")
  expect(css).toContain(".tool-detail-meta {")
  expect(css).toContain(".tool-detail-copy {")
  // 折叠阈值由组件 data-clamped 表达：钳制限高 280px，展开后 560px 内滚。
  expect(css).toMatch(/\.tool-detail-body\[data-clamped="true"\]\s*\{[^}]*max-height:\s*280px/)
  expect(css).toMatch(/\.tool-detail-body\[data-clamped="false"\]\s*\{[^}]*max-height:\s*560px/)
  expect(css).toContain(".tool-detail-expand {")
  // 参数卡片 chevron 随 details[open] 旋转。
  expect(css).toContain(".tool-detail-arguments[open] .tool-detail-chevron { transform: rotate(180deg); }")
})

test("Tool 结构化渲染样式：file-content 行号 gutter、path-list 行、grep 分组（2026-08-18 轮 2）", () => {
  // 三类结构化内容共用等宽 12.5px/1.65 规格。
  expect(css).toMatch(/\.tool-file-lines,\s*\.tool-path-list,\s*\.tool-grep-matches\s*\{[^}]*font-size:\s*12\.5px/)
  // file-content：元信息行 + 行号 gutter（右对齐、subtle、不可选中）。
  expect(css).toContain(".tool-file-meta {")
  expect(css).toMatch(/\.tool-file-lineno\s*\{[^}]*text-align:\s*right/)
  expect(css).toMatch(/\.tool-file-lineno\s*\{[^}]*user-select:\s*none/)
  // path-list：行内图标 + 路径文本。
  expect(css).toMatch(/\.tool-path-row\s*\{[^}]*display:\s*flex/)
  expect(css).toContain(".tool-path-text {")
  // grep：分组路径头加粗，匹配行带行号列。
  expect(css).toContain(".tool-grep-group {")
  expect(css).toMatch(/\.tool-grep-path\s*\{[^}]*font-weight:\s*600/)
  expect(css).toContain(".tool-grep-lineno {")
  // 类型图标色复用侧栏 --file-* token 体系，不在 tool 区新增颜色规则。
  expect(css).not.toMatch(/\.tool-path-row\s+svg\s*\{[^}]*color/)
})

test("取消按钮按设计稿：surface 圆角 + 实心圆角方块可见 14px + 鲜亮 --danger-vivid（2026-08-18 用户实测纠正）", () => {
  expect(css).toContain(".cancel-button { border-radius: var(--radius-surface); }")
  // 可见方块 = 24px 盒 × 14/24 rect = 14px ≈ 按钮的 39%（设计稿 43%，实心形更大一档读感）。
  expect(css).toMatch(/\.cancel-button\s+\.cancel-stop-icon\s*\{[^}]*width:\s*24px/)
  expect(css).toMatch(/\.cancel-button\s+\.cancel-stop-icon\s*\{[^}]*height:\s*24px/)
  // 鲜亮红走新增 --danger-vivid token（light/dark 各定义一次），描边与图标同色。
  expect(css).toContain("--danger-vivid:")
  expect(css).toMatch(/\.cancel-button\s*\{[^}]*border-color:\s*var\(--danger-vivid\)/)
  expect(css).toMatch(/\.cancel-button\s*\{[^}]*color:\s*var\(--danger-vivid\)/)
  expect(css).toMatch(/\.cancel-button\s+\.cancel-stop-icon\s*\{[^}]*color:\s*var\(--danger-vivid\)/)
})

test("回到底部按钮双态：中性找路 / 有新输出 accent 强调（2026-08-18 用户反馈语义）", () => {
  expect(css).toMatch(/\.scroll-to-bottom\s*\{[^}]*color:\s*var\(--text-soft\)/)
  expect(css).toMatch(/\.scroll-to-bottom\[data-new="true"\]\s*\{[^}]*color:\s*var\(--accent-hover\)/)
})

test("Tool diff 视图复用 --diff-* token：红绿行与符号列不引新色（2026-08-18 轮 3）", () => {
  expect(css).toMatch(/\.tool-diff-row\[data-type="add"\]\s*\{[^}]*background:\s*var\(--diff-add-bg\)/)
  expect(css).toMatch(/\.tool-diff-row\[data-type="remove"\]\s*\{[^}]*background:\s*var\(--diff-remove-bg\)/)
  expect(css).toMatch(/\.tool-diff-row\[data-type="add"\]\s+\.tool-diff-sign\s*\{[^}]*color:\s*var\(--diff-add-sign\)/)
  expect(css).toMatch(/\.tool-diff-row\[data-type="remove"\]\s+\.tool-diff-sign\s*\{[^}]*color:\s*var\(--diff-remove-sign\)/)
  expect(css).toContain(".tool-diff-meta {")
  // 终端块：命令行与输出分区，等宽规格一致。
  expect(css).toMatch(/\.tool-terminal-cmd\s*\{[^}]*font-weight:\s*600/)
  expect(css).toContain(".tool-terminal-line {")
})
