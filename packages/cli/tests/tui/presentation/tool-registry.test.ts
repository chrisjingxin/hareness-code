/** 工具名到四种 Renderer 的表驱动分流。 */

import { expect, test } from "bun:test"

import { resolveToolRenderer } from "../../../src/tui/presentation/tools/registry"

const cases: Array<[string, "inline" | "block" | "diff" | "generic"]> = [
  ["read_file", "inline"],
  ["Read", "inline"],
  ["read", "inline"],
  ["grep", "inline"],
  ["glob", "inline"],
  ["ls", "inline"],
  ["list", "inline"],
  ["webfetch", "inline"],
  ["web_fetch", "inline"],
  ["websearch", "inline"],
  ["web_search", "inline"],
  ["codesearch", "inline"],
  ["view_image", "inline"],
  ["read_file!", "inline"],
  ["execute", "block"],
  ["bash", "block"],
  ["exec", "block"],
  ["shell", "block"],
  ["Execute.", "block"],
  ["edit_file", "diff"],
  ["edit", "diff"],
  ["write_file", "diff"],
  ["write", "diff"],
  ["delete_file", "diff"],
  ["delete", "diff"],
  ["task", "generic"],
  ["unknown_plugin_tool", "generic"],
  ["", "generic"],
]

for (const [name, kind] of cases) {
  test(`resolveToolRenderer(${JSON.stringify(name)}) → ${kind}`, () => {
    expect(resolveToolRenderer(name)).toBe(kind)
  })
}
