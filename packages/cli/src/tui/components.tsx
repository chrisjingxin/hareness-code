/** Harness Code 的 OpenTUI 视图组件：首页、thread 时间线、交互面板与 composer。 */

import { type KeyEvent, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { useEffect, useState, type ReactNode, type RefObject } from "react"

import {
  commandMenuItemDescription,
  commandMenuItemLabel,
  type CommandMenuItem,
  type SkillMenuItem,
} from "./commands"
import {
  approvalModeLabel,
  formatDuration,
  formatUsage,
  supportsHomeDecoration,
  type TuiRuntime,
  workspaceLabel,
} from "./model"
import type { ConversationMessage, InteractionCard, TimelineItem, ToolCard, TuiState } from "./state"
import { HarnessCodeLogo } from "./harness-logo"
import { StarryBackground } from "./starry-background"
import { markdownSyntax, tuiTheme } from "./theme"
import { SearchPicker, type SearchPickerRenderContext } from "./overlays"
import { collapseToolOutput } from "./upstream/collapse-tool-output"

export type CommandMenuState = {
  visible: boolean
  selectedIndex: number
}

/** 用户从选择器或 Slash 菜单选中的一次性 Skill 上下文。 */
export type SelectedSkill = SkillMenuItem

/** 恢复选择器使用的 thread 摘要；内部 thread_id 绝不直接渲染。 */
export type ThreadPickerItem = {
  threadId: string
  createdAtMs: number
  updatedAtMs: number
  firstMessage: string
  latestMessage: string
  messageCount: number
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
  commandOptions: readonly CommandMenuItem[]
  onSelectCommand: (command: CommandMenuItem) => void
  onHoverCommand: (index: number) => void
  selectedSkill?: SelectedSkill
  pickerVisible: boolean
  onClearSelectedSkill: () => void
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: "approve" | "reject") => void
  onQuestion: (answer: string) => void
}

