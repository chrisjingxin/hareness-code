/** BTW 临时问答浮层弹窗组件（OpenTUI）。 */

import type { MouseEvent } from "@opentui/core"
import { useRenderer } from "@opentui/react"
import type { ReactNode } from "react"
import { modeAccent, tuiTheme } from "./theme"
import { OverlayShell } from "./overlays"
import type { BtwState } from "../application/adapter"

export type { BtwState }

export type BtwModalProps = {
  visible: boolean
  question: string
  answer?: string
  modelProfileId?: string
  status: "loading" | "ready" | "error"
  error?: string
  copied?: boolean
  workMode?: "build" | "compose"
  terminalWidth: number
  terminalHeight: number
  onClose: () => void
  onCopy: () => void
}

/** 区分真实文本拖选与普通点击，避免选区复制和 BTW 回答复制重复执行。 */
export function handleBtwCopyMouseUp(
  event: Pick<MouseEvent, "stopPropagation">,
  hasTextDragSelection: boolean,
  onCopy: () => void,
): void {
  if (hasTextDragSelection) return
  event.stopPropagation()
  onCopy()
}

/**
 * 临时问答浮层：展示问题与轻量回复，不污染历史 Timeline，按 Esc/Enter 关闭，支持点击或快捷键复制回答。
 */
export function BtwModal(props: BtwModalProps): ReactNode {
  const renderer = useRenderer()
  if (!props.visible) return null
  const accent = modeAccent(props.workMode ?? "build")

  return (
    <OverlayShell terminalWidth={props.terminalWidth} terminalHeight={props.terminalHeight} placement="dialog" zIndex={105}>
      {({ width }: { width: number }) => (
        <box
          width={width}
          maxWidth="100%"
          backgroundColor={tuiTheme.menu}
          flexDirection="column"
          zIndex={1}
          paddingLeft={4}
          paddingRight={4}
          paddingTop={2}
          paddingBottom={2}
        >
          <box flexDirection="row" justifyContent="space-between" alignItems="center">
            <text fg={tuiTheme.text}>
              <strong>BTW 临时问答</strong>
            </text>
            {props.modelProfileId ? (
              <box backgroundColor={tuiTheme.panel} paddingLeft={1} paddingRight={1}>
                <text fg={tuiTheme.muted}>模型: {props.modelProfileId}</text>
              </box>
            ) : null}
          </box>

          <box paddingTop={1} paddingBottom={1}>
            <text fg={accent}>
              <strong>问题：</strong>{props.question}
            </text>
          </box>

          <box
            backgroundColor={tuiTheme.panel}
            paddingLeft={2}
            paddingRight={2}
            paddingTop={1}
            paddingBottom={1}
            maxHeight={16}
            flexDirection="column"
          >
            {props.status === "loading" ? (
              <text fg={tuiTheme.muted}>正在思考中，请稍候…</text>
            ) : props.status === "error" ? (
              <text fg={tuiTheme.danger}>问答失败：{props.error ?? "未知错误"}</text>
            ) : (
              <text fg={tuiTheme.text} wrapMode="word">
                {props.answer ?? "（无回答内容）"}
              </text>
            )}
          </box>

          <box paddingTop={2} flexDirection="row" justifyContent="space-between" alignItems="center">
            <text fg={tuiTheme.muted}>按 Esc 或 Enter 关闭</text>
            {props.status === "ready" ? (
              <box
                backgroundColor={accent}
                paddingLeft={2}
                paddingRight={2}
                onMouseUp={event => {
                  // OpenTUI 会为普通点击创建初始 selection，isDragging 不能区分拖选；仅已移动的选区 isStart 为 false。
                  const hasTextDragSelection = renderer.getSelection()?.isStart === false
                  handleBtwCopyMouseUp(event, hasTextDragSelection, props.onCopy)
                }}
              >
                <text fg={tuiTheme.background}>
                  <strong>复制</strong>
                </text>
              </box>
            ) : null}
          </box>
        </box>
      )}
    </OverlayShell>
  )
}
