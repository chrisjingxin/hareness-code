import { expect, test } from "bun:test"

import { markdownSyntax } from "../../src/tui/theme"

test("Markdown 和代码高亮注册 OpenTUI 的真实 scope", () => {
  const names = markdownSyntax.getRegisteredNames()
  expect(names).toContain("markup.heading")
  expect(names).toContain("markup.raw.block")
  expect(names).toContain("keyword")
  expect(names).toContain("function")
  expect(names).toContain("string")
  expect(names).toContain("tag")
  expect(names).toContain("attribute")
})
