/** 跨端共享语言目录：收敛 canonical ID、别名、TUI parser、Web 语言及降级规范。 */

export type LanguageCatalogEntry = {
  readonly canonical: string
  readonly aliases: readonly string[]
  readonly tuiParser: string
  readonly webLanguage: string
  readonly fallback: string
}

export const LANGUAGE_CATALOG: readonly LanguageCatalogEntry[] = [
  {
    canonical: "javascript",
    aliases: ["js", "mjs", "cjs"],
    tuiParser: "javascript",
    webLanguage: "javascript",
    fallback: "plaintext",
  },
  {
    canonical: "typescript",
    aliases: ["ts", "mts", "cts"],
    tuiParser: "typescript",
    webLanguage: "typescript",
    fallback: "plaintext",
  },
  {
    canonical: "jsx",
    aliases: [],
    tuiParser: "javascript",
    webLanguage: "jsx",
    fallback: "plaintext",
  },
  {
    canonical: "tsx",
    aliases: [],
    tuiParser: "typescript",
    webLanguage: "tsx",
    fallback: "plaintext",
  },
  {
    canonical: "json",
    aliases: ["jsonc"],
    tuiParser: "json",
    webLanguage: "json",
    fallback: "plaintext",
  },
  {
    canonical: "python",
    aliases: ["py"],
    tuiParser: "python",
    webLanguage: "python",
    fallback: "plaintext",
  },
  {
    canonical: "bash",
    aliases: ["sh", "shell", "zsh"],
    tuiParser: "bash",
    webLanguage: "bash",
    fallback: "plaintext",
  },
  {
    canonical: "go",
    aliases: [],
    tuiParser: "go",
    webLanguage: "go",
    fallback: "plaintext",
  },
  {
    canonical: "java",
    aliases: [],
    tuiParser: "java",
    webLanguage: "java",
    fallback: "plaintext",
  },
  {
    canonical: "c",
    aliases: ["h"],
    tuiParser: "c",
    webLanguage: "c",
    fallback: "plaintext",
  },
  {
    canonical: "cpp",
    aliases: ["c++", "cc", "cxx", "hpp", "hxx"],
    tuiParser: "cpp",
    webLanguage: "cpp",
    fallback: "plaintext",
  },
  {
    canonical: "html",
    aliases: ["htm"],
    tuiParser: "html",
    webLanguage: "html",
    fallback: "plaintext",
  },
  {
    canonical: "css",
    aliases: [],
    tuiParser: "css",
    webLanguage: "css",
    fallback: "plaintext",
  },
  {
    canonical: "yaml",
    aliases: ["yml"],
    tuiParser: "yaml",
    webLanguage: "yaml",
    fallback: "plaintext",
  },
  {
    canonical: "markdown",
    aliases: ["md"],
    tuiParser: "markdown",
    webLanguage: "markdown",
    fallback: "plaintext",
  },
  {
    canonical: "plaintext",
    aliases: ["text", "txt", "plain"],
    tuiParser: "plaintext",
    webLanguage: "plaintext",
    fallback: "plaintext",
  },
] as const

const PLAINTEXT_ENTRY: LanguageCatalogEntry = LANGUAGE_CATALOG.find(entry => entry.canonical === "plaintext")!

const aliasMap = new Map<string, LanguageCatalogEntry>()
for (const entry of LANGUAGE_CATALOG) {
  aliasMap.set(entry.canonical.toLowerCase(), entry)
  for (const alias of entry.aliases) {
    aliasMap.set(alias.toLowerCase(), entry)
  }
}

/** 按规范名称或别名查找匹配的 Catalog 条目，未找到或空白自动降级为 plaintext。 */
export function resolveLanguage(languageOrAlias?: string | null): LanguageCatalogEntry {
  if (!languageOrAlias) return PLAINTEXT_ENTRY
  const normalized = languageOrAlias.trim().toLowerCase()
  return aliasMap.get(normalized) ?? PLAINTEXT_ENTRY
}

/** 从逻辑文件路径的最后一个扩展名推导语言；目录名中的点不会误判。 */
export function resolveLanguageForPath(path?: string | null): LanguageCatalogEntry {
  if (!path) return PLAINTEXT_ENTRY
  const name = path.replaceAll("\\", "/").split("/").at(-1) ?? ""
  const dot = name.lastIndexOf(".")
  if (dot <= 0 || dot === name.length - 1) return PLAINTEXT_ENTRY
  return resolveLanguage(name.slice(dot + 1))
}
