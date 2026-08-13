/** 绘制限额：用固定长文验证 head/tail 窗口，期望值不由实现回算。 */

import { expect, test } from "bun:test"

import { boundVisibleText, nextThinkingExpanded, thinkingVisibleBody, writeFileVisibleBody } from "../../src/presentation-shared/paint-budget"

const FIFTY_LINES = [
  "L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08", "L09", "L10",
  "L11", "L12", "L13", "L14", "L15", "L16", "L17", "L18", "L19", "L20",
  "L21", "L22", "L23", "L24", "L25", "L26", "L27", "L28", "L29", "L30",
  "L31", "L32", "L33", "L34", "L35", "L36", "L37", "L38", "L39", "L40",
  "L41", "L42", "L43", "L44", "L45", "L46", "L47", "L48", "L49", "L50",
].join("\n")

test("思考进行中：保留最后 12 行", () => {
  const result = boundVisibleText(FIFTY_LINES, { maxLines: 12, maxChars: 4096, keep: "tail" })
  expect(result).toEqual({
    text: "L39\nL40\nL41\nL42\nL43\nL44\nL45\nL46\nL47\nL48\nL49\nL50",
    overflow: true,
    hiddenLines: 38,
  })
})

test("思考点开：保留开头 40 行", () => {
  const result = boundVisibleText(FIFTY_LINES, { maxLines: 40, maxChars: 8192, keep: "head" })
  expect(result).toEqual({
    text: [
      "L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08", "L09", "L10",
      "L11", "L12", "L13", "L14", "L15", "L16", "L17", "L18", "L19", "L20",
      "L21", "L22", "L23", "L24", "L25", "L26", "L27", "L28", "L29", "L30",
      "L31", "L32", "L33", "L34", "L35", "L36", "L37", "L38", "L39", "L40",
    ].join("\n"),
    overflow: true,
    hiddenLines: 10,
  })
})

test("未超限时原文返回", () => {
  expect(boundVisibleText("a\nb", { maxLines: 12, maxChars: 4096, keep: "tail" })).toEqual({
    text: "a\nb",
    overflow: false,
    hiddenLines: 0,
  })
})

test("单行超长按码点截断：head 取前 4096，tail 取后 4096", () => {
  const long = "a".repeat(5000)
  const head = boundVisibleText(long, { maxLines: 12, maxChars: 4096, keep: "head" })
  expect(head.overflow).toBe(true)
  expect(head.hiddenLines).toBe(0)
  expect(head.text).toBe("a".repeat(4096))

  const tail = boundVisibleText(`HEAD${"b".repeat(5000)}`, { maxLines: 12, maxChars: 4096, keep: "tail" })
  expect(tail.overflow).toBe(true)
  expect(tail.text).toBe("b".repeat(4096))
})

const EIGHTY_LINES = Array.from({ length: 80 }, (_, index) => `T${String(index + 1).padStart(2, "0")}`).join("\n")

test("思考进行中只画最后 12 行", () => {
  const result = thinkingVisibleBody(EIGHTY_LINES, "live")
  expect(result.text).toBe("T69\nT70\nT71\nT72\nT73\nT74\nT75\nT76\nT77\nT78\nT79\nT80")
  expect(result.hiddenLines).toBe(68)
  expect(result.overflow).toBe(true)
})

test("思考点开最多 40 行", () => {
  const result = thinkingVisibleBody(EIGHTY_LINES, "expanded")
  expect(result.text.startsWith("T01\nT02")).toBe(true)
  expect(result.text.endsWith("T39\nT40")).toBe(true)
  expect(result.text.split("\n")).toHaveLength(40)
  expect(result.hiddenLines).toBe(40)
})

test("思考折叠不输出正文", () => {
  expect(thinkingVisibleBody(EIGHTY_LINES, "collapsed")).toEqual({
    text: "",
    overflow: true,
    hiddenLines: 80,
  })
})

test("思考进行中忽略折叠，完成后才切换展开", () => {
  expect(nextThinkingExpanded(true, true)).toBe(true)
  expect(nextThinkingExpanded(true, false)).toBe(true)
  expect(nextThinkingExpanded(false, false)).toBe(true)
  expect(nextThinkingExpanded(false, true)).toBe(false)
})

test("write_file 默认 12 行，展开最多 100 行", () => {
  const lines = Array.from({ length: 120 }, (_, index) => `W${String(index + 1).padStart(3, "0")}`).join("\n")
  const collapsed = writeFileVisibleBody(lines, false)
  expect(collapsed.text.split("\n")).toHaveLength(12)
  expect(collapsed.text.startsWith("W001")).toBe(true)
  expect(collapsed.hiddenLines).toBe(108)

  const expanded = writeFileVisibleBody(lines, true)
  expect(expanded.text.split("\n")).toHaveLength(100)
  expect(expanded.text.endsWith("W100")).toBe(true)
  expect(expanded.hiddenLines).toBe(20)
})

test("空文本不溢出", () => {
  expect(boundVisibleText("", { maxLines: 12, maxChars: 4096, keep: "tail" })).toEqual({
    text: "",
    overflow: false,
    hiddenLines: 0,
  })
})
