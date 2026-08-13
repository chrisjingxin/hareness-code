/** Thread 消息、工具和 Interaction 的统一时间线。 */

import { TextAttributes, type ScrollBoxRenderable } from "@opentui/core"
import { type ReactNode, type RefObject, useMemo, useState } from "react"

import type { ComposeProjection, ComposeSummaryCard, ConversationMessage, InteractionCard, ReasoningCard, TimelineItem } from "../../interactive/state"
import type { InteractiveSnapshot } from "../../interactive/types"
import { formatContext, formatDuration, formatElapsed, formatUsage } from "../../presentation-shared/formatters"
import { diffTextForRenderer, parseFileDiff } from "../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../presentation-shared/language-catalog"

import {
  COMPOSE_STAGE_LABELS,
  activityGroupSubtitle,
  activityGroupTitle,
  composeLiveStatusLine,
  isGroupExpandedByDefault,
  segmentTimeline,
  type TimelineActivityGroup,
} from "../../presentation-shared/timeline-activity-groups"
import { activityLabel, interactionStatusLabel, progressPhaseLabel } from "../../presentation-shared/timeline-presenter"
import { nextThinkingExpanded, thinkingVisibleBody } from "../../presentation-shared/paint-budget"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { useRunElapsed, useSpinner } from "./input-bar"
import { ToolRenderer } from "./tools/renderers"
import { createScrollAcceleration } from "./scroll.js"
import { markdownSyntax, tuiTheme, userMessageAccent } from "./theme"


function stageColor(status: string): string {
  if (status === "passed" || status === "completed") return tuiTheme.success
  if (status === "running" || status === "waiting_user") return tuiTheme.primary
  if (status === "failed" || status === "blocked" || status === "cancelled") return tuiTheme.danger
  return tuiTheme.muted
}

/** 活动投影优先；失败/完成后退回终态摘要快照，保证画面不空白。 */
function renderComposeProgress(interactive: InteractiveSnapshot): React.ReactNode {
  const live = interactive.composeState
  if (live) return <ComposeProgress state={live} />
  const summary = interactive.lastRun?.composeSummary
  if (!summary) return null
  return <ComposeProgress state={summary} />
}

/** 窄行截断 Compose 进度文案，避免任务/证据标题撑破时间线。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

/** Compose 运行进度：只显示五阶段、当前 task、evidence 与 blocked 摘要。 */
function ComposeProgress(props: { state: ComposeProjection }) {
  const state = props.state
  const currentTask = state.tasks.find(task => task.status === "running" || task.status === "pending")
  const runningEvidence = state.evidence.find(item => item.status === "running" || item.status === "failed")
  return (
    <box flexDirection="column" paddingBottom={1} paddingLeft={1} paddingRight={1}>
      <box flexDirection="row" gap={1}>
        {state.stages.map(stage => (
          <text key={stage.id} fg={stageColor(stage.status)} attributes={stage.status === "running" ? TextAttributes.BOLD : undefined}>
            {COMPOSE_STAGE_LABELS[stage.id] ?? stage.id}
            {stage.status === "running" ? "*" : stage.status === "passed" ? "✓" : ""}
          </text>
        ))}
        <text fg={tuiTheme.muted}>rev {state.revision}</text>
      </box>
      {currentTask ? (
        <text fg={tuiTheme.text}>任务：{shorten(currentTask.title, 60)}</text>
      ) : null}
      {runningEvidence ? (
        <text fg={stageColor(runningEvidence.status)}>验证：{shorten(runningEvidence.label, 60)}</text>
      ) : null}
      {state.blockedReason ? (
        <text fg={tuiTheme.danger}>阻塞：{shorten(state.blockedReason, 80)}</text>
      ) : null}
    </box>
  )
}

