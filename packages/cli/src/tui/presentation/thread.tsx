/** Harness Code 的 Thread 主视图。 */

import { Composer, FooterRail, ThreadRuntimeLine } from "./composer"
import { ConversationTimeline } from "./timeline"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

/** thread 流全宽渲染，工具和审批事件以左轨形成明确的操作时间线。 */
export function ThreadView(props: SharedViewProps & { modelName?: string }) {
  const blockingInteraction = Boolean(props.state.pendingApproval || props.state.pendingQuestion?.options.length)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
      <ConversationTimeline
        state={props.state}
        scrollRef={props.conversationScrollRef}
        showToolDetails={props.showToolDetails}
        expandedTools={props.expandedTools}
        onToggleTool={props.onToggleTool}
        onApproval={props.onApproval}
        onQuestion={props.onQuestion}
        modelName={props.modelName}
      />
      {!blockingInteraction ? (
        <box flexShrink={0} paddingLeft={2} paddingRight={2}>
          <ThreadRuntimeLine runtime={props.runtime} state={props.state} />
          <Composer {...props} variant="thread" commandMenuPlacement="above" />
        </box>
      ) : null}
      <FooterRail runtime={props.runtime} state={props.state} terminalWidth={props.terminalWidth} thread />
    </box>
  )
}

