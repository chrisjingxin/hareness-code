import { createHash } from "node:crypto"
import { mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { basename, dirname, relative, resolve } from "node:path"

import config from "./syntax-parsers.config.json" with { type: "json" }

type ParserConfig = {
  filetype: string
  aliases?: string[]
  wasm: string
  queries: { highlights: string[]; injections?: string[] }
  injectionMapping?: { nodeTypes?: Record<string, string>; infoStringMap?: Record<string, string> }
}

type ManifestEntry = {
  filetype: string
  wasm: { path: string; sha256: string; source: string }
  highlights: { path: string; sha256: string; sources: string[] }
  injections?: { path: string; sha256: string; sources: string[] }
}

const tuiRoot = resolve(import.meta.dir, "../src/tui")
const assetsRoot = resolve(tuiRoot, "assets/syntax")
const outputPath = resolve(tuiRoot, "generated-syntax-parsers.ts")
const manifestPath = resolve(assetsRoot, "manifest.json")

/**
 * 此脚本只在维护 parser 版本时运行。它会重新建立资源目录，避免已删除语言的
 * 二进制继续随 npm 包发布；日常 build 与 CLI 运行均不调用网络。
 */
async function main(): Promise<void> {
  const parsers = config.parsers as ParserConfig[]
  await rm(assetsRoot, { recursive: true, force: true })
  await mkdir(assetsRoot, { recursive: true })

  const manifest: ManifestEntry[] = []
  for (const parser of parsers) {
    const directory = resolve(assetsRoot, parser.filetype)
    const wasmPath = resolve(directory, basename(new URL(parser.wasm).pathname))
    await download(parser.wasm, wasmPath)
    const highlights = await materializeQueries(parser.queries.highlights, directory, "highlights.scm")
    const injections = parser.queries.injections?.length
      ? await materializeQueries(parser.queries.injections, directory, "injections.scm")
      : undefined

    manifest.push({
      filetype: parser.filetype,
      wasm: { path: relative(assetsRoot, wasmPath), sha256: await sha256(wasmPath), source: parser.wasm },
      highlights: { path: relative(assetsRoot, highlights.path), sha256: await sha256(highlights.path), sources: parser.queries.highlights },
      ...(injections ? { injections: { path: relative(assetsRoot, injections.path), sha256: await sha256(injections.path), sources: parser.queries.injections! } } : {}),
    })
  }

  await writeFile(manifestPath, `${JSON.stringify({ version: 1, parsers: manifest }, null, 2)}\n`, "utf8")
  await writeFile(outputPath, renderGeneratedParsers(parsers), "utf8")
}

/** 下载阶段保留系统 TLS 校验；企业代理异常应在维护环境解决，不能降低 CLI 运行时安全性。 */
async function download(source: string, destination: string): Promise<void> {
  await mkdir(dirname(destination), { recursive: true })
  const process = Bun.spawn(["curl", "--fail", "--location", "--silent", "--show-error", "--output", destination, source], {
    stdout: "inherit",
    stderr: "inherit",
  })
  const exitCode = await process.exited
  if (exitCode !== 0) throw new Error(`下载失败（${exitCode}）：${source}`)
}

async function materializeQueries(sources: readonly string[], directory: string, filename: string): Promise<{ path: string }> {
  const queries: string[] = []
  for (const [index, source] of sources.entries()) {
    if (source.startsWith("./")) {
      queries.push(await readFile(resolve(import.meta.dir, source), "utf8"))
      continue
    }
    const temporary = resolve(directory, `${filename}.${index}.tmp`)
    await download(source, temporary)
    queries.push(await readFile(temporary, "utf8"))
    await rm(temporary)
  }
  const path = resolve(directory, filename)
  await writeFile(path, queries.join("\n\n"), "utf8")
  return { path }
}

async function sha256(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex")
}

function renderGeneratedParsers(parsers: readonly ParserConfig[]): string {
  const imports = parsers.flatMap(parser => {
    const id = parser.filetype.replaceAll(/[^a-zA-Z0-9]/g, "_")
    const directory = `./assets/syntax/${parser.filetype}`
    const wasm = `${directory}/${basename(new URL(parser.wasm).pathname)}`
    return [
      `import ${id}Wasm from "${wasm}" with { type: "file" }`,
      `import ${id}Highlights from "${directory}/highlights.scm" with { type: "file" }`,
      ...(parser.queries.injections?.length ? [`import ${id}Injections from "${directory}/injections.scm" with { type: "file" }`] : []),
    ]
  }).join("\n")
  const definitions = parsers.map(parser => {
    const id = parser.filetype.replaceAll(/[^a-zA-Z0-9]/g, "_")
    const aliases = parser.aliases?.length ? `aliases: ${JSON.stringify(parser.aliases)}, ` : ""
    const injections = parser.queries.injections?.length ? `, injections: [${id}Injections]` : ""
    const mapping = parser.injectionMapping ? `, injectionMapping: ${JSON.stringify(parser.injectionMapping)}` : ""
    return `  { filetype: "${parser.filetype}", ${aliases}wasm: ${id}Wasm, queries: { highlights: [${id}Highlights]${injections} }${mapping} },`
  }).join("\n")
  return `// 此文件由 scripts/vendor-syntax-assets.ts 生成，请勿手动编辑。\nimport type { FiletypeParserOptions } from "@opentui/core"\n\n${imports}\n\nexport const bundledSyntaxParsers = [\n${definitions}\n] as const satisfies readonly FiletypeParserOptions[]\n`
}

await main()
