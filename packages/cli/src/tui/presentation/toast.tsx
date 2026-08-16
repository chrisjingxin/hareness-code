/** TUI 右上角统一气泡通知容器与条目组件（OpenTUI）。 */

import { useEffect, useState } from "react"
import { TextAttributes } from "@opentui/core"
import type { ReactNode } from "react"
import { tuiTheme } from "./theme"
import type { ToastItem, ToastVariant } from "../application/adapter"

export type { ToastItem, ToastVariant }

export type ToastContainerProps = {
  toasts: readonly ToastItem[]
  terminalWidth: number
}

/** 参考 MiMo-Code 的 SplitBorder 自定义边框字符：仅保留左右侧重竖线，去除上下封闭边框与拐角。 */
export const SPLIT_BORDER_CHARS = {
  topLeft: "",
  topRight: "",
  bottomLeft: "",
  bottomRight: "",
  horizontal: " ",
  vertical: "┃",
  topT: "",
  bottomT: "",
  leftT: "",
  rightT: "",
  cross: "",
} as const

function resolveToastVisuals(variant: ToastVariant): { icon: string; color: string } {
  switch (variant) {
    case "success":
      return { icon: "✓", color: tuiTheme.success }
    case "warning":
      return { icon: "⚠", color: tuiTheme.warning }
    case "error":
      return { icon: "✗", color: tuiTheme.danger }
    case "info":
    default:
      return { icon: "ℹ", color: tuiTheme.thinking }
  }
}

const ANIMATION_FRAME_MS = 20 // 50fps 超平滑帧率
const ENTER_DURATION_MS = 240 // 进场缓动时间
const EXIT_DURATION_MS = 220 // 退场缓动时间
const MAX_SLIDE_DISTANCE = 24 // 最大滑移距离（单元格数）

/** 单条气泡通知组件（支持从右侧平滑滑入、常态停泊与向右平滑滑出特效）。 */
function ToastBubble(props: { toast: ToastItem; maxWidth: number }): ReactNode {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now())
    }, ANIMATION_FRAME_MS)
    return () => clearInterval(timer)
  }, [])

  const elapsed = Math.max(0, now - props.toast.createdAtMs)
  const duration = props.toast.durationMs || 3000
  const remaining = Math.max(0, duration - elapsed)

  const { icon, color } = resolveToastVisuals(props.toast.variant)

  // 计算从右侧滑入/滑出的水平偏移量 (left 属性向右偏移)
  let slideOffset = 0
  if (elapsed < ENTER_DURATION_MS) {
    // 进场：从右侧向左平滑缓出减速滑入到 0 位置 (easeOutCubic)
    const t = elapsed / ENTER_DURATION_MS
    const easeOut = 1 - Math.pow(1 - t, 3)
    slideOffset = Math.round((1 - easeOut) * MAX_SLIDE_DISTANCE)
  } else if (remaining < EXIT_DURATION_MS) {
    // 退场：从 0 位置向右平滑缓入加速滑出到右侧边缘 (easeInCubic)
    const t = 1 - remaining / EXIT_DURATION_MS
    const easeIn = Math.pow(t, 3)
    slideOffset = Math.round(easeIn * MAX_SLIDE_DISTANCE)
  }

  return (
    <box
      position="relative"
      left={slideOffset}
      backgroundColor={tuiTheme.surfaceElevated}
      border={["left", "right"]}
      borderColor={color}
      customBorderChars={SPLIT_BORDER_CHARS}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="row"
      gap={1}
      alignItems="center"
      maxWidth={props.maxWidth}
    >
      <text fg={color} attributes={TextAttributes.BOLD}>
        {icon}
      </text>
      <text fg={tuiTheme.text} wrapMode="word">
        {props.toast.message}
      </text>
    </box>
  )
}

/**
 * 右上角气泡通知容器：绝对悬浮于屏幕右上角，拥有最高 zIndex，独立渲染队列（最多 3 条）。
 */
export function ToastContainer(props: ToastContainerProps): ReactNode {
  if (!props.toasts || props.toasts.length === 0) return null

  const maxBubbleWidth = Math.max(24, Math.min(56, Math.floor(props.terminalWidth * 0.45)))

  return (
    <box
      position="absolute"
      top={2}
      right={2}
      zIndex={120}
      flexDirection="column"
      gap={1}
      alignItems="flex-end"
    >
      {props.toasts.map(toast => (
        <ToastBubble key={toast.id} toast={toast} maxWidth={maxBubbleWidth} />
      ))}
    </box>
  )
}