/** 使用 ScrollBox 渲染统一 timeline，并保留 sticky-scroll 行为。 */
export function ConversationTimeline(props: {
  interactive: InteractiveSnapshot
  scrollRef: RefObject<ScrollBoxRenderable | null>
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  modelName?: string
  transientNotice?: { id: string; message: string }
  terminalWidth: number
}) {
  const segments = useMemo(
    () => segmentTimeline(props.interactive.timeline),
    [props.interactive.timeline],
  )
  const pendingRequestId = props.interactive.interaction?.requestId ?? null
  // 用户显式展开/折叠的 activity；缺省遵循 terminal 默认折叠。
  const [expandedActivities, setExpandedActivities] = useState<ReadonlySet<string>>(() => new Set())
  const [collapsedActivities, setCollapsedActivities] = useState<ReadonlySet<string>>(() => new Set())

  const isExpanded = (group: TimelineActivityGroup): boolean => {
    if (expandedActivities.has(group.key)) return true
    if (collapsedActivities.has(group.key)) return false
    return isGroupExpandedByDefault(group)
  }

  const toggleGroup = (group: TimelineActivityGroup) => {
    const open = isExpanded(group)
    if (open) {
      setExpandedActivities(current => {
        const next = new Set(current)
        next.delete(group.key)
        return next
      })
      setCollapsedActivities(current => new Set(current).add(group.key))
    } else {
      setCollapsedActivities(current => {
        const next = new Set(current)
        next.delete(group.key)
        return next
      })
      setExpandedActivities(current => new Set(current).add(group.key))
    }
  }

  return (
    <scrollbox ref={props.scrollRef} stickyScroll stickyStart="bottom" flexGrow={1} minHeight={0} scrollAcceleration={createScrollAcceleration()} viewportOptions={{ paddingRight: 1 }}>
      <box height={1} />
      {segments.map(segment => {
        if (segment.kind === "flat") {
          if (isPendingLiveInteraction(segment.item, pendingRequestId)) return null
          return (
            <TimelineRow
              key={timelineItemKey(segment.item)}
              item={segment.item}
              interactive={props.interactive}
              showToolDetails={props.showToolDetails}
              expandedTools={props.expandedTools}
              onToggleTool={props.onToggleTool}
              terminalWidth={props.terminalWidth}
            />
          )
        }
        const group = segment.group
        const open = isExpanded(group)
        return (
          <box key={group.key} marginTop={1} marginLeft={1} marginRight={1} flexDirection="column">
            <box flexDirection="row" gap={1} onMouseUp={() => toggleGroup(group)}>
              <text fg={tuiTheme.primary}>{open ? "▼" : "▶"}</text>
              <text fg={tuiTheme.text} attributes={TextAttributes.BOLD}>{activityGroupTitle(group)}</text>
              <text fg={tuiTheme.muted}>{activityGroupSubtitle(group)}</text>
            </box>
            {open ? group.items.filter(item => !isPendingLiveInteraction(item, pendingRequestId)).map(item => (
              <TimelineRow
                key={timelineItemKey(item)}
                item={item}
                interactive={props.interactive}
                showToolDetails={props.showToolDetails}
                expandedTools={props.expandedTools}
                onToggleTool={props.onToggleTool}
                terminalWidth={props.terminalWidth}
              />
            )) : group.summary ? (
              <box paddingLeft={3}>
                <text fg={tuiTheme.muted}>{group.summary.text}</text>
              </box>
            ) : null}
          </box>
        )
      })}
      {/* 当前阶段状态贴近执行区底部，长历史时不因插在顶部而滚出视口。 */}
      <TimelineActivity interactive={props.interactive} />
      {renderComposeProgress(props.interactive)}
      <ErrorBlock interactive={props.interactive} />
      <RunFooter interactive={props.interactive} modelName={props.modelName} />
      {props.transientNotice ? <TransientNotice key={props.transientNotice.id} message={props.transientNotice.message} /> : null}
      <box height={1} />
    </scrollbox>
  )
}

/** 宿主级结果通知（Web 启动等）在时间线末尾以系统消息样式展示。 */
function TransientNotice(props: { message: string }) {
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.warning}>·</text>
      <text content={props.message} fg={tuiTheme.warning} />
    </box>
  )
}

