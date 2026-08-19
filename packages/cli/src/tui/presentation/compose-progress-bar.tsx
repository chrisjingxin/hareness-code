/** Compose 固定进度条：不进时间线滚动，步骤名宽度稳定。 */

import { TextAttributes } from "@opentui/core"

import type { ComposeProjection } from "../../interactive/state"
import type { InteractiveSnapshot } from "../../interactive/types"
import {
  composeStepperSegments,
  composeStepperTrackFilled,
  resolveComposeProgress,
  type ComposeStepperMark,
} from "../../presentation-shared/compose-progress-bar"
import { tuiTheme } from "./theme"

function stepIcon(mark: ComposeStepperMark): string {
  if (mark === "done") return "✓"
  if (mark === "current") return "●"
  if (mark === "failed") return "✕"
  if (mark === "skipped") return "–"
  return "○"
}

function iconColor(mark: ComposeStepperMark): string {
  if (mark === "done") return tuiTheme.success
  if (mark === "current") return tuiTheme.modeCompose
  if (mark === "failed") return tuiTheme.danger
  return tuiTheme.subtle
}

function labelColor(mark: ComposeStepperMark): string {
  if (mark === "done") return tuiTheme.text
  if (mark === "current") return tuiTheme.modeCompose
  if (mark === "failed") return tuiTheme.danger
  return tuiTheme.subtle
}

export function ComposeProgressBar(props: { interactive: InteractiveSnapshot }) {
  const progress = resolveComposeProgress(props.interactive)
  if (!progress) return null
  return <ComposeStepper state={progress} />
}

function ComposeStepper(props: { state: ComposeProjection }) {
  const segments = composeStepperSegments(props.state)
  return (
    <box
      flexShrink={0}
      flexDirection="row"
      alignItems="center"
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
      paddingBottom={0}
      border={["bottom"]}
      borderColor={tuiTheme.border}
    >
      <box flexDirection="row" alignItems="center" gap={1} flexShrink={0}>
        {segments.map((segment, index) => {
          const previous = segments[index - 1]
          const filled = previous ? composeStepperTrackFilled(previous.mark) : false
          const isCurrentOrFailed = segment.mark === "current" || segment.mark === "failed"
          return (
            <box key={segment.id} flexDirection="row" alignItems="center" gap={1} flexShrink={0}>
              {index > 0 ? (
                <text fg={filled ? tuiTheme.success : tuiTheme.border}>─</text>
              ) : null}
              <box flexDirection="row" alignItems="center" gap={1} flexShrink={0}>
                <text fg={iconColor(segment.mark)} attributes={isCurrentOrFailed ? TextAttributes.BOLD : undefined}>
                  {stepIcon(segment.mark)}
                </text>
                <text fg={labelColor(segment.mark)} attributes={isCurrentOrFailed ? TextAttributes.BOLD : undefined}>
                  {segment.label}
                </text>
              </box>
            </box>
          )
        })}
      </box>
    </box>
  )
}
