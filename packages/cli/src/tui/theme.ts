/** Harness Code 终端主题：暖黑画布、单一蓝色品牌强调和离线语法 scope。 */

import { SyntaxStyle } from "@opentui/core"

/** 暖黑画布与 za38 蓝色强调；代码语义色仅用于帮助阅读模型输出。 */
export const tuiTheme = {
  background: "#090a0c",
  panel: "#121316",
  composer: "#1b1c20",
  toolSurface: "#141518",
  menu: "#16181d",
  element: "#17191d",
  border: "#282b32",
  borderActive: "#5c8fe4",
  text: "#e9ebf1",
  muted: "#9297a3",
  subtle: "#575d68",
  primary: "#70a4ff",
  primarySoft: "#456da8",
  star: "#46516a",
  trail: "#7fb1ff",
  success: "#73b99a",
  warning: "#d8b86e",
  danger: "#e17b95",
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
