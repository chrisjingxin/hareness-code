// 此文件由 scripts/vendor-syntax-assets.ts 生成，请勿手动编辑。
import type { FiletypeParserOptions } from "@opentui/core"

import bashWasm from "./assets/syntax/bash/tree-sitter-bash.wasm" with { type: "file" }
import bashHighlights from "./assets/syntax/bash/highlights.scm" with { type: "file" }
import cWasm from "./assets/syntax/c/tree-sitter-c.wasm" with { type: "file" }
import cHighlights from "./assets/syntax/c/highlights.scm" with { type: "file" }
import cppWasm from "./assets/syntax/cpp/tree-sitter-cpp.wasm" with { type: "file" }
import cppHighlights from "./assets/syntax/cpp/highlights.scm" with { type: "file" }
import cssWasm from "./assets/syntax/css/tree-sitter-css.wasm" with { type: "file" }
import cssHighlights from "./assets/syntax/css/highlights.scm" with { type: "file" }
import goWasm from "./assets/syntax/go/tree-sitter-go.wasm" with { type: "file" }
import goHighlights from "./assets/syntax/go/highlights.scm" with { type: "file" }
import htmlWasm from "./assets/syntax/html/tree-sitter-html.wasm" with { type: "file" }
import htmlHighlights from "./assets/syntax/html/highlights.scm" with { type: "file" }
import htmlInjections from "./assets/syntax/html/injections.scm" with { type: "file" }
import javaWasm from "./assets/syntax/java/tree-sitter-java.wasm" with { type: "file" }
import javaHighlights from "./assets/syntax/java/highlights.scm" with { type: "file" }
import jsonWasm from "./assets/syntax/json/tree-sitter-json.wasm" with { type: "file" }
import jsonHighlights from "./assets/syntax/json/highlights.scm" with { type: "file" }
import pythonWasm from "./assets/syntax/python/tree-sitter-python.wasm" with { type: "file" }
import pythonHighlights from "./assets/syntax/python/highlights.scm" with { type: "file" }
import yamlWasm from "./assets/syntax/yaml/tree-sitter-yaml.wasm" with { type: "file" }
import yamlHighlights from "./assets/syntax/yaml/highlights.scm" with { type: "file" }

export const bundledSyntaxParsers = [
  { filetype: "python", aliases: ["py"], wasm: pythonWasm, queries: { highlights: [pythonHighlights] } },
  { filetype: "go", wasm: goWasm, queries: { highlights: [goHighlights] } },
  { filetype: "cpp", aliases: ["c++", "cc", "cxx", "hpp", "hxx"], wasm: cppWasm, queries: { highlights: [cppHighlights] } },
  { filetype: "bash", aliases: ["sh", "shell", "zsh"], wasm: bashWasm, queries: { highlights: [bashHighlights] } },
  { filetype: "c", aliases: ["h"], wasm: cWasm, queries: { highlights: [cHighlights] } },
  { filetype: "java", wasm: javaWasm, queries: { highlights: [javaHighlights] } },
  {
    filetype: "html",
    aliases: ["htm"],
    wasm: htmlWasm,
    queries: { highlights: [htmlHighlights], injections: [htmlInjections] },
    injectionMapping: { nodeTypes: { style_element: "css", raw_text: "javascript" } },
  },
  { filetype: "json", aliases: ["jsonc"], wasm: jsonWasm, queries: { highlights: [jsonHighlights] } },
  { filetype: "yaml", aliases: ["yml"], wasm: yamlWasm, queries: { highlights: [yamlHighlights] } },
  { filetype: "css", wasm: cssWasm, queries: { highlights: [cssHighlights] } },
] as const satisfies readonly FiletypeParserOptions[]
