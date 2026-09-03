/** Web Timeline：消息、Tool 卡和已完成 Interaction 卡的统一时间线，并托管滚动行为。 */
/** @jsxImportSource react */

import {
  AlertTriangle,
  Bot,
  Brain,
  Check,
  ChevronDown,
  Code2,
  Copy,
  FilePenLine,
  FileText,
  Folder,
  FolderOpen,
  Globe,
  ListTodo,
  Loader2,
  MessageCircle,
  MessageCircleQuestion,
  Search,
  Sparkles,
  Terminal,
  Trash2,
  Wrench,
  type LucideIcon,
} from "lucide-react"
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
import { childTimelineEmptyMessage } from "../../presentation-shared/child-timeline-empty"
import { formatElapsed } from "../../presentation-shared/formatters"
import {
  hasTaskDispatchView,
  parseTaskDispatch,
  TASK_DISPATCH_RESULT_LABEL,
  TASK_DISPATCH_TASK_LABEL,
  taskDispatchLabel,
  taskDispatchPrimaryLine,
  type TaskDispatchView,
} from "../../presentation-shared/task-dispatch"
import { toolDisplay, toolPrimaryArgument, type ToolIconName } from "../../presentation-shared/tool-display-policy"
import { prettifyJson, toolOutputView, type ToolOutputView } from "../../presentation-shared/tool-output-render"
import { FileTypeIcon } from "./workspace-sidebar/file-type-icon"
import {
  COMPOSE_STAGE_LABELS,
  activityGroupSubtitle,
  activityGroupTitle,
  composeLiveStatusLine,
  isGroupExpandedByDefault,
  segmentTimeline,
  type TimelineActivityGroup,
} from "../../presentation-shared/timeline-activity-groups"
import {
  composeStepperHint,
  composeStepperSegments,
  composeStepperTrackFilled,
  resolveComposeProgress,
} from "../../presentation-shared/compose-progress-bar"

