/** CSS contract test：主题 token 完备、无系统主题覆盖、无历史双轨 class。 */

import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"

const css = readFileSync(new URL("../../../src/web/presentation/styles.css", import.meta.url), "utf8")

test("styles.css 不包含颜色型系统主题覆盖（prefers-color-scheme）", () => {
  expect(css).not.toContain("prefers-color-scheme")
})

test("light/dark 主题通过 .web-shell[data-theme] 选择器挂载，且关键 token 均存在", () => {
  expect(css).toContain('.web-shell[data-theme="light"]')
  expect(css).toContain('.web-shell[data-theme="dark"]')
  const lightBlock = css.slice(css.indexOf('.web-shell[data-theme="light"]'), css.indexOf('.web-shell[data-theme="dark"]'))
  const darkBlock = css.slice(css.indexOf('.web-shell[data-theme="dark"]'))
  for (const token of ["--bg", "--surface", "--surface-2", "--surface-3", "--line", "--line-strong", "--text", "--text-soft", "--muted", "--subtle", "--accent", "--accent-strong", "--accent-soft", "--accent-border", "--accent-border-strong", "--success", "--warning", "--danger", "--chrome", "--tool-output-bg", "--tool-output-text", "--interaction-bg", "--command-bg", "--command-text", "--composer-bg", "--drawer-bg"]) {
    expect(lightBlock).toContain(token)
    expect(darkBlock).toContain(token)
  }
})

test("可访问性别名 token 存在：action/link/success/warning/danger 文字与按钮色", () => {
  for (const token of ["--action-bg", "--action-text", "--link-text", "--success-text", "--warning-text", "--danger-text"]) {
    expect(css).toContain(token)
  }
})

test("紧凑/标准/字段三档尺寸与桌面命中目标保持为 token contract", () => {
  for (const token of ["--control-compact", "--control-standard", "--control-field", "--radius-control", "--radius-surface"]) {
    expect(css).toContain(token)
  }
  // 桌面化清理后 44px 移动端命中目标已删除；基础 32px 三档保留。
  expect(css).toContain("--control-standard: 32px")
  expect(css).not.toContain("--control-standard: 44px")
  expect(css).toContain(".composer-textarea {")
  expect(css).toContain(".send-button, .cancel-button { width: var(--control-standard); height: var(--control-standard);")
})

test("CodeBlock 独占一层 surface，prose/表格/代码各自维持局部滚动", () => {
  expect(css).toContain(".code-block {")
  expect(css).toContain(".code-block-body {")
  expect(css).toContain("overflow-x: auto")
  expect(css).not.toContain(".markdown-code {")
  expect(css).toContain("max-inline-size: min(72ch, 100%)")
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

test("保留 prefers-reduced-motion 可访问性规则", () => {
  expect(css).toContain("prefers-reduced-motion")
})

test("行号列保持逐行垂直排列：.line-numbers 必须 white-space: pre（否则 \\n 被折叠为一行）", () => {
  const block = css.slice(css.indexOf(".line-numbers {"), css.indexOf(".file-code-pre"))
  expect(block).toContain("white-space: pre")
  // 与代码侧同字号同行高，保证行号与代码行一一对齐。
  expect(block).toContain("line-height: 1.6")
})
