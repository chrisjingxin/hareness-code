/** Thread 消息、工具和 Interaction 的统一时间线。 */

import { type ScrollBoxRenderable } from "@opentui/core"
import { type RefObject } from "react"

import { formatDuration, formatUsage } from "../application/model"
import type { ConversationMessage, InteractionCard, TimelineItem, ToolCard, TuiState } from "../application/state"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { collapseToolOutput } from "../upstream/collapse-tool-output"
import { PROMPT_BORDER, useSpinner } from "./composer"
import { createScrollAcceleration } from "./scroll.js"
import { markdownSyntax, tuiTheme } from "./theme"
import type { ApprovalDecision } from "./types"

/** 使用 ScrollBox 渲染统一 timeline，并保留 sticky-scroll 行为。 */
export function ConversationTimeline(props: {
  state: TuiState
  scrollRef: RefObject<ScrollBoxRenderable | null>
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
  modelName?: string
}) {
  return (
    <scrollbox ref={props.scrollRef} stickyScroll stickyStart="bottom" flexGrow={1} minHeight={0} scrollAcceleration={createScrollAcceleration()} viewportOptions={{ paddingRight: 1 }}>
      <box height={1} />
      {props.state.timeline.map(item => (
        <TimelineRow
          key={timelineItemKey(item)}
          item={item}
          state={props.state}
          showToolDetails={props.showToolDetails}
          expandedTools={props.expandedTools}
          onToggleTool={props.onToggleTool}
          onApproval={props.onApproval}
          onQuestion={props.onQuestion}
        />
      ))}
      <TimelineActivity state={props.state} />
      <RunSummary state={props.state} modelName={props.modelName} />
      <box height={1} />
    </scrollbox>
  )
}

/**
 * 消息与工具共用同一时间线，必须在这里逐项渲染，不能再次按类型拆成两个列表；
 * 否则工具卡片会被错误地堆到所有回答文本之后。
 */
/** 根据统一 timeline item 类型选择消息或工具渲染器。 */
function TimelineRow(props: {
  item: TimelineItem
  state: TuiState
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
}) {
  if (props.item.type === "message") return <MessageBlock message={props.item.message} />
  if (props.item.type === "interaction") {
    return <InteractionRow interaction={props.item.interaction} onApproval={props.onApproval} onQuestion={props.onQuestion} />
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
  const label = props.tool.status === "failed" ? "失败" : props.tool.status === "completed" ? "已完成" : "执行中"
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

/** 当前运行只在时间线末尾显示临时活动状态，绝不插回已有事件之间。 */
function TimelineActivity(props: { state: TuiState }) {
  const tail = props.state.timeline.at(-1)
  const visible = Boolean(props.state.activeRun)
    && !props.state.pendingApproval
    && !props.state.pendingQuestion
    && props.state.status !== "正在调用工具"
    && !(tail?.type === "message" && tail.message.role === "assistant" && tail.message.streaming)
  // Hooks 不能因运行状态不同而跳过；否则 thread 恢复后再次执行会破坏 React hook 顺序。
  const frame = useSpinner(visible, 80)
  if (!visible) return null
  const label = props.state.status === "正在继续执行" ? "继续执行" : props.state.status
  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.warning}>{frame}</text>
      <text fg={tuiTheme.warning}>{label}</text>
    </box>
  )
}

/** 显示运行终态、耗时和 token 用量摘要。 */
function RunSummary(props: { state: TuiState; modelName?: string }) {
  const summary = props.state.lastRun
  if (!summary) return null
  const duration = formatDuration(summary.durationMs)
  const usage = formatUsage(summary.usage)
  const context = summary.context?.estimatedTokens && summary.context.inputCapTokens
    ? `ctx ${summary.context.estimatedTokens}/${summary.context.inputCapTokens}`
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
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
}) {
  const { interaction } = props
  const pending = interaction.status === "pending"
  const approval = interaction.type === "approval"
  const tone = approval ? tuiTheme.warning : tuiTheme.primary

  return (
    <box marginTop={1} marginLeft={2} marginRight={2} border={["left"]} borderColor={tone} customBorderChars={PROMPT_BORDER}>
      <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
        <box flexDirection="row" gap={1}>
          <text fg={tone}>{approval ? "△" : "?"}</text>
          <text fg={tuiTheme.text}><strong>{approval ? "需要审批" : "Agent 需要你的回答"}</strong></text>
        </box>
        {approval ? <>
          {interaction.description ? <text content={interaction.description} fg={tuiTheme.text} /> : null}
          <ApprovalRequestPreview requests={interaction.requests} />
        </> : interaction.question ? <text content={interaction.question} fg={tuiTheme.text} /> : null}
        {pending && approval ? (
          <>
            <select
              focused
              height={10}
              showDescription
              wrapSelection
              options={[
                { name: "允许一次", description: "继续执行当前操作", value: "approve_once" },
                { name: "本线程允许", description: "当前会话内不再询问", value: "approve_thread" },
                { name: "永久允许", description: "此后同类操作自动放行", value: "approve_always" },
                { name: "拒绝", description: "停止此操作并告知 Agent", value: "reject" },
                { name: "拒绝并反馈", description: "拒绝并附带修改建议", value: "reject_with_feedback" },
              ]}
              onSelect={(_, option) => {
                const value = option?.value
                if (value === "approve_once" || value === "approve_thread" || value === "approve_always" || value === "reject" || value === "reject_with_feedback") {
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

/** 为同一 run 重复出现的 provider tool ID 生成稳定的渲染和展开键。 */
function toolTimelineKey(tool: ToolCard): string {
  return ["tool", tool.runId, tool.id].join(":")
}

/** 为三类时间线事件提供不会跨 run 冲突的 React key。 */
function timelineItemKey(item: TimelineItem): string {
  if (item.type === "message") return ["message", item.message.id].join(":")
  if (item.type === "tool") return toolTimelineKey(item.tool)
  return ["interaction", item.interaction.runId, item.interaction.id].join(":")
}

/** 将已落定的交互状态压缩为简短、可扫描的历史标签。 */
function interactionStatusLabel(status: InteractionCard["status"]): string {
  if (status === "approved") return "已允许"
  if (status === "rejected") return "已拒绝"
  if (status === "answered") return "已回答"
  if (status === "cancelled") return "未完成"
  return "已恢复执行"
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

