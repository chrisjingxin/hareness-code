/** Composer、运行状态与底栏视图。 */

import { useEffect, useState } from "react"

import {
  commandMenuItemDescription,
  commandMenuItemLabel,
  type CommandMenuItem,
} from "../../interactive/commands"
import {
  approvalModeLabel,
  workspaceLabel,
} from "../../interactive/runtime"
import { tuiTheme } from "./theme"
import { activityLabel, gitWorkspaceLabel, modelSelectionLabel } from "../../presentation-shared"
import type { SharedViewProps } from "./types"

/** thread composer 上方的实时模型和运行状态行。 */
export function ThreadRuntimeLine(props: { interactive: SharedViewProps["interactive"] }) {
  const runtime = props.interactive.runtime
  const status = activityLabel(props.interactive.activity.kind)
  return (
    <box flexDirection="row" gap={1} paddingBottom={1}>
      <text fg={statusColor(props.interactive.activity.kind)}>□</text>
      <text fg={tuiTheme.primary}>Harness Code</text>
      <text fg={tuiTheme.muted}>·</text>
      <text fg={runtime.modelConfigured ? tuiTheme.text : tuiTheme.warning}>模型 {modelSelectionLabel(props.interactive)}</text>
      <text fg={tuiTheme.muted}>· {status}</text>
    </box>
  )
}

/** 渲染统一左轨 composer、命令菜单和运行时元信息。 */
export function Composer(props: Pick<SharedViewProps, "interactive" | "terminalWidth" | "inputRef" | "value" | "onInput" | "onComposerKeyDown" | "onSubmit" | "commandMenu" | "commandOptions" | "onSelectCommand" | "onHoverCommand" | "selectedSkill" | "pickerVisible" | "onClearSelectedSkill"> & {
  variant: "home" | "thread"
  commandMenuPlacement: "above" | "inline-below"
}) {
  const active = Boolean(props.interactive.activeRun)
  const awaitingQuestion = props.interactive.interaction?.type === "question"
  const options = props.commandOptions
  const placeholder = awaitingQuestion
    ? "输入你的回答后按 Enter"
    : active
      ? "正在执行；Esc 中断"
      : "输入消息..（输入 / 唤起命令）"

  const commandMenu = props.commandMenu.visible ? (
    <CommandMenu
      options={options}
      selectedIndex={Math.min(props.commandMenu.selectedIndex, Math.max(0, options.length - 1))}
      onSelect={props.onSelectCommand}
      onHover={props.onHoverCommand}
      placement={props.commandMenuPlacement}
    />
  ) : null

  const isHome = props.variant === "home"
  const modeBadgeLabel = props.interactive.runtime.approvalMode === "yolo" ? "Yolo" : "Build"

  return (
    <box position="relative" flexDirection="column" flexShrink={0}>
      {commandMenu && props.commandMenuPlacement === "above" ? (
        <box position="absolute" left={0} bottom="100%" width="100%" zIndex={10}>
          {commandMenu}
        </box>
      ) : null}
      <box
        border={isHome ? [] : ["left"]}
        borderColor={active ? tuiTheme.primarySoft : tuiTheme.primary}
        customBorderChars={PROMPT_BORDER}
      >
        <box backgroundColor={tuiTheme.composer} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={isHome ? 1 : 0} flexShrink={0} flexGrow={1}>
          {isHome ? (
            <box flexDirection="row" gap={2} alignItems="flex-start">
              <text fg={tuiTheme.warning} bold>{modeBadgeLabel}</text>
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
            </box>
          ) : (
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
          )}
          {props.selectedSkill ? (
            <box paddingTop={1} flexDirection="row" gap={1}>
              <text fg={tuiTheme.primary}>Skill</text>
              <text fg={tuiTheme.text}>{props.selectedSkill.id}</text>
              <text fg={tuiTheme.muted}>{props.selectedSkill.argumentHint ?? "下一条消息使用"}</text>
              <text fg={tuiTheme.muted} onMouseUp={props.onClearSelectedSkill}>×</text>
            </box>
          ) : null}
          <RuntimeMeta interactive={props.interactive} variant={props.variant} terminalWidth={props.terminalWidth} />
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


/** 渲染输入框下方的配置摘要，只展示当前 Thread 的模型选择。 */
function RuntimeMeta(props: { interactive: SharedViewProps["interactive"]; variant: "home" | "thread"; terminalWidth: number }) {
  // 首页 composer 最大宽度固定为 75 列；thread 则以可用终端宽度估算。模型字段
  // 是唯一可能来自企业配置的长文本，因此只截断它，审批模式始终保持可见。
  const runtime = props.interactive.runtime
  const contentWidth = props.variant === "home"
    ? Math.min(68, Math.max(28, props.terminalWidth - 8))
    : Math.max(28, props.terminalWidth - 10)
  // Shift+Tab 快捷键提示紧跟审批模式展示；窄终端隐藏提示，优先保住模式本身。
  const showApprovalHint = contentWidth >= 52
  const modelName = modelSelectionLabel(props.interactive)
  const model = shorten(modelName, Math.max(14, contentWidth - (showApprovalHint ? 22 : 14)))
  const warning = runtime.approvalModeWarning
    ? shorten(runtime.approvalModeWarning, contentWidth)
    : undefined
  const startupError = runtime.startupError
    ? shorten(`配置需要处理：${runtime.startupError}`, contentWidth)
    : undefined

  if (props.variant === "home") {
    return (
      <box flexDirection="column" paddingTop={1}>
        <box width="100%" flexDirection="row" justifyContent="space-between" gap={2}>
          <text fg={runtime.modelConfigured ? tuiTheme.muted : tuiTheme.warning}>{model}</text>
          <box flexDirection="row" gap={2}>
            <text fg={tuiTheme.subtle}>shift+enter <span fg={tuiTheme.subtle}>new line</span></text>
            <text fg={tuiTheme.subtle}>tab <span fg={tuiTheme.subtle}>modes</span></text>
          </box>
        </box>
        {warning ? <text fg={tuiTheme.warning}>{warning}</text> : null}
        {startupError ? <text fg={tuiTheme.warning}>{startupError}</text> : null}
      </box>
    )
  }

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1}>
      <box width="100%" flexDirection="row" justifyContent="space-between" gap={2}>
        <text fg={runtime.modelConfigured ? tuiTheme.text : tuiTheme.warning}>模型：{model}</text>
        <box flexDirection="row" gap={1}>
          {showApprovalHint ? <text fg={tuiTheme.subtle}>Shift+Tab</text> : null}
          <text fg={runtime.approvalMode === "yolo" ? tuiTheme.warning : tuiTheme.muted}>{approvalModeLabel(runtime)}</text>
        </box>
      </box>
      {warning ? <text fg={tuiTheme.warning}>{warning}</text> : null}
      {startupError ? <text fg={tuiTheme.warning}>{startupError}</text> : null}
    </box>
  )
}

