# Third-Party Notices

## MiMo-Code and OpenCode

The terminal star-field algorithm and the character-raster Logo shimmer/sweep
presentation are adapted from MiMo-Code. The bundled syntax-parser configuration,
prompt-history persistence approach, and generic tool-output collapse helper are
derived from OpenCode (commit `05c3e40a4e641732b991499000ca479e5dad4b02`). Both
projects are licensed under MIT:

Copyright (c) 2026 MiMo Code, Xiaomi Corporation<br>
Copyright (c) 2025 opencode

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions: the above copyright notice and this
permission notice shall be included in all copies or substantial portions of
the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Tree-sitter syntax resources

The offline WASM parsers and highlight queries in
`packages/cli/src/tui/assets/syntax/` are version-pinned resources. Their exact
sources and SHA-256 digests are recorded in `manifest.json` in the same folder.

- Tree-sitter language parsers: MIT.
- tree-sitter-yaml: MIT.
- nvim-treesitter highlight queries: Apache-2.0.

No parser resource is fetched by the published CLI at runtime.
