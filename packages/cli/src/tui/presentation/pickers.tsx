/** Skill、Thread 与只读 Agent 选择器的领域行视图。 */

import { type TextareaRenderable } from "@opentui/core"
import { type ReactNode, type RefObject } from "react"

import type { AgentSummary } from "@za38/protocol"

import type { SkillMenuItem } from "../../interactive/commands"
import { agentBrowsePurpose, agentKindLabel } from "../../presentation-shared/agent-catalog"
import { SearchPicker, type SearchPickerRenderContext } from "./overlays"
import { tuiTheme } from "./theme"
import type { ThreadPickerItem } from "./types"

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
  workMode?: "build" | "compose"
}) {
  return (
    <SearchPicker
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.skills}
      workMode={props.workMode}
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

/** 只读 Agent 浏览浮层；Enter/点击关闭，不切换当前 Agent。 */
export function AgentPicker(props: {
  visible: boolean
  loading: boolean
  error?: string
  agents: readonly AgentSummary[]
  query: string
  selectedIndex: number
  terminalWidth: number
  terminalHeight: number
  searchRef: RefObject<TextareaRenderable | null>
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  onSearch: (query: string) => void
  onSelect: (agent: AgentSummary) => void
  onHover: (index: number) => void
  onClose: () => void
  workMode?: "build" | "compose"
}) {
  return (
    <SearchPicker
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.agents}
      workMode={props.workMode}
      query={props.query}
      selectedIndex={props.selectedIndex}
      terminalWidth={props.terminalWidth}
      terminalHeight={props.terminalHeight}
      searchRef={props.searchRef}
      restoreFocusRef={props.restoreFocusRef}
      shouldRestoreFocus={props.shouldRestoreFocus}
      searchId="agent-search"
      title="可派发 Agent"
      searchPlaceholder="搜索 Agent..."
      emptyMessage="当前没有可派发的 Agent"
      loadingMessage="正在读取 Agent 目录…"
      footer="只读浏览 · Esc 关闭 · 派出请在对话里让主 Agent 使用 task"
      itemKey={agent => agent.id}
      renderItem={(agent, context) => agentPickerRow(agent, context)}
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
  workMode?: "build" | "compose"
}) {
  return (
    <SearchPicker
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.threads}
      workMode={props.workMode}
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

/** Agent 行：id + 来源/用途；不展示工具名单。 */
function agentPickerRow(agent: AgentSummary, context: SearchPickerRenderContext): ReactNode {
  const idWidth = context.compact
    ? Math.max(18, context.width - 6)
    : Math.max(22, Math.min(28, Math.floor(context.width * 0.28)))
  const meta = `${agentKindLabel(agent.kind)} · ${agentBrowsePurpose(agent)}`
  return (
    <>
      <text width={idWidth} fg={context.selected ? tuiTheme.background : tuiTheme.primary} wrapMode="none" overflow="hidden">{shorten(agent.id, idWidth)}</text>
      {!context.compact ? <text flexGrow={1} fg={context.selected ? tuiTheme.background : tuiTheme.muted} wrapMode="none" overflow="hidden">{shorten(meta, Math.max(18, context.width - idWidth - 10))}</text> : null}
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


/** 将更新时间收敛为短标签，避免 picker 因本地化长日期改变固定行高。 */
function threadUpdatedLabel(updatedAtMs: number): string {
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - updatedAtMs) / 60_000))
  if (elapsedMinutes < 1) return "刚刚"
  if (elapsedMinutes < 60) return `${elapsedMinutes} 分钟前`
  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) return `${elapsedHours} 小时前`
  return `${Math.floor(elapsedHours / 24)} 天前`
}

/** 按字符数截断选择器行，保持固定单行布局。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}
