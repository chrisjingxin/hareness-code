import type { KeyEvent, ScrollBoxRenderable, TextareaRenderable } from "@opentui/core"
import { useEffect, useState, type RefObject } from "react"

import { findSlashCommands, type SlashCommandDefinition } from "./commands"
import {
  formatDuration,
  formatUsage,
  runtimeStatusLabel,
  supportsHomeDecoration,
  type TuiRuntime,
  workspaceLabel,
} from "./model"
import type { ConversationMessage, PendingApproval, PendingQuestion, TimelineItem, ToolCard, TuiState } from "./state"
import { HarnessCodeLogo } from "./harness-logo"
import { StarryBackground } from "./starry-background"
import { markdownSyntax, tuiTheme } from "./theme"
import { collapseToolOutput } from "./upstream/collapse-tool-output"

export type CommandMenuState = {
  visible: boolean
  selectedIndex: number
}

type SharedViewProps = {
  runtime: TuiRuntime
  state: TuiState
  terminalWidth: number
  terminalHeight: number
  inputRef: RefObject<TextareaRenderable | null>
  conversationScrollRef: RefObject<ScrollBoxRenderable | null>
  value: string
  onInput: (value: string) => void
  onComposerKeyDown: (event: KeyEvent) => void
  onSubmit: () => void
  commandMenu: CommandMenuState
  onSelectCommand: (command: SlashCommandDefinition) => void
  onHoverCommand: (index: number) => void
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: "approve" | "reject") => void
  onQuestion: (answer: string) => void
}

/** 未开始对话时使用独立的沉浸式首页，避免用空消息列表伪装会话。 */
export function HomeView(props: SharedViewProps) {
  const decorate = supportsHomeDecoration(props.terminalWidth, props.terminalHeight) && process.env.TERM !== "dumb"
  const compact = !decorate || props.terminalWidth < 76
  const showSupplemental = !props.commandMenu.visible || compact
  const commandRows = props.commandMenu.visible && !compact
    ? Math.min(5, Math.max(1, findSlashCommands(props.value).length)) + 2
    : 0

  return (
    <box flexDirection="column" flexGrow={1} backgroundColor={tuiTheme.background}>
      {decorate ? <StarryBackground width={props.terminalWidth} height={props.terminalHeight} /> : null}
      <box flexDirection="column" flexGrow={1} alignItems="center" paddingLeft={2} paddingRight={2} zIndex={1}>
        <box flexGrow={1} minHeight={0} />
        <HarnessCodeLogo compact={compact} />
        {/* 菜单绝对定位在 composer 上方；此处保留同等高度以避免覆盖字标。 */}
        <box height={compact ? 1 : Math.max(2, commandRows + 1)} minHeight={0} flexShrink={1} />
        <box width="100%" maxWidth={75} flexShrink={0}>
          <Composer {...props} variant="home" commandMenuPlacement={compact ? "inline-below" : "above"} />
        </box>
        {showSupplemental ? <HomeSupplemental terminalWidth={props.terminalWidth} /> : null}
        <box flexGrow={1} minHeight={0} />
      </box>
      <FooterRail runtime={props.runtime} state={props.state} terminalWidth={props.terminalWidth} />
    </box>
  )
}

/** 会话流全宽渲染，工具和审批事件以左轨形成明确的操作时间线。 */
export function SessionView(props: SharedViewProps) {
  const blockingInteraction = Boolean(props.state.pendingApproval || props.state.pendingQuestion?.options.length)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} backgroundColor={tuiTheme.background}>
      <ConversationTimeline
        state={props.state}
        scrollRef={props.conversationScrollRef}
        showToolDetails={props.showToolDetails}
        expandedTools={props.expandedTools}
        onToggleTool={props.onToggleTool}
      />
      <InteractionDock
        approval={props.state.pendingApproval}
        question={props.state.pendingQuestion?.options.length ? props.state.pendingQuestion : undefined}
        onApproval={props.onApproval}
        onQuestion={props.onQuestion}
      />
      {!blockingInteraction ? (
        <box flexShrink={0} paddingLeft={2} paddingRight={2}>
          <SessionRuntimeLine runtime={props.runtime} state={props.state} />
          <Composer {...props} variant="session" commandMenuPlacement="above" />
        </box>
      ) : null}
      <FooterRail runtime={props.runtime} state={props.state} terminalWidth={props.terminalWidth} session />
    </box>
  )
}

