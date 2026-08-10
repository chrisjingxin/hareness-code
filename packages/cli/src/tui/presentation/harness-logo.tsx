/** Harness Code 品牌字标：使用 OpenTUI 栅格实现可降级的像素 Logo。 */

import { RGBA, TextAttributes } from "@opentui/core"
import { useEffect, useMemo, useState } from "react"

import { tuiTheme } from "./theme"
import { blendRgba } from "./colors"

type LogoShape = {
  left: string[]
  right: string[]
  full: string[]
}

const FRAME_INTERVAL_MS = 50
const SHIMMER_PERIOD_MS = 4_600
const SWEEP_INTERVAL_MS = 10_000
const SWEEP_DURATION_MS = 1_900
const GAP = 1

/** 参考 MiMo 官方 logoThin 的三行半块字符构型，确保与 MiMo Code 品牌字形完全对齐。 */
const MIMO_THIN_STYLE_FONT: Record<string, string[]> = {
  H: ["█  █", "█▀▀█", "▀  ▀"],
  A: ["█▀▀█", "█▀▀█", "▀  ▀"],
  R: ["█▀▀▄", "█▀▀▄", "▀  ▀"],
  N: ["█▄ █", "█ ▀█", "▀  ▀"],
  E: ["█▀▀▀", "█▀▀ ", "▀▀▀▀"],
  S: ["▄▀▀▀", " ▀▀▄", "▀▀▀ "],
  C: ["█▀▀▀", "█   ", "▀▀▀▀"],
  O: ["▄▀▀▄", "█  █", " ▀▀ "],
  D: ["█▀▀▄", "█  █", "▀▀▀ "],
}

const fullShape = createShape("HARNESS", "CODE")

/** Harness Code 品牌字标：使用硬朗方块像素字形实现。 */
export function HarnessCodeLogo(props: { compact: boolean }) {
  if (props.compact) {
    return (
      <box width={22} height={2} position="relative" flexShrink={0}>
        <text selectable={false}>
          <span fg={tuiTheme.primary}>HARNESS </span>
          <span fg={tuiTheme.text}>CODE</span>
        </text>
        <box position="absolute" right={0} bottom={0}>
          <PoweredBy />
        </box>
      </box>
    )
  }

  return <AnimatedWordmark shape={fullShape} />
}

/** 供首页布局和伪终端回归使用，避免字标宽度与 powered by 的锚点漂移。 */
export const HARNESS_WORDMARK_DIMENSIONS = {
  width: fullShape.full[0]?.length ?? 0,
  height: fullShape.full.length + 2,
}

const HARNESS_LEFT_WIDTH = fullShape.left[0]?.length ?? 0

/** 以定时器驱动 shimmer，并将 powered by 锚定在完整字标右下角。 */
function AnimatedWordmark(props: { shape: LogoShape }) {
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), FRAME_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])

  const lines = useMemo(() => props.shape.full.map((line, y) => renderLine(line, y, now, HARNESS_LEFT_WIDTH)), [now, props.shape.full])
  return (
    <box width={HARNESS_WORDMARK_DIMENSIONS.width} height={HARNESS_WORDMARK_DIMENSIONS.height} position="relative" flexDirection="column" flexShrink={0}>
      {lines.map((line, index) => <box key={index} flexDirection="row" height={1}>{line}</box>)}
      <box position="absolute" right={0} bottom={0}>
        <PoweredBy />
      </box>
    </box>
  )
}

/** 渲染技术品牌小字，避免参与主 Logo 的 flow 布局。 */
function PoweredBy() {
  return (
    <text fg={tuiTheme.muted} selectable={false}>
      powered by <span fg={tuiTheme.primary}>za38</span>
    </text>
  )
}

/** 将单行字形栅格映射到冷白与 za38 蓝色层，并应用 MiMo 风格的呼吸与扫光算法。 */
function renderLine(line: string, y: number, now: number, leftWidth: number) {
  const bluePrimary = RGBA.fromHex(tuiTheme.primary)
  const whitePrimary = RGBA.fromHex(tuiTheme.text)
  const peak = RGBA.fromInts(229, 241, 255)

  return Array.from(line).map((char, x) => {
    if (char === " ") return <text key={x} selectable={false}> </text>

    const isHarness = x < leftWidth
    const primary = isHarness ? bluePrimary : whitePrimary

    const shimmer = shimmerStrength(x, y, line.length, now)
    const foreground = blendRgba(primary, peak, shimmer)

    return (
      <text
        key={x}
        fg={foreground}
        attributes={TextAttributes.BOLD}
        selectable={false}
      >
        {char}
      </text>
    )
  })
}

/** 计算低频呼吸与横向 sweep 的合成亮度。 */
function shimmerStrength(x: number, y: number, width: number, now: number): number {
  const phase = (now % SHIMMER_PERIOD_MS) / SHIMMER_PERIOD_MS
  const ambient = 0.07 + Math.max(0, Math.sin(phase * Math.PI * 2 + x * 0.31 + y * 0.72)) * 0.14
  const sweepAge = now % SWEEP_INTERVAL_MS
  if (sweepAge > SWEEP_DURATION_MS) return ambient
  const center = (sweepAge / SWEEP_DURATION_MS) * (width + 10) - 5
  const distance = Math.abs(x - center)
  const sweep = Math.max(0, 1 - distance / 5)
  return Math.min(1, ambient + sweep * sweep * 0.82)
}

/** 拼接 HARNESS 与 CODE 两个半块字形区域。 */
function createShape(left: string, right: string): LogoShape {
  const leftRows = rasterizeWord(left)
  const rightRows = rasterizeWord(right)
  return {
    left: leftRows,
    right: rightRows,
    full: leftRows.map((line, index) => `${line}${" ".repeat(GAP)}${rightRows[index] ?? ""}`),
  }
}

/** 提取 3 行半块字栅格，利用终端字符的上下半格避免标题显得纵向拉长。 */
function rasterizeWord(text: string): string[] {
  const rows: string[] = []
  for (let r = 0; r < 3; r++) {
    rows.push(text.split("").map(char => MIMO_THIN_STYLE_FONT[char]?.[r] ?? "    ").join(" "))
  }
  return rows
}