/**
 * 消息与工具共用同一时间线，必须在这里逐项渲染，不能再次按类型拆成两个列表；
 * 否则工具卡片会被错误地堆到所有回答文本之后。
 */
/** 根据统一 timeline item 类型选择消息或工具渲染器。 */
function isPendingLiveInteraction(item: TimelineItem, pendingRequestId: string | null): boolean {
  return pendingRequestId !== null
    && item.type === "interaction"
    && item.interaction.id === pendingRequestId
    && item.interaction.status === "pending"
}

function TimelineRow(props: {
  item: TimelineItem
  interactive: InteractiveSnapshot
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  terminalWidth: number
}) {
  if (props.item.type === "message") return <MessageBlock message={props.item.message} />
  if (props.item.type === "reasoning") return <ReasoningRow reasoning={props.item.reasoning} />
  if (props.item.type === "interaction") {
    return <InteractionRow interaction={props.item.interaction} />
  }
  if (props.item.type === "compose-summary") {
    return <ComposeSummaryRow summary={props.item.summary} />
  }
  return <ToolRenderer tool={props.item.tool} terminalWidth={props.terminalWidth} />
}

/** 阶段 Runtime 摘要：非 assistant 文本，仅展示有界结果。 */
function ComposeSummaryRow(props: { summary: ComposeSummaryCard }) {
  const stage = props.summary.composeScope?.stage
    ? (COMPOSE_STAGE_LABELS[props.summary.composeScope.stage] ?? props.summary.composeScope.stage)
    : "compose"
  return (
    <box marginTop={1} marginLeft={2} marginRight={2}>
      <text fg={tuiTheme.muted}>{`阶段摘要 · ${stage} · ${props.summary.status}`}</text>
      <text content={props.summary.text} fg={tuiTheme.text} />
    </box>
  )
}

/** 渲染用户、Agent 和系统消息，并为 Agent Markdown 接入离线语法主题。 */
function MessageBlock(props: { message: ConversationMessage }) {
  if (props.message.role === "user") {
    return (
      <box marginTop={1} paddingLeft={2} paddingRight={2} flexDirection="row" gap={1}>
        <text fg={userMessageAccent(props.message.workMode)}>▌</text>
        <text content={props.message.content} fg={tuiTheme.text} />
      </box>
    )
  }

  if (props.message.role === "assistant") {
    // 流式文本按首次真正到达的 sequence 插入；没有内容就不伪造历史消息。
    if (!props.message.content) return null
    return (
      <box flexDirection="column" marginTop={1} paddingLeft={3} paddingRight={3}>
        {renderAssistantMarkdown(props.message.content || "…", props.message.streaming ?? false)}
      </box>
    )
  }

  return <SystemEvent content={props.message.content} />
}

/** 压缩、中断、Skill 加载等系统事件：一行 muted，不走工具/错误块。 */
function SystemEvent(props: { content: string }) {
  const loaded = props.content.match(/^skill-loaded:\s*(.+)$/)
  const text = loaded ? `已加载 Skill ${loaded[1]}` : props.content
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.subtle}>·</text>
      <text content={text} fg={tuiTheme.muted} />
    </box>
  )
}

