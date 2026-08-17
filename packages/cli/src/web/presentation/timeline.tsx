/** Web Timeline：消息、Tool 卡和已完成 Interaction 卡的统一时间线，并托管滚动行为。 */
/** @jsxImportSource react */

import { AlertTriangle, Check, ChevronDown, Loader2, MessageCircle, Sparkles } from "lucide-react"
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
} from "react"
import { activityLabel, interactionStatusLabel, toolStatusLabel } from "../../presentation-shared/timeline-presenter"
import { progressPhaseLabel } from "../../presentation-shared/timeline-presenter"
import { formatElapsed } from "../../presentation-shared/formatters"
import { toolArgumentSummary } from "../../presentation-shared/tool-output-policy"
import {
  COMPOSE_STAGE_LABELS,
  activityGroupSubtitle,
  activityGroupTitle,
  composeLiveStatusLine,
  isGroupExpandedByDefault,
  segmentTimeline,
  type TimelineActivityGroup,
} from "../../presentation-shared/timeline-activity-groups"

import type {
  ComposeProjection,
  ConversationMessage,
  InteractionCard,
  ReasoningCard,
  TimelineItem,
  ToolCard,
} from "../../interactive/state"
import type { InteractiveSnapshot } from "../../interactive/types"
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
  // home/idle 即「就绪」：就绪即沉默，底部状态区留白；其余活动标签（失败/取消/压缩等瞬态）照常展示。
  const idleActivity = snapshot.interactive.activity.kind === "home" || snapshot.interactive.activity.kind === "idle"
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
    if (item.type === "reasoning" && !item.reasoning.active) return false
    if (item.type === "message" && item.message.role === "assistant" && !item.message.streaming && !item.message.content.trim()) {
      return false
    }
    return true
  })
  const segments = segmentTimeline(visibleItems)
  const [expandedActivities, setExpandedActivities] = useState<ReadonlySet<string>>(() => new Set())
  const [collapsedActivities, setCollapsedActivities] = useState<ReadonlySet<string>>(() => new Set())

  const isExpanded = useCallback((group: TimelineActivityGroup): boolean => {
    if (expandedActivities.has(group.key)) return true
    if (collapsedActivities.has(group.key)) return false
    return isGroupExpandedByDefault(group)
  }, [collapsedActivities, expandedActivities])

  const toggleGroup = useCallback((group: TimelineActivityGroup) => {
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
  }, [isExpanded])

  const compose = snapshot.interactive.composeState
  const currentTask = compose?.tasks.find(task => task.status === "running")
  const phaseLabel = progressPhaseLabel(snapshot.interactive.runProgress?.phase ?? "preparing")
  const liveLine = compose
    ? `${composeLiveStatusLine({
      stage: compose.stage,
      taskTitle: currentTask?.title,
      phaseLabel,
      elapsedLabel: formatElapsed(elapsedMs),
    })} · Esc 取消`
    : `${phaseLabel} · 已运行 ${formatElapsed(elapsedMs)} · Esc 取消`

  // Compose 分段器会把无 scope 的 Build 条目拆成多个 flat segment；先恢复连续 flat 条目，
  // 再交给 Agent 分组渲染，保证 Build 与 Compose 两条路径都能共享同一套气泡结构。
  const renderedSegments: ReactNode[] = []
  let flatItems: TimelineItem[] = []
  const flushFlatItems = () => {
    for (const items of groupTimelineItems(flatItems)) {
      const first = items[0]
      if (!first) continue
      renderedSegments.push(
        <TimelineGroup
          key={timelineItemKey(first)}
          items={items}
          expandedTools={snapshot.expandedTools}
          onToggleTool={handleToggleTool}
        />,
      )
    }
    flatItems = []
  }
  for (const segment of segments) {
    if (segment.kind === "flat") {
      flatItems.push(segment.item)
      continue
    }
    flushFlatItems()
    const group = segment.group
    renderedSegments.push(
      <ActivityGroup
        key={group.key}
        group={group}
        expanded={isExpanded(group)}
        onToggle={() => toggleGroup(group)}
        expandedTools={snapshot.expandedTools}
        onToggleTool={handleToggleTool}
      />,
    )
  }
  flushFlatItems()

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
        renderedSegments
      )}
      <div className="live-interaction-slot" data-pending-request-id={pendingRequestId ?? undefined} />
      {/* 当前阶段状态放在时间线活动区附近，避免长历史把进度顶出视口。 */}
      <div className="run-status-live" aria-live="polite">
        {activeRun ? (
          <div
            className="run-progress"
            role="status"
            aria-live="polite"
            data-phase={snapshot.interactive.runProgress?.phase ?? "preparing"}
          >
            <Loader2 aria-hidden="true" focusable="false" className="run-progress-spinner spinning" />
            <span>{liveLine}</span>
          </div>
        ) : idleActivity ? null : activityLabel(snapshot.interactive.activity.kind)}
      </div>
      {renderComposeProgress(snapshot.interactive)}
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

