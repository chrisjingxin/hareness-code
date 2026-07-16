/** Harness Code 品牌字标：使用 OpenTUI shade 栅格实现可降级的像素 Logo。 */

import { fonts, RGBA, TextAttributes } from "@opentui/core"
import { useEffect, useMemo, useState } from "react"

import { tuiTheme } from "./theme"
import { blendRgba } from "./colors"

type ShadeFont = {
  lines: number
  letterspace: string[]
  chars: Record<string, string[]>
}

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

const shadeFont = fonts.shade as ShadeFont
const fullShape = createShape("HARNESS", "CODE")

/** MiMo 的 Logo 是字符栅格而非图片；沿用这一表达方式以保证各终端一致。 */
export function HarnessCodeLogo(props: { compact: boolean }) {
  if (props.compact) {
    return (
      <box width={22} height={2} position="relative" flexShrink={0}>
        <text fg={tuiTheme.primary} selectable={false}>HARNESS CODE</text>
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

/** 以定时器驱动 shimmer，并将 powered by 锚定在完整字标右下角。 */
function AnimatedWordmark(props: { shape: LogoShape }) {
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), FRAME_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])

  const lines = useMemo(() => props.shape.full.map((line, y) => renderLine(line, y, now)), [now, props.shape.full])
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
function renderLine(line: string, y: number, now: number) {
  const primary = RGBA.fromHex(tuiTheme.primary)
  const shadow = RGBA.fromHex(tuiTheme.primarySoft)
  const peak = RGBA.fromInts(229, 241, 255)
  return Array.from(line).map((char, x) => {
    if (char === " ") return <text key={x} selectable={false}> </text>
    const shimmer = shimmerStrength(x, y, line.length, now)
    const foreground = blendRgba(primary, peak, shimmer)
    const background = char === "█" ? blendRgba(shadow, primary, Math.min(0.72, 0.18 + shimmer * 0.5)) : undefined
    const muted = char === "░" || char === "▒" || char === "▓"
    return (
      <text
        key={x}
        fg={muted ? blendRgba(shadow, primary, shimmer * 0.26) : foreground}
        bg={background}
        attributes={TextAttributes.BOLD}
        selectable={false}
      >
        {char === "█" ? "▀" : char}
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

/** 拼接 HARNESS 与 CODE 两个 shade 字形区域。 */
function createShape(left: string, right: string): LogoShape {
  const leftRows = rasterize(left)
  const rightRows = rasterize(right)
  return {
    left: leftRows,
    right: rightRows,
    full: leftRows.map((line, index) => `${line}${" ".repeat(GAP)}${rightRows[index] ?? ""}`),
  }
}

/** 将字体行数据转换为不含 OpenTUI 颜色标签的纯栅格。 */
function rasterize(text: string): string[] {
  return Array.from({ length: shadeFont.lines }, (_, row) => text
    .split("")
    .map(char => stripColorTags(shadeFont.chars[char]?.[row] ?? ""))
    .join(stripColorTags(shadeFont.letterspace[row] ?? " ")))
}

/** 删除字体资源中的颜色标签，颜色由 Harness 主题统一控制。 */
function stripColorTags(value: string): string {
  return value.replace(/<\/?c\d+>/g, "")
}