/** 渲染工作区、Git 分支、运行快捷键和 CLI 版本底栏。 */
export function FooterRail(props: { interactive: SharedViewProps["interactive"]; terminalWidth: number; thread?: boolean }) {
  const runtime = props.interactive.runtime
  const branchLabel = gitWorkspaceLabel(runtime.gitWorkspace)
  const showFullPath = props.terminalWidth >= 108
  const showBranch = props.terminalWidth >= 84 && branchLabel !== undefined
  const workspace = showFullPath ? runtime.workspace : workspaceLabel(runtime.workspace)

  return (
    <box flexDirection="row" justifyContent="space-between" gap={1} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexShrink={0}>
      <box flexDirection="row" gap={1} flexShrink={1}>
        <text fg={tuiTheme.muted}>{workspace}</text>
        {showBranch ? <text fg={tuiTheme.muted}>:{branchLabel}</text> : null}
      </box>
      {props.interactive.activeRun ? <BusyRunHint /> : props.thread ? <text fg={tuiTheme.muted}>↑↓ 历史 · PgUp/PgDn 滚动 · Ctrl+O 工具</text> : null}
      <text fg={tuiTheme.subtle}>v{runtime.cliVersion}</text>
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


/** 管理 spinner 定时器，并在组件卸载时清理。 */
export function useSpinner(active: boolean, interval: number): string {
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

/** 以最近一次 Host elapsed_ms 为基准在本地连续显示活动时长。 */
export function useRunElapsed(active: boolean, baseElapsedMs: number | undefined): number {
  const [elapsed, setElapsed] = useState(baseElapsedMs ?? 0)
  useEffect(() => {
    const base = Math.max(0, baseElapsedMs ?? 0)
    setElapsed(base)
    if (!active) return
    const startedAt = Date.now() - base
    const timer = setInterval(() => setElapsed(Math.max(0, Date.now() - startedAt)), 1_000)
    return () => clearInterval(timer)
  }, [active, baseElapsedMs])
  return elapsed
}

/** 按字符数截断 composer 内的长文案。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

/** 将运行活动 kind 映射到统一语义色。 */
function statusColor(kind: SharedViewProps["interactive"]["activity"]["kind"]): string {
  if (kind === "completed") return tuiTheme.success
  if (kind === "cancelled") return tuiTheme.muted
  if (kind === "failed") return tuiTheme.danger
  if (kind === "waiting-interaction") return tuiTheme.warning
  return tuiTheme.primary
}

export const PROMPT_BORDER = {
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