function HomeSupplemental(props: { terminalWidth: number }) {
  return (
    <>
      <box paddingTop={1} flexDirection="row" gap={2} flexShrink={0}>
        <text fg={tuiTheme.muted}><span fg={tuiTheme.text}>Enter</span> 发送</text>
        <text fg={tuiTheme.muted}><span fg={tuiTheme.text}>/</span> 命令</text>
        {props.terminalWidth >= 72 ? <text fg={tuiTheme.muted}><span fg={tuiTheme.text}>Ctrl+C</span> 清空/退出</text> : null}
      </box>
      <box paddingTop={2} flexShrink={0}>
        <text fg={tuiTheme.muted}>
          <span fg={tuiTheme.primary}>提示</span>　输入 <span fg={tuiTheme.text}>/help</span> 查看当前可用命令
        </text>
      </box>
    </>
  )
}

export function ConversationTimeline(props: {
  state: TuiState
  scrollRef: RefObject<ScrollBoxRenderable | null>
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
}) {
  return (
    <scrollbox ref={props.scrollRef} stickyScroll stickyStart="bottom" flexGrow={1} minHeight={0} viewportOptions={{ paddingRight: 1 }}>
      <box height={1} />
      {props.state.timeline.map(item => (
        <TimelineRow
          key={item.type === "message" ? item.message.id : item.tool.id}
          item={item}
          state={props.state}
          showToolDetails={props.showToolDetails}
          expandedTools={props.expandedTools}
          onToggleTool={props.onToggleTool}
        />
      ))}
      <RunSummary state={props.state} />
      <box height={1} />
    </scrollbox>
  )
}

/**
 * 消息与工具共用同一时间线，必须在这里逐项渲染，不能再次按类型拆成两个列表；
 * 否则工具卡片会被错误地堆到所有回答文本之后。
 */
function TimelineRow(props: {
  item: TimelineItem
  state: TuiState
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
}) {
  if (props.item.type === "message") return <MessageBlock message={props.item.message} state={props.state} />
  const tool = props.item.tool
  return (
    <ToolRow
      tool={tool}
      expanded={props.showToolDetails || props.expandedTools.has(tool.id) || tool.status !== "completed"}
      onToggle={() => props.onToggleTool(tool.id)}
    />
  )
}

function MessageBlock(props: { message: ConversationMessage; state: TuiState }) {
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
    if (props.message.streaming && !props.message.content) return <ThinkingIndicator state={props.state} />
    // 工具先于文本返回时会留下一个空占位；结束后不应渲染无意义的省略号。
    if (!props.message.content) return null
    return (
      <box flexDirection="column" marginTop={1} paddingLeft={3} paddingRight={3}>
        <markdown
          content={props.message.content || "…"}
          syntaxStyle={markdownSyntax}
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

function ToolRow(props: { tool: ToolCard; expanded: boolean; onToggle: () => void }) {
  const tone = props.tool.status === "failed" ? tuiTheme.danger : props.tool.status === "completed" ? tuiTheme.success : tuiTheme.primary
  const marker = props.tool.status === "failed" ? "×" : props.tool.status === "completed" ? "✓" : "◌"
  const label = props.tool.status === "failed" ? "失败" : props.tool.status === "completed" ? "完成" : "执行中"
  const collapsed = collapseToolOutput(props.tool.detail, 4, 360)
  const detail = props.expanded ? props.tool.detail : collapsed.output

  return (
    <box marginTop={1} marginLeft={3} marginRight={3} border={["left"]} borderColor={tone} customBorderChars={PROMPT_BORDER}>
      <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} onMouseUp={props.onToggle}>
        <box flexDirection="row" justifyContent="space-between" gap={2}>
          <box flexDirection="row" gap={1} flexShrink={1}>
            <text fg={tone}>{marker}</text>
            <text fg={tuiTheme.text}>{props.tool.name}</text>
            <text fg={tuiTheme.muted}>{label}</text>
          </box>
          {collapsed.overflow ? <text fg={tuiTheme.subtle}>{props.expanded ? "收起" : "展开"}</text> : null}
        </box>
        {detail ? <text content={detail} fg={tuiTheme.muted} /> : null}
      </box>
    </box>
  )
}

function ThinkingIndicator(props: { state: TuiState }) {
  const frame = useSpinner(Boolean(props.state.activeRun), 80)
  const label = props.state.status === "正在调用工具" ? "正在调用工具" : props.state.status === "正在继续执行" ? "继续执行" : "Thinking"
  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={tuiTheme.warning}>{frame}</text>
      <text fg={tuiTheme.warning}>{label}</text>
    </box>
  )
}