/** 普通 Agent 回复中的 diff fenced block 复用原生 DiffRenderable，保持与审批卡一致的红绿行面。 */
function renderAssistantMarkdown(content: string, streaming: boolean): ReactNode {
  const pattern = /```(?:diff|patch|udiff|unified-diff)\s*\n([\s\S]*?)```/gi
  const nodes: ReactNode[] = []
  let cursor = 0
  let match: RegExpExecArray | null
  let index = 0
  while ((match = pattern.exec(content)) !== null) {
    const before = content.slice(cursor, match.index)
    if (before) nodes.push(<markdown key={`markdown-${index++}`} content={before} syntaxStyle={markdownSyntax} treeSitterClient={getCommonSyntaxClient()} streaming={streaming} fg={tuiTheme.text} bg={tuiTheme.background} conceal concealCode={false} internalBlockMode="top-level" tableOptions={{ style: "columns", borders: false }} />)
    nodes.push(<DiffMessageBlock key={`diff-${index++}`} diff={match[1] ?? ""} />)
    cursor = match.index + match[0].length
  }
  const after = content.slice(cursor)
  if (nodes.length === 0) return <markdown content={content} syntaxStyle={markdownSyntax} treeSitterClient={getCommonSyntaxClient()} streaming={streaming} fg={tuiTheme.text} bg={tuiTheme.background} conceal concealCode={false} internalBlockMode="top-level" tableOptions={{ style: "columns", borders: false }} />
  if (after) nodes.push(<markdown key={`markdown-${index}`} content={after} syntaxStyle={markdownSyntax} treeSitterClient={getCommonSyntaxClient()} streaming={streaming} fg={tuiTheme.text} bg={tuiTheme.background} conceal concealCode={false} internalBlockMode="top-level" tableOptions={{ style: "columns", borders: false }} />)
  return nodes
}

function DiffMessageBlock(props: { diff: string }) {
  const parsed = parseFileDiff(props.diff)
  if (parsed.status === "invalid" || props.diff.trim() === "") return <text content={props.diff} fg={tuiTheme.text} />
  return <diff width="100%" diff={diffTextForRenderer(props.diff)} view="unified" syncScroll showLineNumbers wrapMode="word" fg={tuiTheme.text} lineNumberFg={tuiTheme.muted} lineNumberBg={tuiTheme.toolSurface} addedBg={tuiTheme.diffAddedBackground} removedBg={tuiTheme.diffRemovedBackground} contextBg={tuiTheme.toolSurface} addedSignColor={tuiTheme.success} removedSignColor={tuiTheme.danger} addedLineNumberBg={tuiTheme.diffAddedBackground} removedLineNumberBg={tuiTheme.diffRemovedBackground} />
}

/** 时间线中的思考条目：标记与正文分列，正文与 Thinking 左对齐。 */
function ReasoningRow(props: { reasoning: ReasoningCard }) {
  const { reasoning } = props
  const [expanded, setExpanded] = useState(false)
  const frame = useSpinner(reasoning.active, 80)
  const firstLine = reasoning.text.split("\n")[0] ?? ""
  const paintState = reasoning.active ? "live" : expanded ? "expanded" : "collapsed"
  const body = thinkingVisibleBody(reasoning.text, paintState)
  const marker = reasoning.active ? frame : expanded ? "-" : "+"
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={2} onMouseUp={() => setExpanded(current => nextThinkingExpanded(reasoning.active, current))}>
      <text fg={tuiTheme.thinking}>{marker}</text>
      <box flexDirection="column" flexShrink={1}>
        <box flexDirection="row" gap={1}>
          <text fg={tuiTheme.thinking}>Thinking</text>
          {reasoning.active ? null : <text fg={tuiTheme.subtle}>{expanded ? "收起" : "展开"}</text>}
        </box>
        {paintState === "collapsed" && firstLine ? <text content={firstLine} fg={tuiTheme.muted} /> : null}
        {body.text ? <text content={body.text} fg={tuiTheme.muted} /> : null}
        {body.overflow && paintState !== "collapsed" ? <text fg={tuiTheme.subtle}>还有 {body.hiddenLines} 行</text> : null}
      </box>
    </box>
  )
}

