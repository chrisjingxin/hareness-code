/** Harness Code 的 Thread 主视图。 */

import { ApprovalDock, DirectoryTrustDock, GoalDock, PlanDock, QuestionDock, bottomAreaKind } from "./bottom-area"
import { ComposeProgressBar } from "./compose-progress-bar"
import { InputBar, FooterRail, ThreadRuntimeLine } from "./input-bar"
import { ConversationTimeline } from "./timeline"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

/** thread 流全宽渲染；Compose 进度钉在时间线上方，不随滚动移动。 */
export function ThreadView(props: SharedViewProps & { modelName?: string }) {
  const interaction = props.interactive.interaction
  const slot = bottomAreaKind(interaction)
  const isChild = Boolean(props.interactive.childTimelineExecutionId)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
      {slot === "plan" ? null : isChild ? (
        <box
          paddingLeft={2}
          paddingRight={2}
          paddingTop={1}
          paddingBottom={1}
          backgroundColor={tuiTheme.surface}
          flexDirection="row"
          gap={2}
        >
          <text fg={tuiTheme.primary}>子代理时间线</text>
          <text fg={tuiTheme.muted}>按 Backspace 或 Esc 返回主对话</text>
        </box>
      ) : (
        <ComposeProgressBar interactive={props.interactive} />
      )}
      {props.interactive.goal || props.interactive.goalPending ? (
        <box paddingLeft={2} paddingRight={2} backgroundColor={tuiTheme.surface} flexDirection="column">
          <box flexDirection="row" gap={1}>
            <text fg={tuiTheme.primary}>目标</text>
            <text
              content={props.interactive.goal
                ? `${props.interactive.goal.status} · r${props.interactive.goal.revision} · ${props.interactive.goal.objective}`
                : `准备中 · ${props.interactive.goalPending?.input_text ?? ""}`}
              fg={tuiTheme.muted}
            />
          </box>
          {props.interactive.goal?.note ? <text content={`备注：${props.interactive.goal.note}`} fg={tuiTheme.muted} /> : null}
          {props.interactive.goalPending ? <text content={`待处理：${props.interactive.goalPending.status} · ${props.interactive.goalPending.input_text}`} fg={tuiTheme.muted} /> : null}
          {props.interactive.goalActivities.length ? <text content={`最近活动：${props.interactive.goalActivities.at(-1)?.summary ?? ""}`} fg={tuiTheme.muted} /> : null}
        </box>
      ) : null}
      {slot === "plan" ? null : (
        <ConversationTimeline
          interactive={props.interactive}
          scrollRef={props.conversationScrollRef}
          showToolDetails={props.showToolDetails}
          expandedTools={props.expandedTools}
          onToggleTool={props.onToggleTool}
          onOpenChildTimeline={props.onOpenChildTimeline}
          modelName={props.modelName}
          transientNotice={props.transientNotice}
          terminalWidth={props.terminalWidth}
        />
      )}
      {slot === "approval" && interaction?.type === "approval" ? (
        <ApprovalDock
          interaction={interaction}
          workMode={props.interactive.workMode}
          terminalWidth={props.terminalWidth}
          onApproval={props.onApproval}
        />
      ) : null}
      {slot === "directory_trust" && interaction?.type === "directory_trust" ? (
        <DirectoryTrustDock
          interaction={interaction}
          workMode={props.interactive.workMode}
          onDirectoryTrust={props.onDirectoryTrust}
        />
      ) : null}
      {slot === "plan" && interaction?.type === "plan" ? (
        <PlanDock
          interaction={interaction}
          workMode={props.interactive.workMode}
          terminalHeight={props.terminalHeight}
          onPlan={props.onPlan}
          onClose={props.onPlanViewClose}
        />
      ) : null}
      {slot === "question" && interaction?.type === "question" ? (
        <QuestionDock
          key={interaction.requestId}
          interaction={interaction}
          workMode={props.interactive.workMode}
          onQuestion={props.onQuestion}
        />
      ) : null}
      {slot === "goal" && interaction?.type === "goal" ? (
        <GoalDock interaction={interaction} workMode={props.interactive.workMode} onGoal={props.onGoal} onClose={props.onGoalViewClose} />
      ) : null}
      {slot === "input" && !isChild ? (
        <box flexShrink={0} paddingLeft={2} paddingRight={2}>
          <ThreadRuntimeLine interactive={props.interactive} inputMode={props.inputMode} />
          <InputBar {...props} variant="thread" commandMenuPlacement="above" />
        </box>
      ) : null}
      <FooterRail
        interactive={props.interactive}
        terminalWidth={props.terminalWidth}
        thread
        sidebarVisible={props.sidebarVisible}
        onToggleSidebar={props.onToggleSidebar}
      />
    </box>
  )
}
