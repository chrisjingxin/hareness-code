/** Web Timeline：消息、Tool 卡和已完成 Interaction 卡的统一时间线，并托管滚动行为。 */
/** @jsxImportSource react */

import { AlertTriangle, Check, ChevronDown, Loader2 } from "lucide-react"
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react"
import { activityLabel } from "../../presentation-shared"

import type {
  ConversationMessage,
  InteractionCard,
  TimelineItem,
  ToolCard,
} from "../../interactive/state"
import type { WebAdapterSnapshot, WebIntent, WebScrollRequest } from "../application/adapter"
import { Markdown } from "./markdown"

/** 自动滚动判定阈值：用户视口底部离容器底部的距离小于该值即视为靠近底部。 */
const BOTTOM_THRESHOLD_PX = 48

/** Tool 参数摘要最大长度；超出后截断，避免折叠头撑破阅读列。 */
const ARGUMENT_SUMMARY_MAX = 80

/**
 * 渲染 Thread 的统一时间线。
 *
 * 组件只读取 `snapshot`、通过 `dispatch` 提交意图，不直接持有业务状态；
 * 滚动位置、Tool 展开集合、Interaction 草稿等表现细节全部来自 Adapter 发布的 snapshot。
 * 当前挂起中的 Interaction 不在这里渲染，会通过 `live-interaction-slot` 占位由其它组件挂载。
 */
export function Timeline({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): ReactElement {
  const timeline = snapshot.interactive.timeline
  const pendingRequestId = snapshot.interactive.interaction?.requestId ?? null
  const containerRef = useRef<HTMLDivElement | null>(null)
  const isNearBottomRef = useRef<boolean>(true)
  const lastScrollRequestRef = useRef<WebScrollRequest>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)

  /** 把容器滚到底部；调用方负责在调用后维护 near-bottom 状态。 */
  const scrollContainerToBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [])

  /** 处理滚动事件：维护 near-bottom ref 与按钮可见性，避免滚动过程触发额外渲染。 */
  const handleScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    const near = distance <= BOTTOM_THRESHOLD_PX
    isNearBottomRef.current = near
    setShowScrollButton(prev => (prev === !near ? prev : !near))
  }, [])

  /** Adapter 显式声明滚动意图时无条件跳到底部；空 → 非空转换都触发。 */
  useEffect(() => {
    const next = snapshot.scrollRequest
    if (next === lastScrollRequestRef.current) return
    lastScrollRequestRef.current = next
    if (next === null) return
    scrollContainerToBottom()
    isNearBottomRef.current = true
    setShowScrollButton(false)
  }, [snapshot.scrollRequest, scrollContainerToBottom])

  /** 时间线内容变化时，若用户原本就在底部则继续跟随；否则保持位置并显示按钮。 */
  useLayoutEffect(() => {
    if (isNearBottomRef.current) scrollContainerToBottom()
  }, [timeline, scrollContainerToBottom])

  const handleScrollToBottom = useCallback(() => {
    scrollContainerToBottom()
    isNearBottomRef.current = true
    setShowScrollButton(false)
  }, [scrollContainerToBottom])

  const handleToggleTool = useCallback(
    (toolId: string) => {
      if (disabled) return
      void dispatch({ type: "tool-toggle", toolId })
    },
    [disabled, dispatch],
  )

  const visibleItems = timeline.filter(item => {
    if (isPendingLive(item, pendingRequestId)) return false
    if (item.type === "message" && item.message.role === "assistant" && !item.message.streaming && !item.message.content.trim()) {
      return false
    }
    return true
  })

  return (
    <div
      ref={containerRef}
      className="timeline"
      role="log"
      aria-relevant="additions"
      aria-label="对话与工具时间线"
      onScroll={handleScroll}
    >
      {snapshot.interactive.currentThreadId !== null && visibleItems.length > 0 ? (
        <div className="timeline-header">THREAD · {timeline.length} 项记录</div>
      ) : null}
      {visibleItems.length === 0 ? (
        <div className="timeline-empty" role="status">
          发送第一条消息后，这里会显示 Agent 的回答、工具调用与审批记录。
        </div>
      ) : (
        visibleItems.map(item => (
          <TimelineRow
            key={timelineItemKey(item)}
            item={item}
            expandedTools={snapshot.expandedTools}
            onToggleTool={handleToggleTool}
          />
        ))
      )}
      <div className="live-interaction-slot" data-pending-request-id={pendingRequestId ?? undefined} />
      <div className="run-status-live" aria-live="polite">
        {activityLabel(snapshot.interactive.activity.kind)}
      </div>
      {showScrollButton ? (
        <button
          type="button"
          className="scroll-to-bottom"
          onClick={handleScrollToBottom}
          aria-label="跳到最新输出"
        >
          有新输出
        </button>
      ) : null}
    </div>
  )
}

