/** Harness Code 终端主题：Mode 金/紫、Semantic 色与离线语法 scope。 */

import { SyntaxStyle } from "@opentui/core"

/** Logo 仍用品牌蓝；Mode 身份只走 modeAccent，不得用 primary 冒充当前 Mode。 */
export const tuiTheme = {
  modeBuild: "#EAB308",
  modeCompose: "#A9A5D4",
  modeShell: "#56B6C2",
  thinking: "#7EB6C9",
  background: "#0B0C0E",
  surface: "#15171A",
  surfaceElevated: "#1B1D21",
  overlay: "#1B1D21",
  panel: "#15171A",
  toolSurface: "#15171A",
  menu: "#1B1D21",
  element: "#15171A",
  border: "#2A2D33",
  borderActive: "#EAB308",
  text: "#E8E9EC",
  muted: "#A0A4AE",
  subtle: "#676C76",
  primary: "#3b82f6",
  primarySoft: "#1d4ed8",
  pickerActive: "#EAB308",
  star: "#3f3f46",
  trail: "#60a5fa",
  success: "#7FA37A",
  warning: "#C88758",
  danger: "#C56F6F",
  diffAdd: "#6F9A72",
  diffRemove: "#B96A6A",
  diffAddedBackground: "#155815",
  diffRemovedBackground: "#581515",
  syntaxComment: "#858b99",
  syntaxKeyword: "#8da7ff",
  syntaxFunction: "#79c6ff",
  syntaxVariable: "#f08ba9",
  syntaxString: "#9bce93",
  syntaxNumber: "#e6bb72",
  syntaxType: "#c4a7f2",
  syntaxOperator: "#7bd4d0",
  syntaxPunctuation: "#b8becb",
} as const

/**
 * OpenTUI 使用 Tree-sitter 与 Markdown scope 名称，而非简化的 heading/strong 名称。
 * 统一 scope 后，普通文本、Markdown 和 fenced code block 可复用同一套语义色。
 */
export const markdownSyntax = SyntaxStyle.fromTheme([
  { scope: ["default"], style: { foreground: tuiTheme.text } },
  { scope: ["comment", "comment.documentation"], style: { foreground: tuiTheme.syntaxComment, italic: true } },
  { scope: ["string", "symbol", "character", "character.special"], style: { foreground: tuiTheme.syntaxString } },
  { scope: ["number", "float", "boolean", "constant"], style: { foreground: tuiTheme.syntaxNumber } },
  {
    scope: ["keyword", "keyword.return", "keyword.conditional", "keyword.repeat", "keyword.exception"],
    style: { foreground: tuiTheme.syntaxKeyword, italic: true },
  },
  { scope: ["keyword.type", "type", "class", "module", "namespace"], style: { foreground: tuiTheme.syntaxType, bold: true } },
  { scope: ["keyword.function", "function", "function.method", "constructor"], style: { foreground: tuiTheme.syntaxFunction } },
  { scope: ["variable", "variable.parameter", "property", "field", "parameter"], style: { foreground: tuiTheme.syntaxVariable } },
  { scope: ["tag", "tag.name"], style: { foreground: tuiTheme.syntaxType, bold: true } },
  { scope: ["tag.error"], style: { foreground: tuiTheme.danger, bold: true } },
  { scope: ["attribute", "tag.attribute"], style: { foreground: tuiTheme.syntaxVariable } },
  { scope: ["operator", "keyword.operator", "punctuation.delimiter"], style: { foreground: tuiTheme.syntaxOperator } },
  { scope: ["punctuation", "punctuation.bracket"], style: { foreground: tuiTheme.syntaxPunctuation } },
  { scope: ["string.escape", "string.regexp"], style: { foreground: tuiTheme.syntaxKeyword } },
  { scope: ["variable.builtin", "type.builtin", "function.builtin", "module.builtin"], style: { foreground: tuiTheme.danger } },
  { scope: ["markup.heading", "markup.heading.1", "markup.heading.2", "markup.heading.3"], style: { foreground: tuiTheme.primary, bold: true } },
  { scope: ["markup.heading.4", "markup.heading.5", "markup.heading.6"], style: { foreground: tuiTheme.primary } },
  { scope: ["markup.bold", "markup.strong"], style: { foreground: tuiTheme.warning, bold: true } },
  { scope: ["markup.italic", "markup.quote"], style: { foreground: tuiTheme.warning, italic: true } },
  { scope: ["markup.list", "markup.list.enumeration"], style: { foreground: tuiTheme.primary } },
  { scope: ["markup.raw", "markup.raw.block", "markup.raw.inline"], style: { foreground: tuiTheme.syntaxString, background: tuiTheme.element } },
  { scope: ["markup.link", "markup.link.url", "string.special.url"], style: { foreground: tuiTheme.primary, underline: true } },
  { scope: ["markup.link.label", "label"], style: { foreground: tuiTheme.trail, underline: true } },
  { scope: ["conceal"], style: { foreground: tuiTheme.subtle } },
])

/** 当前 Run / 输入栏身份色；Build 与 Compose 等权。 */
export function modeAccent(mode: "build" | "compose"): string {
  return mode === "compose" ? tuiTheme.modeCompose : tuiTheme.modeBuild
}

/** 用户消息竖条颜色只看该条 Mode；缺字段按 Build，不读当前会话。 */
export function userMessageAccent(workMode: "build" | "compose" | undefined): string {
  return modeAccent(workMode ?? "build")
}
