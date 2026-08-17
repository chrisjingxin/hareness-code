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
  expect(css).toContain(".cancel-button { background: transparent; border-color: var(--danger); color: var(--danger-text); }")
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

test("截图几何契约：左中固定 16px，右 Dock 不改变中栏起点", () => {
  const geometry = css.slice(css.indexOf("Screenshot geometry contract"))
  expect(geometry).toContain("--workspace-gap: 16px")
  expect(geometry).toContain("--workspace-padding-inline: 20px")
  expect(geometry).toContain("grid-template-columns: var(--sidebar-width) minmax(0, 1fr)")
  expect(geometry).toContain("grid-template-columns: var(--sidebar-width) minmax(0, 1fr) var(--dock-width)")
  expect(geometry).toContain("width: 100%;\n  max-width: none;\n  margin-inline: 0;")
  expect(geometry).toContain("overflow: clip")
  expect(geometry).toContain(".workspace-sidebar { gap: 0; }")
  expect(geometry).toContain("padding: 12px 14px 14px")
  expect(geometry).toContain(".context-dock-code-scroll")
  expect(geometry).toContain("overflow: hidden")
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
  for (const legacy of [".message-bubble", ".composer-wrap", ".status-pill", ".thread-sidebar", ".utility-panel", ".topbar-status", ".mobile-thread-bar", ".sidebar-action", ".sidebar-disabled-reason"]) {
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
  expect(css).toContain(".meta-chip-run {")
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