/** 生成稳定的 React key：每种 timeline item 用其身份字段，跨 run 也不冲突。 */
function timelineItemKey(item: TimelineItem): string {
  switch (item.type) {
    case "message":
      return `message:${item.message.id}`
    case "tool":
      return `tool:${item.tool.runId}:${item.tool.id}`
    case "interaction":
      return `interaction:${item.interaction.runId}:${item.interaction.id}`
  }
}

/** 当前挂起中的 interaction 不在历史时间线重复渲染，由 live slot 负责。 */
function isPendingLive(item: TimelineItem, pendingRequestId: string | null): boolean {
  if (pendingRequestId === null) return false
  return (
    item.type === "interaction"
    && item.interaction.id === pendingRequestId
    && item.interaction.status === "pending"
  )
}

/** 顶层分派：根据 item 类型选 memo 包装；流式 Assistant 不 memo 以接收 in-place 更新。 */
function TimelineRowImpl({
  item,
  expandedTools,
  onToggleTool,
}: {
  item: TimelineItem
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
}): ReactElement {
  if (item.type === "message") {
    if (item.message.role === "assistant" && item.message.streaming === true) {
      return <StreamingAssistantBubble message={item.message} />
    }
    return <MemoMessageBubble message={item.message} />
  }
  if (item.type === "tool") {
    return (
      <div className="timeline-tool">
        <MemoToolCard
          tool={item.tool}
          expanded={expandedTools.has(item.tool.id)}
          onToggle={onToggleTool}
        />
      </div>
    )
  }
  return (
    <div className="timeline-interaction">
      <MemoInteractionCard interaction={item.interaction} />
    </div>
  )
}

const TimelineRow = memo(TimelineRowImpl)
TimelineRow.displayName = "TimelineRow"

/** 流式 Assistant 消息：内容随时 in-place 更新；不 memo 才能接收每帧变更。 */
function StreamingAssistantBubble({ message }: { message: ConversationMessage }): ReactElement {
  return (
    <div className="timeline-message message-assistant" data-streaming="true">
      <div className="message-head">
        <span className="message-author">Harness</span>
      </div>
      <div className="message-body">
        <span className="message-avatar avatar-assistant" aria-hidden="true">H</span>
        <div className="message-content">
          {message.content.length > 0 ? <Markdown text={message.content} /> : null}
          <span className="streaming-cursor" aria-hidden="true" />
        </div>
      </div>
    </div>
  )
}

/** 用户、Assistant（已结束）和系统消息；统一左对齐阅读流，角色由 avatar 标识。 */
function MessageBubbleImpl({ message }: { message: ConversationMessage }): ReactElement {
  if (message.role === "assistant") {
    return (
      <div className="timeline-message message-assistant" data-streaming={message.streaming ? "true" : undefined}>
        <div className="message-head">
          <span className="message-author">Harness</span>
        </div>
        <div className="message-body">
          <span className="message-avatar avatar-assistant" aria-hidden="true">H</span>
          <div className="message-content">
            {message.content.length > 0 ? <Markdown text={message.content} /> : null}
          </div>
        </div>
      </div>
    )
  }
  if (message.role === "system") {
    return (
      <div className="message-system" role="status">
        {message.content}
      </div>
    )
  }
  return (
    <div className="timeline-message message-user">
      <div className="message-head">
        <span className="message-author">你</span>
      </div>
      <div className="message-body">
        <span className="message-avatar avatar-user" aria-hidden="true">U</span>
        <div className="message-content">
          {message.content}
        </div>
      </div>
    </div>
  )
}

const MemoMessageBubble = memo(MessageBubbleImpl)
MemoMessageBubble.displayName = "MessageBubble"

/**
 * Tool 卡：默认折叠展示名称/状态/参数摘要；展开后显示参数与输出。
 *
 * Tool 详情使用 `<pre class="tool-details-pre">` 并自带滚动；外部样式需设置
 * max-height 与 overflow 才能让长输出在卡片内独立滚动而不是撑破时间线。
 */
