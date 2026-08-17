/**
 * 文件类型图标：同族 lucide 描边图标 + 按类型着色（--file-* semantic token）。
 * 所有文件行都有图标保证纵向对齐；颜色区分类型家族，选中行保留类型色。
 */
/** @jsxImportSource react */

import { File, FileCode, FileImage, FileJson, FileText } from "lucide-react"
import type { LucideIcon } from "lucide-react"

/** 文件类型键；与 styles.css 的 .file-row-icon-<type> 颜色规则一一对应。 */
export type FileIconType = "ts" | "js" | "code" | "style" | "doc" | "config" | "image" | "default"

export type FileIconSpec = {
  readonly type: FileIconType
  readonly icon: LucideIcon
}

/** 按扩展名的类型归属（小写）；未列出的扩展名回落 default。 */
const EXTENSION_TYPES: Record<string, FileIconType> = {
  ts: "ts", tsx: "ts", mts: "ts", cts: "ts",
  js: "js", jsx: "js", mjs: "js", cjs: "js",
  py: "code", sh: "code", bash: "code", zsh: "code", rb: "code", go: "code", rs: "code",
  java: "code", c: "code", cc: "code", cpp: "code", h: "code", hpp: "code",
  cs: "code", php: "code", lua: "code", r: "code", swift: "code", kt: "code", kts: "code",
  css: "style", scss: "style", less: "style", sass: "style", html: "style", htm: "style",
  vue: "style", svelte: "style",
  md: "doc", mdx: "doc", txt: "doc", rst: "doc", adoc: "doc",
  json: "config", jsonc: "config", json5: "config", toml: "config", yaml: "config", yml: "config",
  ini: "config", env: "config", cfg: "config", conf: "config", lock: "config",
  sql: "config", csv: "config", xml: "config",
  png: "image", jpg: "image", jpeg: "image", gif: "image", svg: "image",
  webp: "image", ico: "image", avif: "image", bmp: "image",
}

/** 无扩展名特殊文件名的类型归属，优先于扩展名判断（键为小写文件名）。 */
const SPECIAL_NAME_TYPES: Record<string, FileIconType> = {
  dockerfile: "config",
  makefile: "config",
  ".gitignore": "config",
  ".gitattributes": "config",
  ".editorconfig": "config",
  license: "doc",
  readme: "doc",
}

/** 各类型共用的图标形：同族描边风格，类型差异由颜色表达。 */
const TYPE_ICONS: Record<FileIconType, LucideIcon> = {
  ts: FileCode,
  js: FileCode,
  code: FileCode,
  style: FileCode,
  doc: FileText,
  config: FileJson,
  image: FileImage,
  default: File,
}

/** 由文件名推导类型与图标；大小写不敏感，特殊文件名优先于扩展名。 */
export function fileIconFor(name: string): FileIconSpec {
  const lower = name.toLowerCase()
  const special = SPECIAL_NAME_TYPES[lower]
  if (special) return { type: special, icon: TYPE_ICONS[special] }
  const dot = lower.lastIndexOf(".")
  // `.env` 这类点开头文件名按扩展名处理；无点或仅末尾有点则无扩展名。
  const ext = dot >= 0 ? lower.slice(dot + 1) : ""
  const type = (ext && EXTENSION_TYPES[ext]) || "default"
  return { type, icon: TYPE_ICONS[type] }
}

/** 文件行类型图标：装饰性（aria-hidden），颜色由 .file-row-icon-<type> 经 token 表达。 */
export function FileTypeIcon({ name, size = 14 }: { name: string; size?: number }): React.ReactElement {
  const spec = fileIconFor(name)
  const Icon = spec.icon
  return <Icon aria-hidden="true" size={size} className={`file-row-icon file-row-icon-${spec.type}`} />
}
