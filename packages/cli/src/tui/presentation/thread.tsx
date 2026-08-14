/** Harness Code 的 Thread 主视图。 */

import { ApprovalDock, DirectoryTrustDock, QuestionDock, bottomAreaKind } from "./bottom-area"
import { InputBar, FooterRail, ThreadRuntimeLine } from "./input-bar"
import { ConversationTimeline } from "./timeline"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

/** thread 流全宽渲染；底部互斥为输入栏 / 审批 Dock / 目录信任 Dock / 问答 Dock。Compose 与 Build 同一套骨架，不挂阶段顶栏。 */
export function ThreadView(props: SharedViewProps & { modelName?: string }) {
  const interaction = props.interactive.interaction
  const slot = bottomAreaKind(interaction)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
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
          interaction={interaction}
          workMode={props.interactive.workMode}
          onQuestion={props.onQuestion}
        />
      ) : null}
      {slot === "input" ? (
        <box flexShrink={0} paddingLeft={2} paddingRight={2}>
          <ThreadRuntimeLine interactive={props.interactive} />
          <InputBar {...props} variant="thread" commandMenuPlacement="above" />
        </box>
      ) : null}
      <FooterRail interactive={props.interactive} terminalWidth={props.terminalWidth} thread />
    </box>
  )
}
