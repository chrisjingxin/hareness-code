/** 提及文本语法解析策略测试：从消息中提取普通路径、带引号路径、GitHub/Colon 行号切片。 */

import { expect, describe, test } from "bun:test"
import { parseMentionsFromText } from "../../src/presentation-shared/mention-parse-policy"

describe("parseMentionsFromText", () => {
  test("提取普通未带引号的文件提及", () => {
    const text = "请查看 @packages/cli/src/index.ts 这个入口文件"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: "@packages/cli/src/index.ts",
      path: "packages/cli/src/index.ts",
      lineStart: undefined,
      lineEnd: undefined,
    })
  })

  test("提取带双引号的空格路径", () => {
    const text = '请参考 @"docs/my test plan.md" 的内容'
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: '@"docs/my test plan.md"',
      path: "docs/my test plan.md",
      lineStart: undefined,
      lineEnd: undefined,
    })
  })

  test("提取 GitHub 标准行号范围 (#L20-50)", () => {
    const text = "解释 @src/app.tsx#L20-50 这段代码"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: "@src/app.tsx#L20-50",
      path: "src/app.tsx",
      lineStart: 20,
      lineEnd: 50,
    })
  })

  test("提取 GitHub 单行号 (#L42)", () => {
    const text = "检查 @src/app.tsx#L42 的空指针"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: "@src/app.tsx#L42",
      path: "src/app.tsx",
      lineStart: 42,
      lineEnd: 42,
    })
  })

  test("提取冒号行号范围 (:20-50) 与单行 (:42)", () => {
    const text = "对比 @src/a.ts:20-50 和 @src/b.ts:42"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(2)
    expect(mentions[0]).toEqual({
      raw: "@src/a.ts:20-50",
      path: "src/a.ts",
      lineStart: 20,
      lineEnd: 50,
    })
    expect(mentions[1]).toEqual({
      raw: "@src/b.ts:42",
      path: "src/b.ts",
      lineStart: 42,
      lineEnd: 42,
    })
  })

  test("带引号路径后附加行号", () => {
    const text = '看下 @"docs/my guide.md"#L10-20'
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: '@"docs/my guide.md"#L10-20',
      path: "docs/my guide.md",
      lineStart: 10,
      lineEnd: 20,
    })
  })

  test("行号倒序自动纠偏 (如 #L50-20 自动纠为 20-50)", () => {
    const text = "看下 @src/index.ts#L50-20"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(1)
    expect(mentions[0]).toEqual({
      raw: "@src/index.ts#L50-20",
      path: "src/index.ts",
      lineStart: 20,
      lineEnd: 50,
    })
  })

  test("邮箱地址不会被误解析为提及", () => {
    const text = "联系 support@domain.com 获取帮助"
    const mentions = parseMentionsFromText(text)
    expect(mentions).toHaveLength(0)
  })
})
