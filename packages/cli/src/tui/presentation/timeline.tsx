/** Thread 消息、工具和 Interaction 的统一时间线。 */

import { type ScrollBoxRenderable } from "@opentui/core"
import { type RefObject, useState } from "react"

import type { ConversationMessage, InteractionCard, ReasoningCard, TimelineItem, ToolCard } from "../../interactive/state"
import type { InteractiveSnapshot } from "../../interactive/types"
import { formatContext, formatDuration, formatElapsed, formatUsage } from "../../presentation-shared/formatters"
import { diffTextForRenderer, parseFileDiff } from "../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../presentation-shared/language-catalog"
import { APPROVAL_DECISION_ORDER, approvalDecisionDescription, approvalDecisionLabel, isApprovalDecision } from "../../presentation-shared/interaction-policy"
import { activityLabel, interactionStatusLabel, progressPhaseLabel, toolStatusLabel } from "../../presentation-shared/timeline-presenter"
import { collapseToolOutput } from "../../presentation-shared/tool-output-policy"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { PROMPT_BORDER, useRunElapsed, useSpinner } from "./composer"
import { createScrollAcceleration } from "./scroll.js"
import { markdownSyntax, tuiTheme } from "./theme"
import type { ApprovalDecision } from "./types"

/** 使用 ScrollBox 渲染统一 timeline，并保留 sticky-scroll 行为。 */
export function ConversationTimeline(props: {
  interactive: InteractiveSnapshot
  scrollRef: RefObject<ScrollBoxRenderable | null>
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
  modelName?: string
  transientNotice?: { id: string; message: string }
  terminalWidth: number
}) {
  return (
    <scrollbox ref={props.scrollRef} stickyScroll stickyStart="bottom" flexGrow={1} minHeight={0} scrollAcceleration={createScrollAcceleration()} viewportOptions={{ paddingRight: 1 }}>
      <box height={1} />
      {props.interactive.timeline.map(item => (
        <TimelineRow
          key={timelineItemKey(item)}
          item={item}
          interactive={props.interactive}
          showToolDetails={props.showToolDetails}
          expandedTools={props.expandedTools}
          onToggleTool={props.onToggleTool}
          onApproval={props.onApproval}
          onQuestion={props.onQuestion}
          terminalWidth={props.terminalWidth}
        />
      ))}
      <TimelineActivity interactive={props.interactive} />
      <RunSummary interactive={props.interactive} modelName={props.modelName} />
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
function TimelineRow(props: {
  item: TimelineItem
  interactive: InteractiveSnapshot
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
  terminalWidth: number
}) {
  if (props.item.type === "message") return <MessageBlock message={props.item.message} />
  if (props.item.type === "reasoning") return <ReasoningRow reasoning={props.item.reasoning} />
  if (props.item.type === "interaction") {
    return <InteractionRow interaction={props.item.interaction} activeInteraction={props.interactive.interaction} onApproval={props.onApproval} onQuestion={props.onQuestion} terminalWidth={props.terminalWidth} />
  }
  const tool = props.item.tool
  const toolKey = toolTimelineKey(tool)
  return (
    <ToolRow
      tool={tool}
      expanded={props.showToolDetails || props.expandedTools.has(toolKey) || tool.status !== "completed"}
      onToggle={() => props.onToggleTool(toolKey)}
    />
  )
}

/** 渲染用户、Agent 和系统消息，并为 Agent Markdown 接入离线语法主题。 */
function MessageBlock(props: { message: ConversationMessage }) {
  if (props.message.role === "user") {
    return (
      <box marginTop={1} marginLeft={2} marginRight={2} border={["left"]} borderColor={tuiTheme.primary} customBorderChars={PROMPT_BORDER}>
        <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
          <text content={props.message.content} fg={tuiTheme.text} />
        </box>
      </box>
    )
  }

  if (props.message.role === "assistant") {
    // 流式文本按首次真正到达的 sequence 插入；没有内容就不伪造历史消息。
    if (!props.message.content) return null
    return (
      <box flexDirection="column" marginTop={1} paddingLeft={3} paddingRight={3}>
        <markdown
          content={props.message.content || "…"}
          syntaxStyle={markdownSyntax}
          treeSitterClient={getCommonSyntaxClient()}
          streaming={props.message.streaming ?? false}
          fg={tuiTheme.text}
          bg={tuiTheme.background}
          conceal
          concealCode={false}
          internalBlockMode="top-level"
          tableOptions={{ style: "columns", borders: false }}
        />
      </box>
    )
  }

  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.subtle}>·</text>
      <text content={props.message.content} fg={tuiTheme.muted} />
    </box>
  )
}

