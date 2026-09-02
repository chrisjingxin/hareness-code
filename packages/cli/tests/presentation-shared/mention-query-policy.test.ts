/** 共享提及词法触发策略测试：光标处 @ 识别、查询词提取与替换范围判定。 */

import { expect, describe, test } from "bun:test"
import { extractMentionQuery } from "../../src/presentation-shared/mention-query-policy"

describe("extractMentionQuery", () => {
  test("光标位于 @ 紧邻右侧时激活触发，query 为空", () => {
    const result = extractMentionQuery("@", 1)
    expect(result).toEqual({
      active: true,
      query: "",
      start: 0,
      end: 1,
      isQuoted: false,
    })
  })

  test("前面有普通文本时正确识别 @ 后缀", () => {
    const text = "hello @app"
    const result = extractMentionQuery(text, text.length)
    expect(result).toEqual({
      active: true,
      query: "app",
      start: 6,
      end: 10,
      isQuoted: false,
    })
  })

  test("光标位于引号内的 @ 触发项", () => {
    const text = 'look @"src/my file'
    const result = extractMentionQuery(text, text.length)
    expect(result).toEqual({
      active: true,
      query: "src/my file",
      start: 5,
      end: 18,
      isQuoted: true,
    })
  })

  test("光标位于已闭合引号的 @ 外部时应处于非激活或已闭合状态", () => {
    const text = 'look @"src/file.ts" '
    const result = extractMentionQuery(text, text.length)
    expect(result.active).toBe(false)
  })

  test("邮箱地址或单词中间的 @ 不触发", () => {
    const text = "alice@example.com"
    const result = extractMentionQuery(text, text.length)
    expect(result.active).toBe(false)
  })

  test("普通无 @ 文本返回未激活", () => {
    const text = "hello world"
    const result = extractMentionQuery(text, text.length)
    expect(result.active).toBe(false)
  })

  test("光标在输入中间的 @query 处", () => {
    const text = "check @app and more"
    // 光标位于 '@app' 后面（offset 10）
    const result = extractMentionQuery(text, 10)
    expect(result).toEqual({
      active: true,
      query: "app",
      start: 6,
      end: 10,
      isQuoted: false,
    })
  })

  test("光标在 @ 前面时不触发", () => {
    const text = "check @app"
    // 光标在 '@' 之前（offset 6）
    const result = extractMentionQuery(text, 6)
    expect(result.active).toBe(false)
  })
})
