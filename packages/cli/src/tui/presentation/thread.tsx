/** Harness Code 的 Thread 主视图。 */

import { Composer, FooterRail, ThreadRuntimeLine } from "./composer"
import { ConversationTimeline } from "./timeline"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

/** thread 流全宽渲染，工具和审批事件以左轨形成明确的操作时间线。 */
export function ThreadView(props: SharedViewProps & { modelName?: string }) {
  const interaction = props.interactive.interaction
  const blockingInteraction = Boolean(
    interaction?.type === "approval"
    || interaction?.type === "directory_trust"
    || (interaction?.type === "question" && interaction.questions[0]?.options.length),
  )

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
      <ConversationTimeline
        interactive={props.interactive}
        scrollRef={props.conversationScrollRef}
        showToolDetails={props.showToolDetails}
        expandedTools={props.expandedTools}
        onToggleTool={props.onToggleTool}
        onApproval={props.onApproval}
        onDirectoryTrust={props.onDirectoryTrust}
        onQuestion={props.onQuestion}
        modelName={props.modelName}
        transientNotice={props.transientNotice}
        terminalWidth={props.terminalWidth}
      />
      {!blockingInteraction ? (
        <box flexShrink={0} paddingLeft={2} paddingRight={2}>
          <ThreadRuntimeLine interactive={props.interactive} />
          <Composer {...props} variant="thread" commandMenuPlacement="above" />
        </box>
      ) : null}
      <FooterRail interactive={props.interactive} terminalWidth={props.terminalWidth} thread />
    </box>
  )
}