function ToolCardImpl({
  tool,
  expanded,
  onToggle,
}: {
  tool: ToolCard
  expanded: boolean
  onToggle: (toolId: string) => void
}): ReactElement {
  const summary = argumentSummary(tool.arguments)
  return (
    <div className={`tool-card tool-card-${tool.status}`} data-tool-id={tool.id}>
      <button
        type="button"
        className="tool-card-header"
        aria-expanded={expanded}
        onClick={() => onToggle(tool.id)}
      >
        <span className="tool-card-name">{tool.name}</span>
        {summary ? <span className="tool-card-args">{summary}</span> : null}
        <span className="tool-card-status">{renderToolStatus(tool.status)}</span>
        <ChevronDown
          aria-hidden="true"
          focusable="false"
          className={expanded ? "tool-card-chevron expanded" : "tool-card-chevron"}
        />
      </button>
      {expanded ? (
        <div className="tool-details">
          {tool.arguments ? (
            <section className="tool-details-section">
              <h4 className="tool-details-title">参数</h4>
              <pre className="tool-details-pre">{tool.arguments}</pre>
            </section>
          ) : null}
          {tool.output ? (
            <section className="tool-details-section">
              <h4 className="tool-details-title">输出</h4>
              <pre className="tool-details-pre">{tool.output}</pre>
            </section>
          ) : null}
          {!tool.arguments && !tool.output ? (
            <p className="tool-details-empty">该调用尚无可显示内容。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

const MemoToolCard = memo(ToolCardImpl, (previous, next) => {
  return (
    previous.tool === next.tool
    && previous.expanded === next.expanded
    && previous.onToggle === next.onToggle
  )
})
MemoToolCard.displayName = "ToolCard"

/**
 * 折叠头参数摘要：只在该字段能安全收敛为单行文本时显示。
 * arguments 是 JSON 字符串时取 key: value 摘要；否则做空白归一化并截断。
 * 不显示任何虚构的 path / duration / 完成时间。
 */
function argumentSummary(argumentsText: string | undefined): string | null {
  if (!argumentsText) return null
  const trimmed = argumentsText.trim()
  if (!trimmed) return null
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>
    const entries = Object.entries(parsed)
    if (entries.length === 0) return null
    const summary = entries.map(([key, value]) => `${key}: ${stringifySummaryValue(value)}`).join(" · ")
    return truncateSingleLine(summary)
  } catch {
    return truncateSingleLine(trimmed)
  }
}

/** 把 JSON 标量/嵌套值收敛为短字符串；对象与数组只保留第一层规模。 */
function stringifySummaryValue(value: unknown): string {
  if (value === null) return "null"
  if (typeof value === "string") return value.length > 24 ? `${value.slice(0, 21)}…` : value
  if (typeof value === "object") {
    const size = Array.isArray(value) ? value.length : Object.keys(value).length
    return `{${size}}`
  }
  return String(value)
}

function truncateSingleLine(text: string): string {
  const singleLine = text.replace(/\s+/g, " ").trim()
  if (singleLine.length <= ARGUMENT_SUMMARY_MAX) return singleLine
  return `${singleLine.slice(0, ARGUMENT_SUMMARY_MAX - 1)}…`
}

/** Tool 状态标签：使用 lucide 图标而不是 Unicode，保持视觉一致。 */
function renderToolStatus(status: ToolCard["status"]): ReactNode {
  if (status === "running") {
    return (
      <span className="tool-status-running">
        <Loader2 aria-hidden="true" focusable="false" className="tool-status-icon spinning" />
        运行中
      </span>
    )
  }
  if (status === "failed") {
    return (
      <span className="tool-status-failed">
        <AlertTriangle aria-hidden="true" focusable="false" className="tool-status-icon" />
        失败
      </span>
    )
  }
  return (
    <span className="tool-status-completed">
      <Check aria-hidden="true" focusable="false" className="tool-status-icon" />
      已完成
    </span>
  )
}

/** 已完成 Interaction 的历史卡：只展示类型与终态标签；不渲染可交互控件。 */
function InteractionCardImpl({ interaction }: { interaction: InteractionCard }): ReactElement {
  const label = interactionTerminalLabel(interaction)
  return (
    <div
      className={`interaction-card interaction-${interaction.type} interaction-${interaction.status}`}
      data-request-id={interaction.id}
    >
      <div className="interaction-card-header">
        <span className="interaction-card-type">
          {interaction.type === "approval" ? "审批" : "询问"}
        </span>
        <span className={`interaction-card-status interaction-status-${label.tone}`}>
          {label.text}
        </span>
      </div>
      {interaction.description ? (
        <p className="interaction-card-description">{interaction.description}</p>
      ) : null}
      {interaction.question ? (
        <p className="interaction-card-question">{interaction.question}</p>
      ) : null}
    </div>
  )
}

const MemoInteractionCard = memo(InteractionCardImpl)
MemoInteractionCard.displayName = "InteractionCard"

/**
 * Interaction 终态展示标签。
 *
 * pending 不进入历史区（被 `live-interaction-slot` 取代），但保留分支避免
 * 状态类型未来增加时出现未匹配分支；cancelled 包含本地超时与远端失效两种来源。
 */
function interactionTerminalLabel(interaction: InteractionCard): {
  text: string
  tone: "ok" | "reject" | "timeout" | "neutral"
} {
  switch (interaction.status) {
    case "approved":
      return { text: "已允许", tone: "ok" }
    case "rejected":
      return { text: "已拒绝", tone: "reject" }
    case "answered":
      return { text: "已回答", tone: "ok" }
    case "cancelled":
      return { text: "已超时", tone: "timeout" }
    case "resolved":
      return { text: "已解决", tone: "neutral" }
    case "pending":
      return { text: "等待中", tone: "neutral" }
  }
}