function RunSummary(props: { state: TuiState }) {
  const summary = props.state.lastRun
  if (!summary) return null
  const duration = formatDuration(summary.durationMs)
  const usage = formatUsage(summary.usage)
  const outcome = summary.outcome === "completed" ? "已完成" : summary.outcome === "cancelled" ? "已取消" : "失败"
  const color = summary.outcome === "completed" ? tuiTheme.success : summary.outcome === "cancelled" ? tuiTheme.muted : tuiTheme.danger
  const parts = [outcome, duration, usage].filter((part): part is string => Boolean(part))

  return (
    <box marginTop={1} paddingLeft={3} flexDirection="row" gap={1}>
      <text fg={color}>●</text>
      <text fg={tuiTheme.muted}>{parts.join(" · ")}</text>
    </box>
  )
}

export function InteractionDock(props: {
  approval?: PendingApproval
  question?: PendingQuestion
  onApproval: (decision: "approve" | "reject") => void
  onQuestion: (answer: string) => void
}) {
  if (props.approval) {
    return (
      <box marginLeft={2} marginRight={2} marginBottom={1} border={["left"]} borderColor={tuiTheme.warning} customBorderChars={PROMPT_BORDER}>
        <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
          <box flexDirection="row" gap={1}>
            <text fg={tuiTheme.warning}>△</text>
            <text fg={tuiTheme.text}><strong>需要审批</strong></text>
          </box>
          <text content={props.approval.description} fg={tuiTheme.text} />
          <ApprovalRequestPreview requests={props.approval.requests} />
          <select
            focused
            options={[
              { name: "允许一次", description: "继续执行当前操作", value: "approve" },
              { name: "拒绝", description: "停止此操作并告知 Agent", value: "reject" },
            ]}
            onSelect={(_, option) => {
              if (option?.value === "approve" || option?.value === "reject") props.onApproval(option.value)
            }}
          />
          <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
        </box>
      </box>
    )
  }

  if (props.question?.options.length) {
    return (
      <box marginLeft={2} marginRight={2} marginBottom={1} border={["left"]} borderColor={tuiTheme.primary} customBorderChars={PROMPT_BORDER}>
        <box backgroundColor={tuiTheme.toolSurface} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
          <box flexDirection="row" gap={1}>
            <text fg={tuiTheme.primary}>?</text>
            <text fg={tuiTheme.text}><strong>Agent 需要你的回答</strong></text>
          </box>
          <text content={props.question.question} fg={tuiTheme.text} />
          <select
            focused
            options={props.question.options.map(option => ({ ...option, description: option.name }))}
            onSelect={(_, option) => { if (typeof option?.value === "string") props.onQuestion(option.value) }}
          />
          <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
        </box>
      </box>
    )
  }

  return null
}

function SessionRuntimeLine(props: { runtime: TuiRuntime; state: TuiState }) {
  return (
    <box flexDirection="row" gap={1} paddingBottom={1}>
      <text fg={statusColor(props.state.status)}>□</text>
      <text fg={tuiTheme.primary}>Harness Code</text>
      <text fg={tuiTheme.muted}>·</text>
      <text fg={props.runtime.modelConfigured ? tuiTheme.text : tuiTheme.warning}>{modelLabel(props.runtime)}</text>
      <text fg={tuiTheme.muted}>· {props.state.status}</text>
    </box>
  )
}

