/**
 * TUI 文件树语言图标与品牌专色映射引擎。
 * 采用全终端 100% 原生支持的标准 Unicode 符号/Emoji 与官方品牌专色，
 * 无需用户安装额外的 Nerd Font 字体即可完美呈现。
 */

export type FileIconInfo = {
  /** 字符图标（带尾部空格以保持 2 单元格对齐） */
  readonly icon: string
  /** 品牌专属色彩（十六进制颜色值） */
  readonly color: string
}

const DIRECTORY_COLOR = "#e6bb72" // 琥珀金
const SYMLINK_COLOR = "#c4a7f2"   // 柔和紫
const DEFAULT_FILE_COLOR = "#a6accd" // 中性灰蓝

/** 特殊完整文件名精准匹配 */
const EXACT_FILE_MAP: Readonly<Record<string, FileIconInfo>> = {
  "package.json": { icon: "📦 ", color: "#cb3837" },
  "package-lock.json": { icon: "🔒 ", color: "#cb3837" },
  "bun.lockb": { icon: "🔒 ", color: "#fbf0df" },
  "bun.lock": { icon: "🔒 ", color: "#fbf0df" },
  "tsconfig.json": { icon: "⚙ ", color: "#3178c6" },
  "jsconfig.json": { icon: "⚙ ", color: "#f7df1e" },
  "dockerfile": { icon: "🐳 ", color: "#2496ed" },
  "docker-compose.yml": { icon: "🐳 ", color: "#2496ed" },
  "docker-compose.yaml": { icon: "🐳 ", color: "#2496ed" },
  ".gitignore": { icon: "🐙 ", color: "#f05032" },
  ".gitattributes": { icon: "🐙 ", color: "#f05032" },
  ".gitmodules": { icon: "🐙 ", color: "#f05032" },
  ".env": { icon: "🔑 ", color: "#ebd15b" },
  ".env.local": { icon: "🔑 ", color: "#ebd15b" },
  ".env.development": { icon: "🔑 ", color: "#ebd15b" },
  ".env.production": { icon: "🔑 ", color: "#ebd15b" },
  "readme.md": { icon: "📝 ", color: "#519aba" },
  "readme": { icon: "📝 ", color: "#519aba" },
  "changelog.md": { icon: "📝 ", color: "#519aba" },
  "license": { icon: "📜 ", color: "#d0bf41" },
  "license.md": { icon: "📜 ", color: "#d0bf41" },
  "license.txt": { icon: "📜 ", color: "#d0bf41" },
  "makefile": { icon: "🔧 ", color: "#6d8086" },
  "cargo.toml": { icon: "⚙ ", color: "#dea584" },
  "cargo.lock": { icon: "🔒 ", color: "#dea584" },
  "pyproject.toml": { icon: "⚙ ", color: "#3572a5" },
  "requirements.txt": { icon: "📋 ", color: "#3572a5" },
  "go.mod": { icon: "⚙ ", color: "#00add8" },
  "go.sum": { icon: "🔒 ", color: "#00add8" },
}