import type {
  ComposeProjection,
  ConversationMessage,
  InteractionCard,
  ReasoningCard,
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
  /** 上滚期间是否有新内容到达：决定按钮是「有新输出」强调态还是中性「回到底部」。 */
  const [hasNewOutput, setHasNewOutput] = useState(false)
  const activeRun = snapshot.interactive.activeRun
  // home/idle 即「就绪」：就绪即沉默，底部状态区留白；其余活动标签（失败/取消/压缩等瞬态）照常展示。
  const idleActivity = snapshot.interactive.activity.kind === "home" || snapshot.interactive.activity.kind === "idle"
  const baseElapsedMs = snapshot.interactive.runProgress?.elapsedMs
  const elapsedMs = useLiveElapsed(Boolean(activeRun), baseElapsedMs)

  /**
   * 真正的滚动容器是父级 .timeline-scroll（overflow-y: auto）；.timeline 是不滚动的内容层。
   * 独立渲染（无滚动父级）时回退到自身，保证测试与嵌入场景行为一致。
   */
  const resolveScroller = useCallback((): HTMLElement | null => {
    const el = containerRef.current
    if (!el) return null
    return (el.closest(".timeline-scroll") as HTMLElement | null) ?? el
  }, [])

  /** 把滚动容器滚到底部；调用方负责在调用后维护 near-bottom 状态。 */
  const scrollContainerToBottom = useCallback(() => {
    const scroller = resolveScroller()
    if (!scroller) return
    scroller.scrollTop = scroller.scrollHeight
  }, [resolveScroller])

  /**
   * 滚动事件必须挂在滚动容器上（React onScroll 不冒泡父级滚动）：
   * 维护 near-bottom ref 与「回到底部」按钮可见性，避免滚动过程触发额外渲染。
   */
  useEffect(() => {
    const scroller = resolveScroller()
    if (!scroller) return
    const onScroll = (): void => {
      const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
      const near = distance <= BOTTOM_THRESHOLD_PX
      isNearBottomRef.current = near
      if (near) setHasNewOutput(false)
      setShowScrollButton(prev => (prev === !near ? prev : !near))
    }
    scroller.addEventListener("scroll", onScroll, { passive: true })
    return () => scroller.removeEventListener("scroll", onScroll)
  }, [resolveScroller])

  /** Adapter 显式声明滚动意图时无条件跳到底部；空 → 非空转换都触发。 */
  useEffect(() => {
    const next = snapshot.scrollRequest
    if (next === lastScrollRequestRef.current) return
    lastScrollRequestRef.current = next
    if (next === null) return
    scrollContainerToBottom()
    isNearBottomRef.current = true
    setHasNewOutput(false)
    setShowScrollButton(false)
  }, [snapshot.scrollRequest, scrollContainerToBottom])

  /** 时间线内容变化时，若用户原本就在底部则继续跟随；否则保持位置并把按钮升级为「有新输出」。 */
  useLayoutEffect(() => {
    if (isNearBottomRef.current) {
      scrollContainerToBottom()
    } else {
      setHasNewOutput(true)
    }
  }, [timeline, scrollContainerToBottom])

  const handleScrollToBottom = useCallback(() => {
    scrollContainerToBottom()
    isNearBottomRef.current = true
    setHasNewOutput(false)
    setShowScrollButton(false)
  }, [scrollContainerToBottom])

  const handleToggleTool = useCallback(
    (runId: string, toolId: string) => {
      // Tool 展开是纯本地表现（不修改 Agent 状态），不受只读/断连状态阻断。
      void dispatch({ type: "tool-toggle", runId, toolId })
    },
    [dispatch],
  )

  const handleOpenChildTimeline = useCallback(
    (executionId: string) => {
      void dispatch({ type: "child-timeline-open", executionId })
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
  const childEmptyMessage = childTimelineEmptyMessage(
    snapshot.interactive.childTimelineExecutionId,
    visibleItems.length > 0,
    activeRun,
  )
  const segments = segmentTimeline(visibleItems)
  /** 流式光标只挂在最后一项（生成位置）；运行中历史文本段后面已有工具/新段时不再闪烁。 */
  const lastVisible = visibleItems[visibleItems.length - 1]
  const liveKey = lastVisible ? timelineItemKey(lastVisible) : null
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
  const phaseLabel = progressPhaseLabel(snapshot.interactive.runProgress?.phase ?? "preparing")
  const liveLine = compose
    ? `${composeLiveStatusLine({
      stage: compose.currentStage,
      taskTitle: null,
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
          onOpenChildTimeline={handleOpenChildTimeline}
          liveKey={liveKey}
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
        onOpenChildTimeline={handleOpenChildTimeline}
        liveKey={liveKey}
      />,
    )
  }
  flushFlatItems()

  const composeProgress = resolveComposeProgress(snapshot.interactive)

  return (
    <div className="timeline-column">
    {composeProgress ? <ComposeProgress state={composeProgress} /> : null}
    <div
      ref={containerRef}
      className="timeline"
      role="log"
      aria-relevant="additions"
      aria-label="对话与工具时间线"
    >
      {snapshot.interactive.childTimelineExecutionId ? (
        <div className="child-timeline-banner" role="region" aria-label="子代理时间线">
          <span className="child-timeline-title">子代理时间线（只读）</span>
          <button
            type="button"
            className="child-timeline-back-btn"
            onClick={() => dispatch({ type: "child-timeline-leave" })}
          >
            返回主对话
          </button>
        </div>
      ) : null}
      {snapshot.interactive.currentThreadId !== null && visibleItems.length > 0 ? (
        <div className="timeline-header">THREAD · {timeline.length} 项记录</div>
      ) : null}
      {visibleItems.length === 0 ? (
        <div className={childEmptyMessage ? "timeline-empty child-timeline-empty" : "timeline-empty"} role="status">
          {childEmptyMessage ?? "发送第一条消息后，这里会显示 Agent 的回答、工具调用与审批记录。"}
        </div>
      ) : (
        renderedSegments
      )}
      <div className="live-interaction-slot" data-pending-request-id={pendingRequestId ?? undefined} />
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
      {showScrollButton ? (
        <button
          type="button"
          className="scroll-to-bottom"
          data-new={hasNewOutput}
          onClick={handleScrollToBottom}
          aria-label={hasNewOutput ? "跳到最新输出" : "回到底部"}
        >
          {hasNewOutput ? "有新输出" : "回到底部"}
        </button>
      ) : null}
    </div>
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
  onOpenChildTimeline,
  liveKey,
}: {
  group: TimelineActivityGroup
  expanded: boolean
  onToggle: () => void
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
  onOpenChildTimeline?: (executionId: string) => void
  liveKey: string | null
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
              onOpenChildTimeline={onOpenChildTimeline}
              liveKey={liveKey}
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
  onOpenChildTimeline,
  liveKey,
}: {
  items: TimelineItem[]
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
  onOpenChildTimeline?: (executionId: string) => void
  liveKey: string | null
}): ReactElement {
  const assistantIndex = items.findIndex(isAssistantMessage)
  const reasoningItems = items.filter((item): item is Extract<TimelineItem, { type: "reasoning" }> => item.type === "reasoning")
  if (assistantIndex < 0) {
    const toolItems = items.filter((item): item is Extract<TimelineItem, { type: "tool" }> => item.type === "tool")
    if (toolItems.length === 0 && reasoningItems.length === 0 && items.length === 1) {
      const item = items[0]
      if (item) return <TimelineRow item={item} expandedTools={expandedTools} onToggleTool={onToggleTool} onOpenChildTimeline={onOpenChildTimeline} liveKey={liveKey} />
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
                onOpenChildTimeline={onOpenChildTimeline}
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
  if (items.length === 1) return <TimelineRow item={assistant} expandedTools={expandedTools} onToggleTool={onToggleTool} onOpenChildTimeline={onOpenChildTimeline} liveKey={liveKey} />

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
              onOpenChildTimeline={onOpenChildTimeline}
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
            onOpenChildTimeline={onOpenChildTimeline}
            liveKey={liveKey}
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
  onOpenChildTimeline,
  grouped = false,
  liveKey = null,
}: {
  item: TimelineItem
  expandedTools: ReadonlySet<string>
  onToggleTool: (runId: string, toolId: string) => void
  onOpenChildTimeline?: (executionId: string) => void
  grouped?: boolean
  liveKey?: string | null
}): ReactElement {
  if (item.type === "message") {
    if (item.message.role === "assistant" && item.message.streaming === true) {
      return <StreamingAssistantBubble message={item.message} showHeader={!grouped} showCursor={timelineItemKey(item) === liveKey} />
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
          onOpenChildTimeline={onOpenChildTimeline}
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
  showCursor,
}: {
  message: ConversationMessage
  showHeader: boolean
  /** 流式光标只挂在时间线最后一项（生成位置）；后续已有工具/新段的历史段不再闪烁。 */
  showCursor: boolean
}): ReactElement {
  return (
    <div className="timeline-message message-assistant agent-message-card" data-streaming="true">
      {showHeader ? <AgentMessageHeader timestampMs={message.createdAtMs} /> : null}
      <div className="message-body">
        <div className="message-content">
          {message.content.length > 0 ? <Markdown text={message.content} /> : null}
          {showCursor ? <span className="streaming-cursor" aria-hidden="true" /> : null}
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

/** 工具图标语义名 → lucide 组件的 Web 端映射；图标语义本身定义在 presentation-shared。 */
const TOOL_ICONS: Record<ToolIconName, LucideIcon> = {
  "file-read": FileText,
  "file-write": FilePenLine,
  "file-delete": Trash2,
  folder: FolderOpen,
  search: Search,
  terminal: Terminal,
  globe: Globe,
  brain: Brain,
  code: Code2,
  plan: ListTodo,
  agents: Bot,
  question: MessageCircleQuestion,
  wrench: Wrench,
}

/**
 * Tool 行：默认折叠为融入阅读流的单行（图标 + 动词标签 + 主参数 + 状态 + 常驻 chevron）；
 * 展开后在引导竖线内以同级卡片展示输出与参数（2026-08-18 与用户确认的结构化方向）。
 *
 * 输出卡片：头部条（标题 + 行数 + 复制）+ 内容体；超过折叠阈值时钳制限高并给
 * 「展开全部/收起」就地切换。内容体的 max-height/overflow 由 data-clamped 样式表达，
 * 长输出在详情区内独立滚动而不是撑破时间线。
 */
function ToolCardImpl({
  tool,
  expanded,
  onToggle,
  onOpenChildTimeline,
}: {
  tool: ToolCard
  expanded: boolean
  onToggle: (runId: string, toolId: string) => void
  onOpenChildTimeline?: (executionId: string) => void
}): ReactElement {
  const display = toolDisplay(tool.name)
  const dispatch = tool.name === "task" ? parseTaskDispatch(tool.arguments) : null
  const friendlyDispatch = dispatch !== null && hasTaskDispatchView(dispatch)
  const label = friendlyDispatch ? taskDispatchLabel(dispatch) : display.label
  const primary = friendlyDispatch
    ? taskDispatchPrimaryLine(dispatch)
    : toolPrimaryArgument(tool.name, tool.arguments)
  const Icon = TOOL_ICONS[display.icon]
  return (
    <div className={`tool-row tool-row-${tool.status}`} data-tool-id={tool.id} data-tone={display.tone}>
      <button
        type="button"
        className="tool-row-header"
        aria-expanded={expanded}
        onClick={() => onToggle(tool.runId, tool.id)}
      >
        <Icon aria-hidden="true" focusable="false" className="tool-row-icon" />
        <span className="tool-row-label" title={tool.name}>{label}</span>
        {primary ? <span className="tool-row-args">{primary}</span> : null}
        {renderToolStatus(tool.status)}
        <ChevronDown
          aria-hidden="true"
          focusable="false"
          className={expanded ? "tool-row-chevron expanded" : "tool-row-chevron"}
        />
      </button>
      {expanded ? (
        <div className="tool-row-details">
          {friendlyDispatch ? (
            <TaskDispatchSection
              view={dispatch}
              output={tool.output}
              childExecutionId={tool.childExecutionId}
              onOpenChildTimeline={onOpenChildTimeline}
            />
          ) : null}
          {tool.output && !friendlyDispatch ? <ToolOutputSection toolName={tool.name} output={tool.output} argumentsText={tool.arguments} /> : null}
          {tool.arguments && !friendlyDispatch ? <ToolArgumentsSection arguments={tool.arguments} /> : null}
          {!tool.arguments && !tool.output ? (
            <p className="tool-row-empty">该调用尚无可显示内容。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** task 展开详情：子代理 / 任务 / 结论分区；结论走 Markdown，代替 JSON 参数卡。 */
function TaskDispatchSection({
  view,
  output,
  childExecutionId,
  onOpenChildTimeline,
}: {
  view: TaskDispatchView
  output: string
  childExecutionId?: string
  onOpenChildTimeline?: (executionId: string) => void
}): ReactElement {
  const result = output.trim()
  return (
    <section className="tool-detail-card" data-section="dispatch">
      <header className="tool-detail-header">
        <span className="tool-detail-title">派出</span>
      </header>
      <div className="tool-detail-body" data-clamped="false">
        {view.agentId ? (
          <div className="tool-dispatch-field">
            <span className="tool-dispatch-key">子代理</span>
            <span className="tool-dispatch-value">{view.agentId}</span>
          </div>
        ) : null}
        {view.description ? (
          <div className="tool-dispatch-field">
            <span className="tool-dispatch-key">{TASK_DISPATCH_TASK_LABEL}</span>
            <span className="tool-dispatch-value">{view.description}</span>
          </div>
        ) : null}
        {childExecutionId && onOpenChildTimeline ? (
          <div className="tool-dispatch-field tool-dispatch-child-action">
            <button
              type="button"
              className="child-timeline-open-btn btn-link"
              onClick={() => onOpenChildTimeline(childExecutionId)}
            >
              进入子时间线
            </button>
          </div>
        ) : null}
        {result ? (
          <div className="tool-dispatch-field tool-dispatch-result">
            <span className="tool-dispatch-key">{TASK_DISPATCH_RESULT_LABEL}</span>
            <div className="tool-dispatch-value tool-dispatch-markdown">
              <Markdown text={result} />
            </div>
          </div>
        ) : null}
      </div>
    </section>
  )
}

/** 输出卡片：按渲染模型分派（结构化/JSON 美化/纯文本）；超阈值钳制 + 就地展开；复制成功短暂反馈对勾。 */
function ToolOutputSection({ toolName, output, argumentsText }: { toolName: string; output: string; argumentsText: string }): ReactElement {
  const view = toolOutputView(toolName, output, argumentsText)
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const copyOutput = (): void => {
    const write = navigator.clipboard?.writeText(view.text) ?? Promise.resolve()
    void write.then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    }).catch(() => {})
  }
  return (
    <section className="tool-detail-card" data-section="output">
      <header className="tool-detail-header">
        <span className="tool-detail-title">输出</span>
        <span className="tool-detail-meta">{view.metaLabel}</span>
        <button
          type="button"
          className={copied ? "tool-detail-copy copied" : "tool-detail-copy"}
          aria-label="复制输出"
          title="复制输出"
          onClick={copyOutput}
        >
          {copied
            ? <Check aria-hidden="true" focusable="false" className="tool-detail-copy-icon" />
            : <Copy aria-hidden="true" focusable="false" className="tool-detail-copy-icon" />}
        </button>
      </header>
      <div className="tool-detail-body" data-clamped={view.collapsible && !expanded}>
        <ToolOutputBody view={view} />
      </div>
      {view.collapsible ? (
        <button
          type="button"
          className="tool-detail-expand"
          onClick={() => setExpanded(previous => !previous)}
        >
          {expanded ? "收起" : "展开全部"}
        </button>
      ) : null}
    </section>
  )
}

/** 输出内容体：结构化种类各走专属布局，text/json 走通用 pre。 */
function ToolOutputBody({ view }: { view: ToolOutputView }): ReactElement {
  if (view.kind === "file-content" && view.fileContent) {
    const file = view.fileContent
    return (
      <>
        <div className="tool-file-meta">
          {file.path} · 第 {file.shownStart}–{file.shownEnd} 行 / 共 {file.totalLines} 行{file.truncated ? " · 已截断" : ""}
        </div>
        <div className="tool-file-lines">
          {file.lines.map((line, index) => (
            <div className="tool-file-line" key={index}>
              <span className="tool-file-lineno">{line.number ?? ""}</span>
              <span className="tool-file-text">{line.text}</span>
            </div>
          ))}
        </div>
      </>
    )
  }
  if (view.kind === "path-list" && view.pathList) {
    return (
      <div className="tool-path-list">
        {view.pathList.entries.map((entry, index) => (
          <div className="tool-path-row" key={index}>
            <ToolPathIcon entry={entry} />
            <span className="tool-path-text">{entry}</span>
          </div>
        ))}
      </div>
    )
  }
  if (view.kind === "diff" && view.diff) {
    const diff = view.diff
    return (
      <>
        {diff.path ? <div className="tool-diff-meta">{diff.path}</div> : null}
        <div className="tool-diff-rows">
          {diff.rows.map((row, index) => (
            <div className="tool-diff-row" data-type={row.type} key={index}>
              <span className="tool-diff-sign">{row.type === "add" ? "+" : row.type === "remove" ? "−" : " "}</span>
              <span className="tool-diff-text">{row.text}</span>
            </div>
          ))}
        </div>
      </>
    )
  }
  if (view.kind === "terminal" && view.terminal) {
    const terminal = view.terminal
    return (
      <div className="tool-terminal">
        {terminal.command !== null ? <div className="tool-terminal-cmd">$ {terminal.command}</div> : null}
        {terminal.lines.map((line, index) => (
          <div className="tool-terminal-line" key={index}>{line}</div>
        ))}
        {terminal.truncated ? <div className="tool-terminal-truncated">输出因大小限制被截断</div> : null}
      </div>
    )
  }
  if (view.kind === "grep-matches" && view.grepMatches) {
    return (
      <div className="tool-grep-matches">
        {view.grepMatches.groups.map((group, index) => (
          <div className="tool-grep-group" key={index}>
            <div className="tool-grep-path">
              <ToolPathIcon entry={group.path} />
              {group.path}
            </div>
            {group.matches.map((match, matchIndex) => (
              <div className="tool-grep-line" key={matchIndex}>
                <span className="tool-grep-lineno">{match.line ?? ""}</span>
                <span className="tool-grep-text">
                  {match.count !== undefined ? `${match.count} 处匹配` : match.text}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    )
  }
  return <pre className="tool-details-pre">{view.text}</pre>
}

/** 路径行图标：目录（尾斜杠）用 Folder，文件复用侧栏类型图标着色体系。 */
function ToolPathIcon({ entry }: { entry: string }): ReactElement {
  if (entry.endsWith("/")) {
    return <Folder aria-hidden="true" focusable="false" size={13} className="file-row-icon" />
  }
  const name = entry.split("/").filter(Boolean).pop() ?? entry
  return <FileTypeIcon name={name} size={13} />
}

/** 参数卡片：与输出同级的 details 折叠卡，内容美化 JSON（非法 JSON 回退原文）。 */
function ToolArgumentsSection({ arguments: argumentsText }: { arguments: string }): ReactElement {
  return (
    <details className="tool-detail-card tool-detail-arguments" data-section="arguments">
      <summary className="tool-detail-header tool-detail-arguments-summary">
        <span className="tool-detail-title">参数</span>
        <ChevronDown aria-hidden="true" focusable="false" className="tool-detail-chevron" />
      </summary>
      <div className="tool-detail-body" data-clamped="false">
        <pre className="tool-details-pre">{prettifyJson(argumentsText) ?? argumentsText}</pre>
      </div>
    </details>
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
 * Tool 状态：纯图标语义（旋转/对勾/告警），中文状态文案挂在 aria-label 与 tooltip 上；
 * 状态区分不依赖颜色——形状与动画本身即可分辨，成功不再占用文字位。
 */
function renderToolStatus(status: ToolCard["status"]): ReactNode {
  const label = toolStatusLabel(status)
  if (status === "running") {
    return (
      <span className="tool-row-status tool-status-running" role="img" aria-label={label} title={label}>
        <Loader2 aria-hidden="true" focusable="false" className="tool-status-icon spinning" />
      </span>
    )
  }
  if (status === "failed") {
    // 失败带文字徽章：扫读时间线时不依赖整行底色与图标形状也能立刻定位（2026-08-18 与用户确认）。
    return (
      <span className="tool-row-status tool-status-failed" aria-label={label} title={label}>
        <AlertTriangle aria-hidden="true" focusable="false" className="tool-status-icon" />
        <span className="tool-status-text">{label}</span>
      </span>
    )
  }
  return (
    <span className="tool-row-status tool-status-completed" role="img" aria-label={label} title={label}>
      <Check aria-hidden="true" focusable="false" className="tool-status-icon" />
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


/** Compose 固定步骤条：等宽 chip + 轨道，状态只出现在右侧 hint。 */
function ComposeProgress({ state }: { state: ComposeProjection }) {
  const segments = composeStepperSegments(state)
  const hint = composeStepperHint(state)
  const hintKind = hint === "失败" ? "failed" : hint === "等你确认" ? "wait" : "live"
  return (
    <div className="compose-progress" role="status" aria-label="Compose 工作流进度">
      <div className="compose-stages">
        {segments.map((segment, index) => {
          const previous = segments[index - 1]
          const filled = previous ? composeStepperTrackFilled(previous.mark) : false
          return (
            <span key={segment.id} className="compose-stage-wrap">
              {index > 0 ? (
                <span
                  className={`compose-track${filled ? " compose-track-filled" : ""}`}
                  aria-hidden="true"
                />
              ) : null}
              <span
                className={`compose-chip compose-chip-${segment.mark}`}
                aria-current={segment.mark === "current" ? "step" : undefined}
              >
                {segment.label}
              </span>
            </span>
          )
        })}
      </div>
      {hint ? <span className={`compose-hint compose-hint-${hintKind}`}>{hint}</span> : null}
    </div>
  )
}
