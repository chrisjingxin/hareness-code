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
import { activityLabel, interactionStatusLabel, toolStatusLabel } from "../../presentation-shared/timeline-presenter"
import { progressPhaseLabel } from "../../presentation-shared/timeline-presenter"
import { formatElapsed } from "../../presentation-shared/formatters"
import { toolArgumentSummary } from "../../presentation-shared/tool-output-policy"

import type {
  ConversationMessage,
  InteractionCard,
  TimelineItem,
  ToolCard,
} from "../../interactive/state"
import type { WebAdapterSnapshot, WebIntent, WebScrollRequest } from "../application/adapter"
import { toolKey } from "../application/adapter"
import { Markdown } from "./markdown"

/** 自动滚动判定阈值：用户视口底部离容器底部的距离小于该值即视为靠近底部。 */
const BOTTOM_THRESHOLD_PX = 48

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
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
}): ReactElement {
  const timeline = snapshot.interactive.timeline
  const pendingRequestId = snapshot.interactive.interaction?.requestId ?? null
  const containerRef = useRef<HTMLDivElement | null>(null)
  const isNearBottomRef = useRef<boolean>(true)
  const lastScrollRequestRef = useRef<WebScrollRequest>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const activeRun = snapshot.interactive.activeRun
  const baseElapsedMs = snapshot.interactive.runProgress?.elapsedMs
  const elapsedMs = useLiveElapsed(Boolean(activeRun), baseElapsedMs)

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
    (runId: string, toolId: string) => {
      // Tool 展开是纯本地表现（不修改 Agent 状态），不受只读/断连状态阻断。
      void dispatch({ type: "tool-toggle", runId, toolId })
    },
    [dispatch],
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
      {activeRun && snapshot.interactive.reasoningSummary ? (
        <div className="reasoning-summary" role="status" aria-label="思考摘要（仅本次运行）">
          <div className="reasoning-summary-title">思考摘要（仅本次运行）</div>
          <div className="reasoning-summary-text">{snapshot.interactive.reasoningSummary}</div>
        </div>
      ) : null}
      <div className="run-status-live" aria-live="polite">
        {activeRun ? (
          <div
            className="run-progress"
            role="status"
            aria-live="polite"
            data-phase={snapshot.interactive.runProgress?.phase ?? "preparing"}
          >
            <Loader2 aria-hidden="true" focusable="false" className="run-progress-spinner spinning" />
            <span>
              {progressPhaseLabel(snapshot.interactive.runProgress?.phase ?? "preparing")}
              {" · 已运行 "}
              {formatElapsed(elapsedMs)}
              {" · Esc 取消"}
            </span>
          </div>
        ) : activityLabel(snapshot.interactive.activity.kind)}
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

/** 以最近一次 Host elapsed_ms 为基准连续更新活动时长；无 active Run 时停止计时。 */
function useLiveElapsed(active: boolean, baseElapsedMs: number | undefined): number {
  const [elapsedMs, setElapsedMs] = useState(baseElapsedMs ?? 0)
  useEffect(() => {
    const base = Math.max(0, baseElapsedMs ?? 0)
    setElapsedMs(base)
    if (!active) return
    const startedAt = Date.now() - base
    const timer = window.setInterval(() => setElapsedMs(Math.max(0, Date.now() - startedAt)), 1_000)
    return () => window.clearInterval(timer)
  }, [active, baseElapsedMs])
  return elapsedMs
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
  onToggleTool: (runId: string, toolId: string) => void
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
          expanded={expandedTools.has(toolKey(item.tool.runId, item.tool.id))}
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
  onToggle: (runId: string, toolId: string) => void
}): ReactElement {
  const summary = toolArgumentSummary(tool.arguments)
  return (
    <div className={`tool-card tool-card-${tool.status}`} data-tool-id={tool.id}>
      <button
        type="button"
        className="tool-card-header"
        aria-expanded={expanded}
        onClick={() => onToggle(tool.runId, tool.id)}
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

/** Tool 状态标签：使用 lucide 图标而不是 Unicode，保持视觉一致。 */
function renderToolStatus(status: ToolCard["status"]): ReactNode {
  const label = toolStatusLabel(status)
  if (status === "running") {
    return (
      <span className="tool-status-running">
        <Loader2 aria-hidden="true" focusable="false" className="tool-status-icon spinning" />
        {label}
      </span>
    )
  }
  if (status === "failed") {
    return (
      <span className="tool-status-failed">
        <AlertTriangle aria-hidden="true" focusable="false" className="tool-status-icon" />
        {label}
      </span>
    )
  }
  return (
    <span className="tool-status-completed">
      <Check aria-hidden="true" focusable="false" className="tool-status-icon" />
      {label}
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
  const tones: Record<InteractionCard["status"], "ok" | "reject" | "timeout" | "neutral"> = {
    approved: "ok",
    rejected: "reject",
    answered: "ok",
    cancelled: "timeout",
    resolved: "neutral",
    pending: "neutral",
  }
  return { text: interactionStatusLabel(interaction.status), tone: tones[interaction.status] }
}