/** 当前运行只在时间线末尾显示临时活动状态，绝不插回已有事件之间。 */
function TimelineActivity(props: { interactive: InteractiveSnapshot }) {
  const { interactive } = props
  const visible = Boolean(interactive.activeRun)
    && interactive.interaction === null
    && interactive.activity.kind !== "waiting-interaction"
  // Hooks 不能因运行状态不同而跳过；否则 thread 恢复后再次执行会破坏 React hook 顺序。
  const frame = useSpinner(visible, 80)
  const elapsed = useRunElapsed(visible, interactive.runProgress?.elapsedMs)
  if (!visible) return null
  const phase = interactive.runProgress
    ? progressPhaseLabel(interactive.runProgress.phase)
    : activityLabel(interactive.activity.kind)
  const compose = interactive.composeState
  const currentTask = compose?.tasks.find(task => task.status === "running")
  const line = compose
    ? `${composeLiveStatusLine({
      stage: compose.stage,
      taskTitle: currentTask?.title,
      phaseLabel: phase,
      elapsedLabel: formatElapsed(elapsed),
    })} · Esc 取消`
    : `${phase} · 已运行 ${formatElapsed(elapsed)} · Esc 取消`
  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.warning}>{frame}</text>
      <text fg={tuiTheme.warning}>{line}</text>
    </box>
  )
}

/** 仅 Run / 连接级失败；工具失败留在对应 Tool 组件。 */
function ErrorBlock(props: { interactive: InteractiveSnapshot }) {
  const { interactive } = props
  const connection = interactive.connection
  if (connection.status === "protocol-error" || connection.status === "closed") {
    return (
      <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="column">
        <text fg={tuiTheme.danger}>运行失败</text>
        <text content={connection.message} fg={tuiTheme.muted} />
      </box>
    )
  }
  if (interactive.lastRun?.outcome !== "failed" && interactive.activity.kind !== "failed") return null
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="column">
      <text fg={tuiTheme.danger}>运行失败</text>
    </box>
  )
}

/** 一轮结束后一行 muted：模型 · 耗时 · 用量。失败改走 ErrorBlock。 */
function RunFooter(props: { interactive: InteractiveSnapshot; modelName?: string }) {
  const summary = props.interactive.lastRun
  if (!summary || summary.outcome === "failed") return null
  const duration = formatDuration(summary.durationMs)
  const usage = formatUsage(summary.usage)
  const context = summary.context?.estimatedTokens && summary.context.inputCapTokens
    ? `ctx ${formatContext(summary.context.estimatedTokens, summary.context.inputCapTokens)}`
    : undefined
  const outcome = summary.outcome === "cancelled" ? "已取消" : "已完成"
  const parts = [outcome, props.modelName, duration, usage, context].filter((part): part is string => Boolean(part))

  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.muted}>{parts.join(" · ")}</text>
    </box>
  )
}

/** 已完成的审批/问答只留一行结果；pending 由底部 Dock 处理。 */
function InteractionRow(props: { interaction: InteractionCard }) {
  const { interaction } = props
  if (interaction.status === "pending") return null
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={interactionStatusColor(interaction.status)}>{interaction.type === "approval" ? "△" : "?"}</text>
      <text fg={tuiTheme.muted}>{interactionStatusLabel(interaction.status)}</text>
      {interaction.description ? <text content={interaction.description} fg={tuiTheme.subtle} /> : null}
    </box>
  )
}

/** 终端审批内容达到 120 列时使用双栏，否则使用行内 Diff。 */
export function tuiDiffViewForWidth(contentWidth: number): "split" | "unified" {
  return contentWidth >= 120 ? "split" : "unified"
}

/** 为时间线事件提供不会跨 run/activity 冲突的 React key。 */
function timelineItemKey(item: TimelineItem): string {
  if (item.type === "message") return ["message", item.message.id].join(":")
  if (item.type === "tool") {
    return ["tool", item.tool.runId, item.tool.executionId ?? "root", item.tool.activityId ?? "root", item.tool.id].join(":")
  }
  if (item.type === "reasoning") return ["reasoning", item.reasoning.id].join(":")
  if (item.type === "compose-summary") return ["compose-summary", item.summary.id].join(":")
  return ["interaction", item.interaction.runId, item.interaction.id].join(":")
}

/** 拒绝和取消保留警示色，其余处理结果按成功状态展示。 */
function interactionStatusColor(status: InteractionCard["status"]): string {
  if (status === "rejected" || status === "cancelled") return tuiTheme.warning
  return tuiTheme.success
}