export function Composer(props: Pick<SharedViewProps, "runtime" | "state" | "inputRef" | "value" | "onInput" | "onComposerKeyDown" | "onSubmit" | "commandMenu" | "onSelectCommand" | "onHoverCommand"> & {
  variant: "home" | "session"
  commandMenuPlacement: "above" | "inline-below"
}) {
  const active = Boolean(props.state.activeRun)
  const awaitingQuestion = Boolean(props.state.pendingQuestion)
  const options = findSlashCommands(props.value)
  const placeholder = awaitingQuestion
    ? "输入你的回答后按 Enter"
    : active
      ? "正在执行；Esc 中断"
      : "输入消息…（输入 / 唤起命令）"

  const commandMenu = props.commandMenu.visible ? (
    <CommandMenu
      options={options}
      selectedIndex={Math.min(props.commandMenu.selectedIndex, Math.max(0, options.length - 1))}
      onSelect={props.onSelectCommand}
      onHover={props.onHoverCommand}
      placement={props.commandMenuPlacement}
    />
  ) : null

  return (
    <box position="relative" flexDirection="column" flexShrink={0}>
      {commandMenu && props.commandMenuPlacement === "above" ? (
        <box position="absolute" left={0} bottom="100%" width="100%" zIndex={10}>
          {commandMenu}
        </box>
      ) : null}
      <box border={["left"]} borderColor={active ? tuiTheme.primarySoft : tuiTheme.primary} customBorderChars={PROMPT_BORDER}>
        <box backgroundColor={tuiTheme.composer} paddingLeft={2} paddingRight={2} paddingTop={1} flexShrink={0} flexGrow={1}>
          <textarea
            ref={props.inputRef}
            placeholder={placeholder}
            placeholderColor={tuiTheme.muted}
            textColor={tuiTheme.text}
            focusedTextColor={tuiTheme.text}
            backgroundColor={tuiTheme.composer}
            focusedBackgroundColor={tuiTheme.composer}
            cursorColor={tuiTheme.primary}
            minHeight={1}
            maxHeight={6}
            keyBindings={COMPOSER_KEY_BINDINGS}
            focused={!active || awaitingQuestion}
            onContentChange={() => props.onInput(props.inputRef.current?.plainText ?? "")}
            onKeyDown={props.onComposerKeyDown}
            onSubmit={props.onSubmit}
          />
          <RuntimeMeta runtime={props.runtime} variant={props.variant} />
        </box>
      </box>
      {commandMenu && props.commandMenuPlacement === "inline-below" ? commandMenu : null}
    </box>
  )
}

function CommandMenu(props: {
  options: readonly SlashCommandDefinition[]
  selectedIndex: number
  onSelect: (command: SlashCommandDefinition) => void
  onHover: (index: number) => void
  placement: "above" | "inline-below"
}) {
  return (
    <box
      marginTop={props.placement === "inline-below" ? 1 : 0}
      marginBottom={props.placement === "above" ? 1 : 0}
      border={["left"]}
      borderColor={tuiTheme.borderActive}
      customBorderChars={PROMPT_BORDER}
    >
      <box backgroundColor={tuiTheme.menu} paddingTop={1} paddingBottom={1}>
        {props.options.length ? props.options.map((command, index) => {
          const selected = index === props.selectedIndex
          return (
            <box
              key={command.name}
              backgroundColor={selected ? tuiTheme.primarySoft : tuiTheme.menu}
              paddingLeft={2}
              paddingRight={2}
              flexDirection="row"
              justifyContent="space-between"
              onMouseOver={() => props.onHover(index)}
              onMouseUp={() => props.onSelect(command)}
            >
              <text fg={selected ? tuiTheme.text : tuiTheme.primary}>/{command.name}</text>
              <text fg={selected ? tuiTheme.text : tuiTheme.muted}>{command.description}</text>
            </box>
          )
        }) : (
          <box paddingLeft={2} paddingRight={2}>
            <text fg={tuiTheme.muted}>没有匹配的命令</text>
          </box>
        )}
      </box>
    </box>
  )
}