/** 渲染工具状态、折叠预览和可展开原始输出。 */
function ToolRow(props: { tool: ToolCard; expanded: boolean; onToggle: () => void }) {
  const tone = props.tool.status === "failed" ? tuiTheme.danger : props.tool.status === "completed" ? tuiTheme.success : tuiTheme.primary
  const marker = props.tool.status === "failed" ? "×" : props.tool.status === "completed" ? "✓" : "◌"
  const label = toolStatusLabel(props.tool.status)
  const collapsed = collapseToolOutput(props.tool.output, 4, 360)
  const output = props.expanded ? props.tool.output : collapsed.output
  const argumentsPreview = collapseToolOutput(props.tool.arguments, 1, 240).output

  return (
    <box marginTop={1} marginLeft={3} marginRight={3} border={["left"]} borderColor={tone} customBorderChars={PROMPT_BORDER}>
      <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} onMouseUp={props.onToggle}>
        <box flexDirection="row" justifyContent="space-between" gap={2}>
          <box flexDirection="row" gap={1} flexShrink={1}>
            <text fg={tone}>{marker}</text>
            <text fg={tuiTheme.text}>{props.tool.name}</text>
            <text fg={tuiTheme.muted}>{label}</text>
          </box>
          {collapsed.overflow ? <text fg={tuiTheme.subtle}>{props.expanded ? "收起结果" : "展开结果"}</text> : null}
        </box>
        {argumentsPreview ? (
          <box paddingTop={1} flexDirection="row" gap={1}>
            <text fg={tuiTheme.subtle}>›</text>
            <text content={argumentsPreview} fg={tuiTheme.subtle} />
          </box>
        ) : null}
        {output ? <text content={output} fg={tuiTheme.muted} /> : null}
      </box>
    </box>
  )
}

/** 时间线中的思考条目：流式中显示 spinner+全文，冻结后折叠为可展开摘要头。 */
function ReasoningRow(props: { reasoning: ReasoningCard }) {
  const { reasoning } = props
  const [expanded, setExpanded] = useState(false)
  const frame = useSpinner(reasoning.active, 80)
  const firstLine = reasoning.text.split("\n")[0] ?? ""
  const showFull = expanded || reasoning.active
  return (
    <box marginTop={1} marginLeft={3} marginRight={3} border={["left"]} borderColor={tuiTheme.primarySoft} customBorderChars={PROMPT_BORDER}>
      <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} onMouseUp={() => setExpanded(current => !current)}>
        <box flexDirection="row" gap={1}>
          {reasoning.active ? <text fg={tuiTheme.warning}>{frame}</text> : <text fg={tuiTheme.primary}>◆</text>}
          <text fg={tuiTheme.primary}>{reasoning.active ? "思考中" : "思考"}</text>
          {reasoning.active ? null : <text fg={tuiTheme.muted}>{expanded ? "收起" : "展开"}</text>}
        </box>
        {showFull ? <text content={reasoning.text} fg={tuiTheme.muted} /> : <text content={firstLine} fg={tuiTheme.muted} />}
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
  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.warning}>{frame}</text>
      <text fg={tuiTheme.warning}>{phase} · 已运行 {formatElapsed(elapsed)} · Esc 取消</text>
    </box>
  )
}