/** 文件扩展名映射 */
const EXTENSION_MAP: Readonly<Record<string, FileIconInfo>> = {
  // Python: 🐍 蟒蛇
  py: { icon: "🐍 ", color: "#3572a5" },
  ipynb: { icon: "🐍 ", color: "#3572a5" },
  pyc: { icon: "🐍 ", color: "#515c6b" },

  // TypeScript / JavaScript
  ts: { icon: "📄 ", color: "#3178c6" },
  tsx: { icon: "📄 ", color: "#3178c6" },
  js: { icon: "📄 ", color: "#f7df1e" },
  jsx: { icon: "📄 ", color: "#61dafb" },
  mjs: { icon: "📄 ", color: "#f7df1e" },
  cjs: { icon: "📄 ", color: "#f7df1e" },

  // Rust: 🦀 螃蟹
  rs: { icon: "🦀 ", color: "#dea584" },

  // Go: 🐹 Gopher
  go: { icon: "🐹 ", color: "#00add8" },

  // C / C++
  c: { icon: "📄 ", color: "#599eff" },
  h: { icon: "📄 ", color: "#a074c4" },
  cpp: { icon: "📄 ", color: "#f34b7d" },
  cc: { icon: "📄 ", color: "#f34b7d" },
  cxx: { icon: "📄 ", color: "#f34b7d" },
  hpp: { icon: "📄 ", color: "#a074c4" },
  hxx: { icon: "📄 ", color: "#a074c4" },

  // Java / Kotlin / Scala
  java: { icon: "☕ ", color: "#b07219" },
  kt: { icon: "📄 ", color: "#7f52ff" },
  kts: { icon: "📄 ", color: "#7f52ff" },
  scala: { icon: "📄 ", color: "#dc322f" },

  // Web Frontend
  html: { icon: "🌐 ", color: "#e34f26" },
  htm: { icon: "🌐 ", color: "#e34f26" },
  css: { icon: "🎨 ", color: "#1572b6" },
  scss: { icon: "🎨 ", color: "#c6538c" },
  sass: { icon: "🎨 ", color: "#c6538c" },
  less: { icon: "🎨 ", color: "#1d365d" },
  vue: { icon: "📄 ", color: "#41b883" },
  svelte: { icon: "📄 ", color: "#ff3e00" },

  // Data & Config: ⚙ 齿轮
  json: { icon: "⚙ ", color: "#cbcb41" },
  json5: { icon: "⚙ ", color: "#cbcb41" },
  jsonc: { icon: "⚙ ", color: "#cbcb41" },
  yaml: { icon: "⚙ ", color: "#cb171e" },
  yml: { icon: "⚙ ", color: "#cb171e" },
  toml: { icon: "⚙ ", color: "#9c4221" },
  xml: { icon: "⚙ ", color: "#e37933" },
  sql: { icon: "🗄 ", color: "#e38c00" },
  graphql: { icon: "📄 ", color: "#e10098" },
  gql: { icon: "📄 ", color: "#e10098" },

  // Shell / Scripts: 💻
  sh: { icon: "💻 ", color: "#4eaa25" },
  bash: { icon: "💻 ", color: "#4eaa25" },
  zsh: { icon: "💻 ", color: "#4eaa25" },
  fish: { icon: "💻 ", color: "#4eaa25" },
  ps1: { icon: "💻 ", color: "#012456" },

  // Documentation / Prose: 📝
  md: { icon: "📝 ", color: "#519aba" },
  markdown: { icon: "📝 ", color: "#519aba" },
  txt: { icon: "📄 ", color: "#89ddff" },
  pdf: { icon: "📕 ", color: "#b30b00" },

  // Others
  rb: { icon: "💎 ", color: "#701516" },
  php: { icon: "🐘 ", color: "#777bb4" },
  swift: { icon: "🕊 ", color: "#f05138" },
  dart: { icon: "🎯 ", color: "#00b4ab" },
  lua: { icon: "🌙 ", color: "#51a0cf" },
  zig: { icon: "⚡ ", color: "#f69a1b" },
  ex: { icon: "💧 ", color: "#6e4a7e" },
  exs: { icon: "💧 ", color: "#6e4a7e" },
  wasm: { icon: "🕸 ", color: "#654ff0" },
  svg: { icon: "🖼 ", color: "#ffb13b" },
  png: { icon: "🖼 ", color: "#a074c4" },
  jpg: { icon: "🖼 ", color: "#a074c4" },
  jpeg: { icon: "🖼 ", color: "#a074c4" },
  gif: { icon: "🖼 ", color: "#a074c4" },
  ico: { icon: "🖼 ", color: "#cbcb41" },
  lock: { icon: "🔒 ", color: "#e5c07b" },
}

/**
 * 解析文件或目录对应的标准 Unicode 图标与官方品牌专色。
 */
export function getFileIconInfo(
  name: string,
  kind: "directory" | "file" | "symlink",
  expanded = false,
): FileIconInfo {
  if (kind === "directory") {
    return {
      icon: expanded ? "📂 " : "📁 ",
      color: DIRECTORY_COLOR,
    }
  }

  if (kind === "symlink") {
    return {
      icon: "↪ ",
      color: SYMLINK_COLOR,
    }
  }

  const lowerName = name.toLowerCase()

  // 1. 检查特定完整文件名
  const exactMatch = EXACT_FILE_MAP[lowerName]
  if (exactMatch) return exactMatch

  // 2. 检查扩展名
  const dotIndex = lowerName.lastIndexOf(".")
  if (dotIndex !== -1 && dotIndex < lowerName.length - 1) {
    const ext = lowerName.slice(dotIndex + 1)
    const extMatch = EXTENSION_MAP[ext]
    if (extMatch) return extMatch
  }

  // 3. 常规默认文件
  return {
    icon: "📄 ",
    color: DEFAULT_FILE_COLOR,
  }
}