function RuntimeMeta(props: { runtime: TuiRuntime; variant: "home" | "session" }) {
  const showConnectionStatus = props.runtime.modelConfigured || Boolean(props.runtime.startupError)
  return (
    <box flexDirection="row" gap={1} paddingTop={1} paddingBottom={1}>
      <text fg={tuiTheme.primary}>Harness Code</text>
      <text fg={tuiTheme.muted}>·</text>
      <text fg={props.runtime.modelConfigured ? tuiTheme.text : tuiTheme.warning}>{modelLabel(props.runtime)}</text>
      {props.variant === "home" && showConnectionStatus ? <text fg={tuiTheme.muted}>· {runtimeStatusLabel(props.runtime)}</text> : null}
    </box>
  )
}

function FooterRail(props: { runtime: TuiRuntime; state: TuiState; terminalWidth: number; session?: boolean }) {
  const showFullPath = props.terminalWidth >= 108
  const showBranch = props.terminalWidth >= 84 && props.runtime.gitBranch
  const workspace = showFullPath ? props.runtime.workspace : workspaceLabel(props.runtime.workspace)

  return (
    <box flexDirection="row" justifyContent="space-between" gap={1} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexShrink={0}>
      <box flexDirection="row" gap={1} flexShrink={1}>
        <text fg={tuiTheme.muted}>{workspace}</text>
        {showBranch ? <text fg={tuiTheme.subtle}>:{props.runtime.gitBranch}</text> : null}
      </box>
      {props.state.activeRun ? <BusyRunHint /> : props.session ? <text fg={tuiTheme.muted}>↑↓ 历史提示词 · PgUp/PgDn 浏览 · Ctrl+O 工具详情</text> : null}
      <text fg={tuiTheme.subtle}>v{props.runtime.cliVersion}</text>
    </box>
  )
}

function BusyRunHint() {
  const frame = useSpinner(true, 80)
  return (
    <box flexDirection="row" gap={1}>
      <text fg={tuiTheme.primary}>{frame}</text>
      <text fg={tuiTheme.muted}>Esc 中断</text>
    </box>
  )
}

function ApprovalRequestPreview(props: { requests: unknown }) {
  const preview = approvalPreview(props.requests)
  return preview ? <text content={preview} fg={tuiTheme.muted} /> : null
}

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

function useSpinner(active: boolean, interval: number): string {
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    if (!active) {
      setFrame(0)
      return
    }
    const timer = setInterval(() => setFrame(current => current + 1), interval)
    return () => clearInterval(timer)
  }, [active, interval])
  return SPINNER_FRAMES[frame % SPINNER_FRAMES.length] ?? "·"
}

function modelLabel(runtime: TuiRuntime): string {
  return runtime.modelConfigured ? (runtime.modelName ?? "已配置模型") : "模型未配置"
}

function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

function safePreview(value: unknown): string | undefined {
  if (value === undefined) return undefined
  try {
    return shorten(JSON.stringify(value), 120)
  } catch {
    return "参数不可序列化"
  }
}

function statusColor(status: string): string {
  if (status === "已完成") return tuiTheme.success
  if (status === "已取消") return tuiTheme.muted
  if (status === "执行失败") return tuiTheme.danger
  if (status.includes("审批")) return tuiTheme.warning
  return tuiTheme.primary
}

const PROMPT_BORDER = {
  topLeft: " ",
  topRight: " ",
  bottomLeft: "╹",
  bottomRight: " ",
  horizontal: " ",
  vertical: "│",
  topT: " ",
  bottomT: " ",
  leftT: "│",
  rightT: " ",
  cross: "│",
} as const

const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", "✈"]

/**
 * Textarea 默认把 Enter 绑定为换行，和 Coding Agent 的终端习惯不符。
 * 覆盖同一按键后 Enter 用于发送，仍为需要多行提示词的用户保留 Shift+Enter。
 */
const COMPOSER_KEY_BINDINGS: Array<{ name: string; shift?: boolean; action: "submit" | "newline" }> = [
  { name: "return", action: "submit" },
  { name: "kpenter", action: "submit" },
  { name: "linefeed", action: "submit" },
  { name: "return", shift: true, action: "newline" },
  { name: "kpenter", shift: true, action: "newline" },
]