/** 显示运行终态、耗时和 token 用量摘要。 */
function RunSummary(props: { interactive: InteractiveSnapshot; modelName?: string }) {
  const summary = props.interactive.lastRun
  if (!summary) return null
  const duration = formatDuration(summary.durationMs)
  const usage = formatUsage(summary.usage)
  const context = summary.context?.estimatedTokens && summary.context.inputCapTokens
    ? `ctx ${formatContext(summary.context.estimatedTokens, summary.context.inputCapTokens)}`
    : undefined
  const outcome = summary.outcome === "completed" ? "已完成" : summary.outcome === "cancelled" ? "已取消" : "失败"
  const color = summary.outcome === "completed" ? tuiTheme.success : summary.outcome === "cancelled" ? tuiTheme.muted : tuiTheme.danger
  const parts = [outcome, props.modelName, duration, usage, context].filter((part): part is string => Boolean(part))

  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={color}>●</text>
      <text fg={tuiTheme.muted}>{parts.join(" · ")}</text>
    </box>
  )
}

/** 审批和问答是不可脱离时间线的阻塞事件，完成后保留用户处理结果。 */
function InteractionRow(props: {
  interaction: InteractionCard
  activeInteraction?: InteractiveSnapshot["interaction"]
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
  terminalWidth: number
}) {
  const { interaction } = props
  const pending = interaction.status === "pending"
  const approval = interaction.type === "approval"
  const tone = approval ? tuiTheme.warning : tuiTheme.primary
  const allowedDecisions = props.activeInteraction?.type === "approval" ? props.activeInteraction.decisions : APPROVAL_DECISION_ORDER
  const decisionOptions = allowedDecisions
    .filter(isApprovalDecision)
    .map(decision => ({ name: approvalDecisionLabel(decision), description: approvalDecisionDescription(decision), value: decision }))

  return (
    <box marginTop={1} marginLeft={2} marginRight={2} border={["left"]} borderColor={tone} customBorderChars={PROMPT_BORDER}>
      <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
        <box flexDirection="row" gap={1}>
          <text fg={tone}>{approval ? "△" : "?"}</text>
          <text fg={tuiTheme.text}><strong>{approval ? "需要审批" : "Agent 需要你的回答"}</strong></text>
        </box>
        {approval ? <>
          {interaction.description ? <text content={interaction.description} fg={tuiTheme.text} /> : null}
          {pending && props.activeInteraction?.type === "approval" && props.activeInteraction.presentation
            ? <FileDiffApprovalPreview presentation={props.activeInteraction.presentation} terminalWidth={props.terminalWidth} />
            : <ApprovalRequestPreview requests={interaction.requests} />}
        </> : interaction.question ? <text content={interaction.question} fg={tuiTheme.text} /> : null}
        {pending && approval ? (
          <>
            <select
              focused
              height={Math.max(2, Math.min(10, allowedDecisions.length * 2))}
              showDescription
              wrapSelection
              options={decisionOptions}
              onSelect={(_, option) => {
                const value = option?.value
                if (value === "approve_once" || value === "approve_thread" || value === "approve_project" || value === "reject" || value === "reject_with_feedback") {
                  props.onApproval(value)
                }
              }}
            />
            <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
          </>
        ) : null}
        {pending && !approval && interaction.options?.length ? (
          <>
            <select
              focused
              height={Math.max(2, Math.min(6, interaction.options.length * 2))}
              showDescription
              wrapSelection
              options={interaction.options.map(option => ({ ...option, description: option.name }))}
              onSelect={(_, option) => { if (typeof option?.value === "string") props.onQuestion(option.value) }}
            />
            <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
          </>
        ) : null}
        {pending && !approval && !interaction.options?.length ? <text fg={tuiTheme.muted}>等待回答</text> : null}
        {!pending ? <text fg={interactionStatusColor(interaction.status)}>{interactionStatusLabel(interaction.status)}</text> : null}
      </box>
    </box>
  )
}

/** 终端审批内容达到 120 列时使用双栏，否则使用行内 Diff。 */
export function tuiDiffViewForWidth(contentWidth: number): "split" | "unified" {
  return contentWidth >= 120 ? "split" : "unified"
}