/** Compose activity 分组：终态默认折叠，Enter/Space 切换，ARIA 暴露展开状态。 */
function ActivityGroup({
  group,
  expanded,
  onToggle,
  expandedTools,
  onToggleTool,
}: {
  group: TimelineActivityGroup
  expanded: boolean
  onToggle: () => void
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
}): ReactElement {
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      onToggle()
    }
  }
  return (
    <section className="timeline-activity-group" data-terminal={group.terminal ? "true" : "false"}>
      <button
        type="button"
        className="timeline-activity-header"
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={onKeyDown}
      >
        <span className="timeline-activity-chevron" aria-hidden="true">{expanded ? "▼" : "▶"}</span>
        <span className="timeline-activity-title">{activityGroupTitle(group)}</span>
        <span className="timeline-activity-subtitle">{activityGroupSubtitle(group)}</span>
      </button>
      {expanded ? (
        <div className="timeline-activity-body">
          {groupTimelineItems(group.items).map(items => (
            <TimelineGroup
              key={timelineItemKey(items[0] as TimelineItem)}
              items={items}
              expandedTools={expandedTools}
              onToggleTool={onToggleTool}
            />
          ))}
        </div>
      ) : group.summary ? (
        <div className="timeline-activity-collapsed-summary">{group.summary.text}</div>
      ) : null}
    </section>
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

/** 思考文本折叠时只展示首行，减少长推理占用的阅读空间。 */
function firstLine(text: string): string {
  const line = text.split("\n")[0] ?? ""
  return line
}

/** 生成稳定的 React key：每种 timeline item 用其身份字段，跨 run/activity 也不冲突。 */
function timelineItemKey(item: TimelineItem): string {
  switch (item.type) {
    case "message":
      return `message:${item.message.id}`
    case "tool":
      return `tool:${item.tool.runId}:${item.tool.executionId ?? "root"}:${item.tool.activityId ?? "root"}:${item.tool.id}`
    case "reasoning":
      return `reasoning:${item.reasoning.id}`
    case "interaction":
      return `interaction:${item.interaction.runId}:${item.interaction.id}`
    case "compose-summary":
      return `compose-summary:${item.summary.id}`
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

/** 将 Agent 回复前后的 reasoning 及连续 tool 记录收进同一个消息气泡，并保持协议顺序。 */
function groupTimelineItems(items: TimelineItem[]): TimelineItem[][] {
  const groups: TimelineItem[][] = []
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index]
    if (!isAssistantMessage(item) && !isAgentContinuation(item)) {
      groups.push([item])
      continue
    }
    const group: TimelineItem[] = [item]
    let hasAssistant = isAssistantMessage(item)
    while (index + 1 < items.length) {
      const next = items[index + 1]
      if (!next) break
      if (isAssistantMessage(next)) {
        if (hasAssistant) break
        hasAssistant = true
      } else if (!isAgentContinuation(next)) {
        break
      }
      index += 1
      group.push(next)
    }
    groups.push(group)
  }
  return groups
}

function isAssistantMessage(item: TimelineItem): item is Extract<TimelineItem, { type: "message" }> {
  return item.type === "message" && item.message.role === "assistant"
}

function isAgentContinuation(item: TimelineItem): boolean {
  return item.type === "reasoning" || item.type === "tool"
}

function TimelineGroup({
  items,
  expandedTools,
  onToggleTool,
}: {
  items: TimelineItem[]
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
}): ReactElement {
  const assistantIndex = items.findIndex(isAssistantMessage)
  const reasoningItems = items.filter((item): item is Extract<TimelineItem, { type: "reasoning" }> => item.type === "reasoning")
  if (assistantIndex < 0) {
    const toolItems = items.filter((item): item is Extract<TimelineItem, { type: "tool" }> => item.type === "tool")
    if (toolItems.length === 0 && reasoningItems.length === 0 && items.length === 1) {
      const item = items[0]
      if (item) return <TimelineRow item={item} expandedTools={expandedTools} onToggleTool={onToggleTool} />
    }
    if (toolItems.length > 0) {
      return (
        <div className="timeline-agent-group" data-tool-grouped="true">
          <AgentGroupHeader />
          {items.map(item => {
            if (item.type === "reasoning") {
              return <ReasoningRow key={timelineItemKey(item)} reasoning={item.reasoning} />
            }
            return (
              <TimelineRow
                key={timelineItemKey(item)}
                item={item}
                expandedTools={expandedTools}
                onToggleTool={onToggleTool}
              />
            )
          })}
        </div>
      )
    }
    return <AgentThinkingBubble reasoning={reasoningItems.map(item => item.reasoning)} />
  }

  const assistant = items[assistantIndex]
  if (!assistant || assistant.type !== "message") return <></>
  if (items.length === 1) return <TimelineRow item={assistant} expandedTools={expandedTools} onToggleTool={onToggleTool} />

  return (
    <div className="timeline-agent-group" data-tool-grouped="true">
      <AgentGroupHeader message={assistant.message} />
      {items.map(item => {
        if (item.type === "reasoning") {
          return <ReasoningRow key={timelineItemKey(item)} reasoning={item.reasoning} />
        }
        if (item.type === "message" && item.message.role === "assistant") {
          return (
            <TimelineRow
              key={timelineItemKey(item)}
              item={item}
              expandedTools={expandedTools}
              onToggleTool={onToggleTool}
              grouped
            />
          )
        }
        return (
          <TimelineRow
            key={timelineItemKey(item)}
            item={item}
            expandedTools={expandedTools}
            onToggleTool={onToggleTool}
          />
        )
      })}
    </div>
  )
}

/** 顶层分派：根据 item 类型选 memo 包装；流式 Assistant 不 memo 以接收 in-place 更新。 */
function TimelineRowImpl({
  item,
  expandedTools,
  onToggleTool,
  grouped = false,
}: {
  item: TimelineItem
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
  grouped?: boolean
}): ReactElement {
  if (item.type === "message") {
    if (item.message.role === "assistant" && item.message.streaming === true) {
      return <StreamingAssistantBubble message={item.message} showHeader={!grouped} />
    }
    return <MemoMessageBubble message={item.message} showHeader={!grouped} />
  }
  if (item.type === "tool") {
    return (
      <div className="timeline-tool tool-step">
        <MemoToolCard
          tool={item.tool}
          expanded={expandedTools.has(toolKey(item.tool.runId, item.tool.id))}
          onToggle={onToggleTool}
        />
      </div>
    )
  }
  if (item.type === "reasoning") {
    return <ReasoningRow reasoning={item.reasoning} />
  }
  if (item.type === "compose-summary") {
    const stageKey = item.summary.composeScope?.stage
    const stage = stageKey ? (COMPOSE_STAGE_LABELS[stageKey] ?? stageKey) : "compose"
    return (
      <div className="timeline-compose-summary" role="status">
        <div className="compose-summary-header">{`阶段摘要 · ${stage} · ${item.summary.status}`}</div>
        <div className="compose-summary-text">{item.summary.text}</div>
      </div>
    )
  }
  return (
    <div className="timeline-interaction approval-history-card">
      <MemoInteractionCard interaction={item.interaction} />
    </div>
  )
}

function AgentGroupHeader({ message }: { message?: ConversationMessage }): ReactElement {
  return (
    <div className="timeline-agent-group-header agent-message-card">
      <AgentMessageHeader timestampMs={message?.createdAtMs} />
    </div>
  )
}

function AgentMessageHeader({ timestampMs }: { timestampMs?: number }): ReactElement {
  return (
    <div className="message-head">
      <span className="message-avatar avatar-assistant" aria-hidden="true"><Sparkles size={16} strokeWidth={1.8} /></span>
      <span className="message-author-group">
        <span className="message-author">Agent</span>
        <MessageTimestamp timestampMs={timestampMs} />
      </span>
    </div>
  )
}

/** Agent 尚未输出正文时也使用同一类模型气泡承载思考内容。 */
function AgentThinkingBubble({ reasoning }: { reasoning: ReasoningCard[] }): ReactElement {
  return (
    <div className="timeline-message message-assistant agent-message-card agent-thinking-card" data-streaming="true">
      <AgentMessageHeader />
      <div className="message-body">
        {reasoning.map(item => <ReasoningRow key={item.id} reasoning={item} />)}
      </div>
    </div>
  )
}

/** 时间线中的思考条目：进行中显示全文，完成后由 Timeline 过滤掉。 */
function ReasoningRow({ reasoning }: { reasoning: ReasoningCard }): ReactElement {
  const [expanded, setExpanded] = useState(false)
  const first = firstLine(reasoning.text)
  const showFull = expanded || reasoning.active
  return (
    <div className="reasoning reasoning-card" role="status" aria-live="polite" data-active={reasoning.active}>
      <div className="reasoning-header" onClick={() => setExpanded(current => !current)}>
        {reasoning.active ? (
          <Loader2 aria-hidden="true" focusable="false" className="run-progress-spinner spinning" />
        ) : (
          <span className="reasoning-dot" aria-hidden="true">◆</span>
        )}
        <span className="reasoning-title">{reasoning.active ? "思考中" : "思考"}</span>
        {reasoning.active ? null : (
          <button type="button" className="reasoning-toggle" onClick={() => setExpanded(current => !current)}>
            {expanded ? "收起" : "展开"}
          </button>
        )}
      </div>
      <div className="reasoning-text">{showFull ? reasoning.text : first}</div>
    </div>
  )
}

const TimelineRow = memo(TimelineRowImpl)
TimelineRow.displayName = "TimelineRow"

/** 流式 Assistant 消息：内容随时 in-place 更新；不 memo 才能接收每帧变更。 */
function StreamingAssistantBubble({
  message,
  showHeader,
}: {
  message: ConversationMessage
  showHeader: boolean
}): ReactElement {
  return (
    <div className="timeline-message message-assistant agent-message-card" data-streaming="true">
      {showHeader ? <AgentMessageHeader timestampMs={message.createdAtMs} /> : null}
      <div className="message-body">
        <div className="message-content">
          {message.content.length > 0 ? <Markdown text={message.content} /> : null}
          <span className="streaming-cursor" aria-hidden="true" />
        </div>
      </div>
    </div>
  )
}

/** 用户、Assistant（已结束）和系统消息；统一左对齐阅读流，角色由 avatar 标识。 */
function MessageBubbleImpl({
  message,
  showHeader,
}: {
  message: ConversationMessage
  showHeader: boolean
}): ReactElement {
  if (message.role === "assistant") {
    return (
      <div className="timeline-message message-assistant agent-message-card" data-streaming={message.streaming ? "true" : undefined}>
        {showHeader ? <AgentMessageHeader timestampMs={message.createdAtMs} /> : null}
        <div className="message-body">
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
    <div className="timeline-message message-user user-message-card">
      <div className="message-head">
        <span className="message-avatar avatar-user" aria-hidden="true"><MessageCircle size={16} strokeWidth={1.8} /></span>
        <span className="message-author-group">
          <span className="message-author">用户</span>
          <MessageTimestamp timestampMs={message.createdAtMs} />
        </span>
      </div>
      <div className="message-body">
        <div className="message-content">
          {message.content}
        </div>
      </div>
    </div>
  )
}

/** 将消息首次进入时间线的毫秒时间戳显示为本地时区的小时和分钟。 */
function MessageTimestamp({ timestampMs }: { timestampMs?: number }): ReactElement | null {
  if (typeof timestampMs !== "number" || !Number.isFinite(timestampMs)) return null
  const date = new Date(timestampMs)
  if (Number.isNaN(date.getTime())) return null
  return (
    <time className="message-time" dateTime={date.toISOString()}>
      {date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
    </time>
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
          {interaction.type === "approval" ? "审批" : interaction.type === "directory_trust" ? "目录信任" : "询问"}
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


/** 活动投影优先；失败/完成后退回终态摘要快照，保证画面不空白。 */
function renderComposeProgress(interactive: InteractiveSnapshot): React.ReactNode {
  const live = interactive.composeState
  if (live) return <ComposeProgress state={live} />
  const summary = interactive.lastRun?.composeSummary
  if (!summary) return null
  return <ComposeProgress state={summary} />
}

/** Compose 五阶段/当前任务/evidence/blocked 的只读进度条。 */
function ComposeProgress({ state }: { state: ComposeProjection }) {
  const currentTask = state.tasks.find(task => task.status === "running" || task.status === "pending")
  const failedEvidence = state.evidence.find(item => item.status === "failed")
  return (
    <div className="compose-progress" role="status" aria-label="Compose 工作流进度">
      <div className="compose-stages">
        {state.stages.map(stage => (
          <span key={stage.id} className={`compose-stage compose-stage-${stage.status}`}>
            {COMPOSE_STAGE_LABELS[stage.id] ?? stage.id}
            {stage.status === "running" ? "…" : stage.status === "passed" ? "✓" : ""}
          </span>
        ))}
        <span className="compose-revision">rev {state.revision}</span>
      </div>
      {currentTask ? <div className="compose-task">任务：{currentTask.title}</div> : null}
      {failedEvidence ? <div className="compose-task compose-task-failed">验证：{failedEvidence.label}</div> : null}
      {state.blockedReason ? <div className="compose-blocked">阻塞：{state.blockedReason}</div> : null}
    </div>
  )
}
