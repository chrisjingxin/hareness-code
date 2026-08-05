/** 共享语义 tone 枚举与映射的契约测试。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { SEMANTIC_TONES, toneLabel, type SemanticTone } from "../../src/presentation-shared/semantic-tone"

test("语义枚举覆盖两端使用的全部语义", () => {
  expect(SEMANTIC_TONES).toEqual(["default", "muted", "accent", "success", "warning", "danger"])
})

test("toneLabel 为每个语义提供稳定中文名", () => {
  const labels = SEMANTIC_TONES.map(tone => toneLabel(tone))
  expect(labels).toContain("默认")
  expect(labels).toContain("次要")
  expect(labels).toContain("强调")
  expect(labels).toContain("成功")
  expect(labels).toContain("警告")
  expect(labels).toContain("危险")
})

test("未知语义回退到默认名，不抛错", () => {
  expect(toneLabel("info" as SemanticTone)).toBe("默认")
})

test("两端共有的语义色键必须全部来自 SEMANTIC_TONES，防止命名漂移", async () => {
  const theme = await readFile(resolve(import.meta.dir, "../../src/tui/presentation/theme.ts"), "utf8")
  const styles = await readFile(resolve(import.meta.dir, "../../src/web/presentation/styles.css"), "utf8")
  const tuiKeys = new Set([...theme.matchAll(/^  (\w+): "#/gm)].map(match => match[1]!))
  const webKeys = new Set([...styles.matchAll(/^\s*--([a-z-]+):/gm)].map(match => match[1]!))
  const commonSemantic = [...tuiKeys].filter(key => webKeys.has(key) && SEMANTIC_TONES.includes(key as SemanticTone))
  // 两端语义键交集恰好是 4 个核心语义；新增共有语义必须先进入 SEMANTIC_TONES 并同步两端。
  expect(commonSemantic.sort()).toEqual(["danger", "muted", "success", "warning"])
})