/** 使用 OpenTUI 原生 Diff renderer 展示同一 prepared plan 的有界预览。 */
function FileDiffApprovalPreview(props: {
  presentation: NonNullable<Extract<InteractiveSnapshot["interaction"], { type: "approval" }>["presentation"]>
  terminalWidth: number
}) {
  const { presentation } = props
  const contentWidth = Math.max(1, props.terminalWidth - 10)
  const view = tuiDiffViewForWidth(contentWidth)
  const language = resolveLanguageForPath(presentation.path).tuiParser
  const parsed = parseFileDiff(presentation.unified_diff)
  const operation = presentation.operation === "write" ? "创建文件" : presentation.operation === "delete" ? "删除文件" : "编辑文件"
  const summary = `${operation} · ${presentation.path} · +${presentation.added_lines} / -${presentation.removed_lines}`
  return (
    <box flexDirection="column" marginTop={1}>
      <text content={summary} fg={tuiTheme.text} />
      {presentation.truncated ? (
        <text content="预览已按 200 行或 16 KiB 上限截断；批准仍会应用完整变更。" fg={tuiTheme.warning} />
      ) : null}
      {presentation.unified_diff === "" ? (
        <text content="创建空文件（没有可显示的内容行）" fg={tuiTheme.muted} />
      ) : parsed.status === "invalid" ? (
        <>
          <text content="无法解析结构化 Diff，以下按纯文本展示。" fg={tuiTheme.warning} />
          <text content={presentation.unified_diff} fg={tuiTheme.text} />
        </>
      ) : (
        <diff
          width="100%"
          diff={diffTextForRenderer(presentation.unified_diff)}
          view={view}
          syncScroll
          filetype={language === "plaintext" ? undefined : language}
          syntaxStyle={markdownSyntax}
          treeSitterClient={getCommonSyntaxClient()}
          showLineNumbers
          wrapMode="word"
          fg={tuiTheme.text}
          lineNumberFg={tuiTheme.muted}
          lineNumberBg={tuiTheme.toolSurface}
          addedBg={tuiTheme.diffAddedBackground}
          removedBg={tuiTheme.diffRemovedBackground}
          contextBg={tuiTheme.toolSurface}
          addedSignColor={tuiTheme.success}
          removedSignColor={tuiTheme.danger}
          addedLineNumberBg={tuiTheme.diffAddedBackground}
          removedLineNumberBg={tuiTheme.diffRemovedBackground}
        />
      )}
    </box>
  )
}

/** 为同一 run 重复出现的 provider tool ID 生成稳定的渲染和展开键。 */
function toolTimelineKey(tool: ToolCard): string {
  return ["tool", tool.runId, tool.id].join(":")
}

/** 为三类时间线事件提供不会跨 run 冲突的 React key。 */
function timelineItemKey(item: TimelineItem): string {
  if (item.type === "message") return ["message", item.message.id].join(":")
  if (item.type === "tool") return toolTimelineKey(item.tool)
  if (item.type === "reasoning") return ["reasoning", item.reasoning.id].join(":")
  return ["interaction", item.interaction.runId, item.interaction.id].join(":")
}

/** 拒绝和取消保留警示色，其余处理结果按成功状态展示。 */
function interactionStatusColor(status: InteractionCard["status"]): string {
  if (status === "rejected" || status === "cancelled") return tuiTheme.warning
  return tuiTheme.success
}


/** 将审批请求中的动作摘要交给工具面板显示。 */
function ApprovalRequestPreview(props: { requests: unknown }) {
  const preview = approvalPreview(props.requests)
  return preview ? <text content={preview} fg={tuiTheme.muted} /> : null
}

/** 从不可信审批 payload 中提取有限长度的安全预览。 */
function approvalPreview(requests: unknown): string | undefined {
  if (!requests || typeof requests !== "object") return undefined
  const actions = (requests as Record<string, unknown>).action_requests
  if (!Array.isArray(actions) || actions.length === 0) return undefined
  return actions.slice(0, 2).flatMap(action => {
    if (!action || typeof action !== "object") return []
    const record = action as Record<string, unknown>
    const name = typeof record.name === "string" ? record.name : "tool"
    const args = safePreview(record.args)
    return [`${name}${args ? ` · ${args}` : ""}`]
  }).join("\n")
}


/** 按字符数截断普通预览文本。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}


/** 安全序列化工具参数，避免循环引用破坏整个 TUI。 */
function safePreview(value: unknown): string | undefined {
  if (value === undefined) return undefined
  try {
    return shorten(JSON.stringify(value), 120)
  } catch {
    return "参数不可序列化"
  }
}
