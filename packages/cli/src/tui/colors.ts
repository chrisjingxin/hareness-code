import { RGBA } from "@opentui/core"

/**
 * OpenTUI 的 RGBA 通道 getter 返回 0~1 的归一化值；动画组件必须使用
 * fromValues，而不是接收 0~255 整数的 fromInts，否则颜色会被截断为近黑色。
 */
export function blendRgba(from: RGBA, to: RGBA, amount: number): RGBA {
  const safeAmount = Math.max(0, Math.min(1, amount))
  return RGBA.fromValues(
    from.r + (to.r - from.r) * safeAmount,
    from.g + (to.g - from.g) * safeAmount,
    from.b + (to.b - from.b) * safeAmount,
  )
}
