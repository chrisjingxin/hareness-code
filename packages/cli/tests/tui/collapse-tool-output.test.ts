import { expect, test } from "bun:test"

import { collapseToolOutput } from "../../src/tui/upstream/collapse-tool-output"

test("短工具输出保持原样，不显示无意义的展开入口", () => {
  expect(collapseToolOutput("src/app.ts", 4, 360)).toEqual({ output: "src/app.ts", overflow: false })
})

test("长工具输出按行或字符安全截断，并保留展开标识", () => {
  expect(collapseToolOutput("1\n2\n3", 2, 100)).toEqual({ output: "1\n2\n…", overflow: true })
  expect(collapseToolOutput("一二三四五", 4, 4)).toEqual({ output: "一二三…", overflow: true })
})
