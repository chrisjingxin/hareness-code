/** Harness Code 的 Thread 主视图。 */

import { ApprovalDock, DirectoryTrustDock, QuestionDock, bottomAreaKind } from "./bottom-area"
import { ComposeProgressBar } from "./compose-progress-bar"
import { InputBar, FooterRail, ThreadRuntimeLine } from "./input-bar"
import { ConversationTimeline } from "./timeline"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

/** thread 流全宽渲染；Compose 进度钉在时间线上方，不随滚动移动。 */
export function ThreadView(props: SharedViewProps & { modelName?: string }) {
  const interaction = props.interactive.interaction
  const slot = bottomAreaKind(interaction)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
      <ComposeProgressBar interactive={props.interactive} />
      <ConversationTimeline
        interactive={props.interactive}
        scrollRef={props.conversationScrollRef}
        showToolDetails={props.showToolDetails}
        expandedTools={props.expandedTools}
        onToggleTool={props.onToggleTool}
        modelName={props.modelName}
        transientNotice={props.transientNotice}
        terminalWidth={props.terminalWidth}
      />
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
      {slot === "question" && interaction?.type === "question" ? (
        <QuestionDock
          key={interaction.requestId}
          interaction={interaction}
          workMode={props.interactive.workMode}
          onQuestion={props.onQuestion}
        />
      ) : null}
      {slot === "input" ? (
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
