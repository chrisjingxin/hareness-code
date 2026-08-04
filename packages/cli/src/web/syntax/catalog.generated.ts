// 此文件由 scripts/vendor-syntax-assets.ts 生成，请勿手动编辑。

export type WebCatalogEntry = {
  readonly filetype: string
  readonly aliases: readonly string[]
  readonly assetId: string
  readonly wasmFileName: string
}

export const bundledSyntaxLanguages: readonly WebCatalogEntry[] = [
  {
    filetype: "python",
    aliases: ["py"],
    assetId: "python",
    wasmFileName: "tree-sitter-python.wasm",
  },
  {
    filetype: "go",
    aliases: [],
    assetId: "go",
    wasmFileName: "tree-sitter-go.wasm",
  },
  {
    filetype: "cpp",
    aliases: ["c++", "cc", "cxx", "hpp", "hxx"],
    assetId: "cpp",
    wasmFileName: "tree-sitter-cpp.wasm",
  },
  {
    filetype: "bash",
    aliases: ["sh", "shell", "zsh"],
    assetId: "bash",
    wasmFileName: "tree-sitter-bash.wasm",
  },
  {
    filetype: "c",
    aliases: ["h"],
    assetId: "c",
    wasmFileName: "tree-sitter-c.wasm",
  },
  {
    filetype: "java",
    aliases: [],
    assetId: "java",
    wasmFileName: "tree-sitter-java.wasm",
  },
  {
    filetype: "html",
    aliases: ["htm"],
    assetId: "html",
    wasmFileName: "tree-sitter-html.wasm",
  },
  {
    filetype: "json",
    aliases: ["jsonc"],
    assetId: "json",
    wasmFileName: "tree-sitter-json.wasm",
  },
  {
    filetype: "yaml",
    aliases: ["yml"],
    assetId: "yaml",
    wasmFileName: "tree-sitter-yaml.wasm",
  },
  {
    filetype: "css",
    aliases: [],
    assetId: "css",
    wasmFileName: "tree-sitter-css.wasm",
  },
] as const

const aliasMap = new Map<string, WebCatalogEntry>()
for (const entry of bundledSyntaxLanguages) {
  aliasMap.set(entry.filetype.toLowerCase(), entry)
  for (const alias of entry.aliases) {
    aliasMap.set(alias.toLowerCase(), entry)
  }
}

/** 按名称或别名查找匹配的 Catalog 条目，未找到返回 null */
export function resolveSyntaxLanguage(languageOrAlias: string): WebCatalogEntry | null {
  if (!languageOrAlias) return null
  return aliasMap.get(languageOrAlias.trim().toLowerCase()) ?? null
}
