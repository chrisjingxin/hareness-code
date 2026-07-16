/** TUI 动画颜色工具：统一处理 OpenTUI 归一化 RGBA 通道。 */

import { RGBA } from "@opentui/core"

/**
 * OpenTUI 的 RGBA 通道 getter 返回 0~1 的归一化值；动画组件必须使用
 * fromValues，而不是接收 0~255 整数的 fromInts，否则颜色会被截断为近黑色。
 */
/** 在两个颜色之间进行受限线性插值，避免把归一化通道误传给整数构造器。 */
export function blendRgba(from: RGBA, to: RGBA, amount: number): RGBA {
  const safeAmount = Math.max(0, Math.min(1, amount))
  return RGBA.fromValues(
    from.r + (to.r - from.r) * safeAmount,
    from.g + (to.g - from.g) * safeAmount,
    from.b + (to.b - from.b) * safeAmount,
  )
}