/** 未开始对话时使用独立的沉浸式首页，避免用空消息列表伪装 thread。 */
export function HomeView(props: SharedViewProps) {
  const decorate = supportsHomeDecoration(props.terminalWidth, props.terminalHeight) && process.env.TERM !== "dumb"
  const compact = !decorate || props.terminalWidth < 76
  const showSupplemental = !props.commandMenu.visible || compact
  const commandRows = props.commandMenu.visible && !compact
    ? Math.min(5, Math.max(1, props.commandOptions.length)) + 2
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

/** thread 流全宽渲染，工具和审批事件以左轨形成明确的操作时间线。 */
export function ThreadView(props: SharedViewProps) {
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

/** 首页快捷键提示，在命令菜单展开时由上层隐藏。 */
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

/** 使用 ScrollBox 渲染统一 timeline，并保留 sticky-scroll 行为。 */
export function ConversationTimeline(props: {
  state: TuiState
  scrollRef: RefObject<ScrollBoxRenderable | null>
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: "approve" | "reject") => void
  onQuestion: (answer: string) => void
}) {
  return (
    <scrollbox ref={props.scrollRef} stickyScroll stickyStart="bottom" flexGrow={1} minHeight={0} viewportOptions={{ paddingRight: 1 }}>
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
      <RunSummary state={props.state} />
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
  onApproval: (decision: "approve" | "reject") => void
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
function RunSummary(props: { state: TuiState }) {
  const summary = props.state.lastRun
  if (!summary) return null
  const duration = formatDuration(summary.durationMs)
  const usage = formatUsage(summary.usage)
  const context = summary.context?.estimatedTokens && summary.context.inputCapTokens
    ? `ctx ${summary.context.estimatedTokens}/${summary.context.inputCapTokens}`
    : undefined
  const outcome = summary.outcome === "completed" ? "已完成" : summary.outcome === "cancelled" ? "已取消" : "失败"
  const color = summary.outcome === "completed" ? tuiTheme.success : summary.outcome === "cancelled" ? tuiTheme.muted : tuiTheme.danger
  const parts = [outcome, duration, usage, context].filter((part): part is string => Boolean(part))

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
  onApproval: (decision: "approve" | "reject") => void
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
              height={4}
              showDescription
              wrapSelection
              options={[
                { name: "允许一次", description: "继续执行当前操作", value: "approve" },
                { name: "拒绝", description: "停止此操作并告知 Agent", value: "reject" },
              ]}
              onSelect={(_, option) => {
                if (option?.value === "approve" || option?.value === "reject") props.onApproval(option.value)
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

/** thread composer 上方的实时模型和运行状态行。 */
function ThreadRuntimeLine(props: { runtime: TuiRuntime; state: TuiState }) {
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

/** 渲染统一左轨 composer、命令菜单和运行时元信息。 */
export function Composer(props: Pick<SharedViewProps, "runtime" | "state" | "terminalWidth" | "inputRef" | "value" | "onInput" | "onComposerKeyDown" | "onSubmit" | "commandMenu" | "commandOptions" | "onSelectCommand" | "onHoverCommand" | "selectedSkill" | "pickerVisible" | "onClearSelectedSkill"> & {
  variant: "home" | "thread"
  commandMenuPlacement: "above" | "inline-below"
}) {
  const active = Boolean(props.state.activeRun)
  const awaitingQuestion = Boolean(props.state.pendingQuestion)
  const options = props.commandOptions
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
            focused={(!active || awaitingQuestion) && !props.pickerVisible}
            onContentChange={() => props.onInput(props.inputRef.current?.plainText ?? "")}
            onKeyDown={props.onComposerKeyDown}
            onSubmit={props.onSubmit}
          />
          {props.selectedSkill ? (
            <box paddingTop={1} flexDirection="row" gap={1}>
              <text fg={tuiTheme.primary}>Skill</text>
              <text fg={tuiTheme.text}>{props.selectedSkill.id}</text>
              <text fg={tuiTheme.muted}>{props.selectedSkill.argumentHint ?? "下一条消息使用"}</text>
              <text fg={tuiTheme.muted} onMouseUp={props.onClearSelectedSkill}>×</text>
            </box>
          ) : null}
          <RuntimeMeta runtime={props.runtime} variant={props.variant} terminalWidth={props.terminalWidth} />
        </box>
      </box>
      {commandMenu && props.commandMenuPlacement === "inline-below" ? commandMenu : null}
    </box>
  )
}

/** 渲染可筛选的 Slash 命令候选列表，并共享键盘与鼠标选择回调。 */
function CommandMenu(props: {
  options: readonly CommandMenuItem[]
  selectedIndex: number
  onSelect: (command: CommandMenuItem) => void
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
        {props.options.length ? props.options.map((item, index) => {
          const selected = index === props.selectedIndex
          const disabled = item.kind === "command" && item.availability.state === "disabled"
          return (
            <box
              key={item.kind === "command" ? item.command.name : item.skill.id}
              backgroundColor={selected && !disabled ? tuiTheme.primarySoft : tuiTheme.menu}
              paddingLeft={2}
              paddingRight={2}
              flexDirection="row"
              justifyContent="space-between"
              onMouseOver={() => props.onHover(index)}
              onMouseUp={() => props.onSelect(item)}
            >
              <text fg={disabled ? tuiTheme.muted : selected ? tuiTheme.text : tuiTheme.primary}>{commandMenuItemLabel(item)}</text>
              <text fg={disabled ? tuiTheme.subtle : selected ? tuiTheme.text : tuiTheme.muted}>{shorten(commandMenuItemDescription(item), 54)}</text>
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

/** Skill 领域仅提供行内容；搜索、遮罩、焦点、滚动和状态统一交给 SearchPicker。 */
export function SkillPicker(props: {
  visible: boolean
  loading: boolean
  error?: string
  skills: readonly SkillMenuItem[]
  query: string
  selectedIndex: number
  terminalWidth: number
  terminalHeight: number
  searchRef: RefObject<TextareaRenderable | null>
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  onSearch: (query: string) => void
  onSelect: (skill: SkillMenuItem) => void
  onHover: (index: number) => void
  onClose: () => void
}) {
  return (
    <SearchPicker
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.skills}
      query={props.query}
      selectedIndex={props.selectedIndex}
      terminalWidth={props.terminalWidth}
      terminalHeight={props.terminalHeight}
      searchRef={props.searchRef}
      restoreFocusRef={props.restoreFocusRef}
      shouldRestoreFocus={props.shouldRestoreFocus}
      searchId="skill-search"
      title="Skills"
      searchPlaceholder="搜索 Skills..."
      emptyMessage="没有匹配的 Skill"
      loadingMessage="正在读取 Skill catalog…"
      itemKey={skill => skill.id}
      renderItem={(skill, context) => skillPickerRow(skill, context)}
      onSearch={props.onSearch}
      onSelect={props.onSelect}
      onHover={props.onHover}
      onClose={props.onClose}
    />
  )
}

/** Thread 领域保留用户可识别的摘要行；内部 ID 始终只作为稳定 React key。 */
export function ThreadPicker(props: {
  visible: boolean
  loading: boolean
  error?: string
  threads: readonly ThreadPickerItem[]
  query: string
  selectedIndex: number
  terminalWidth: number
  terminalHeight: number
  searchRef: RefObject<TextareaRenderable | null>
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  onSearch: (query: string) => void
  onSelect: (thread: ThreadPickerItem) => void
  onHover: (index: number) => void
  onClose: () => void
}) {
  return (
    <SearchPicker
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.threads}
      query={props.query}
      selectedIndex={props.selectedIndex}
      terminalWidth={props.terminalWidth}
      terminalHeight={props.terminalHeight}
      searchRef={props.searchRef}
      restoreFocusRef={props.restoreFocusRef}
      shouldRestoreFocus={props.shouldRestoreFocus}
      searchId="thread-search"
      title="Threads"
      searchPlaceholder="搜索 Threads..."
      emptyMessage="没有可恢复的 thread"
      loadingMessage="正在读取 Threads…"
      itemKey={thread => thread.threadId}
      renderItem={(thread, context) => threadPickerRow(thread, context)}
      onSearch={props.onSearch}
      onSelect={props.onSelect}
      onHover={props.onHover}
      onClose={props.onClose}
    />
  )
}

/** 将 Skill 行的窄终端降级限定在领域内容，不泄漏到通用 Picker 布局。 */
function skillPickerRow(skill: SkillMenuItem, context: SearchPickerRenderContext): ReactNode {
  const idWidth = context.compact
    ? Math.max(18, context.width - 6)
    : Math.max(24, Math.min(34, Math.floor(context.width * 0.34)))
  return (
    <>
      <text width={idWidth} fg={context.selected ? tuiTheme.background : tuiTheme.primary} wrapMode="none" overflow="hidden">{shorten(skill.id, idWidth)}</text>
      {!context.compact ? <text flexGrow={1} fg={context.selected ? tuiTheme.background : tuiTheme.muted} wrapMode="none" overflow="hidden">{shorten(skill.description, Math.max(18, context.width - idWidth - 10))}</text> : null}
    </>
  )
}

/** Thread 行只渲染用户可识别摘要与元数据，窄终端下保持单列。 */
function threadPickerRow(thread: ThreadPickerItem, context: SearchPickerRenderContext): ReactNode {
  const summaryWidth = context.compact
    ? Math.max(18, context.width - 6)
    : Math.max(24, Math.min(34, Math.floor(context.width * 0.34)))
  const meta = `${threadUpdatedLabel(thread.updatedAtMs)} · ${thread.messageCount} 条消息`
  return (
    <>
      <text width={summaryWidth} fg={context.selected ? tuiTheme.background : tuiTheme.primary} wrapMode="none" overflow="hidden">{shorten(thread.firstMessage, summaryWidth)}</text>
      {!context.compact ? <text flexGrow={1} fg={context.selected ? tuiTheme.background : tuiTheme.muted} wrapMode="none" overflow="hidden">{shorten(meta, Math.max(18, context.width - summaryWidth - 10))}</text> : null}
    </>
  )
}

/** 渲染输入框下方的配置摘要：模型靠左、审批模式靠右，避免重复品牌和拥挤折行。 */
function RuntimeMeta(props: { runtime: TuiRuntime; variant: "home" | "thread"; terminalWidth: number }) {
  // 首页 composer 最大宽度固定为 75 列；thread 则以可用终端宽度估算。模型字段
  // 是唯一可能来自企业配置的长文本，因此只截断它，审批模式始终保持可见。
  const contentWidth = props.variant === "home"
    ? Math.min(68, Math.max(28, props.terminalWidth - 8))
    : Math.max(28, props.terminalWidth - 10)
  const model = shorten(modelLabel(props.runtime), Math.max(14, contentWidth - 14))
  const warning = props.runtime.approvalModeWarning
    ? shorten(props.runtime.approvalModeWarning, contentWidth)
    : undefined
  const startupError = props.runtime.startupError
    ? shorten(`配置需要处理：${props.runtime.startupError}`, contentWidth)
    : undefined

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1}>
      <box width="100%" flexDirection="row" justifyContent="space-between" gap={2}>
        <text fg={props.runtime.modelConfigured ? tuiTheme.text : tuiTheme.warning}>{model}</text>
        <text fg={props.runtime.approvalMode === "yolo" ? tuiTheme.warning : tuiTheme.muted}>{approvalModeLabel(props.runtime)}</text>
      </box>
      {warning ? <text fg={tuiTheme.warning}>{warning}</text> : null}
      {startupError ? <text fg={tuiTheme.warning}>{startupError}</text> : null}
    </box>
  )
}

/** 渲染工作区、Git 分支、运行快捷键和 CLI 版本底栏。 */
function FooterRail(props: { runtime: TuiRuntime; state: TuiState; terminalWidth: number; thread?: boolean }) {
  const showFullPath = props.terminalWidth >= 108
  const showBranch = props.terminalWidth >= 84 && props.runtime.gitBranch
  const workspace = showFullPath ? props.runtime.workspace : workspaceLabel(props.runtime.workspace)

  return (
    <box flexDirection="row" justifyContent="space-between" gap={1} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexShrink={0}>
      <box flexDirection="row" gap={1} flexShrink={1}>
        <text fg={tuiTheme.muted}>{workspace}</text>
        {showBranch ? <text fg={tuiTheme.subtle}>:{props.runtime.gitBranch}</text> : null}
      </box>
      {props.state.activeRun ? <BusyRunHint /> : props.thread ? <text fg={tuiTheme.muted}>↑↓ 历史 · PgUp/PgDn 滚动 · Ctrl+O 工具</text> : null}
      <text fg={tuiTheme.subtle}>v{props.runtime.cliVersion}</text>
    </box>
  )
}

/** 运行中底栏提示，使用同一 spinner 视觉语言。 */
function BusyRunHint() {
  const frame = useSpinner(true, 80)
  return (
    <box flexDirection="row" gap={1}>
      <text fg={tuiTheme.primary}>{frame}</text>
      <text fg={tuiTheme.muted}>PgUp/PgDn 滚动 · Esc 中断</text>
    </box>
  )
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

/** 管理 spinner 定时器，并在组件卸载时清理。 */
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

/** 将运行时模型配置转换为简短状态文案。 */
function modelLabel(runtime: TuiRuntime): string {
  return runtime.modelConfigured ? (runtime.modelName ?? "已配置模型") : "模型未配置"
}

/** 按字符数截断普通预览文本。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

/** 将更新时间收敛为短标签，避免 picker 因本地化长日期改变固定行高。 */
function threadUpdatedLabel(updatedAtMs: number): string {
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - updatedAtMs) / 60_000))
  if (elapsedMinutes < 1) return "刚刚"
  if (elapsedMinutes < 60) return `${elapsedMinutes} 分钟前`
  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) return `${elapsedHours} 小时前`
  return `${Math.floor(elapsedHours / 24)} 天前`
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

/** 将运行状态映射到统一语义色。 */
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
