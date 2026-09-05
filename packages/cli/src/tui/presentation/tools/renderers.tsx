/** 四种工具 Renderer：inline / block / diff / generic。 */

import { useRenderer } from "@opentui/react"
import { useState } from "react"

import type { TimelineItem, ToolCard } from "../../../interactive/state"
import { boundVisibleText, TOOL_PREVIEW_BUDGET, WRITE_FILE_EXPANDED_BUDGET, writeFileVisibleBody } from "../../../presentation-shared/paint-budget"
import { diffTextForRenderer, parseFileDiff } from "../../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../../presentation-shared/language-catalog"
import {
  hasTaskDispatchView,
  parseTaskDispatch,
  TASK_DISPATCH_RESULT_LABEL,
  TASK_DISPATCH_TASK_LABEL,
  taskDispatchLabel,
} from "../../../presentation-shared/task-dispatch"
import { toolArgumentSummary } from "../../../presentation-shared/tool-output-policy"
import { getCommonSyntaxClient } from "../../platform/syntax-parsers"
import { useSpinner } from "../input-bar"
import { markdownSyntax, tuiTheme } from "../theme"
import { parseFileMutationArgs, parseToolResultPreview, shortMutationPath, unifiedDiffFromReplacement } from "./file-mutation-args"
import { canonicalizeToolName, resolveToolRenderer } from "./registry"
import { currentTodo, parseWriteTodos, todoMarker, todoProgressLabel, type TodoItem } from "./write-todos"

function diffViewForWidth(contentWidth: number): "split" | "unified" {
  return contentWidth >= 120 ? "split" : "unified"
}

/** 按工具名选择 Renderer；未知走 generic。 */
export function ToolRenderer(props: {
  tool: ToolCard
  terminalWidth: number
  todoDetail?: boolean
  onOpenChildTimeline?: (executionId: string) => void
}) {
  const kind = resolveToolRenderer(props.tool.name)
  const name = canonicalizeToolName(props.tool.name)
  if (name === "write" || name === "write_file") return <WriteTool tool={props.tool} />
  if (name === "edit" || name === "edit_file") return <EditTool tool={props.tool} terminalWidth={props.terminalWidth} />
  if (name === "ask_user") return <AskUserTool tool={props.tool} />
  if (name === "write_todos") return <TodoTool tool={props.tool} detail={props.todoDetail !== false} />
  if (name === "task") return <TaskTool tool={props.tool} onOpenChildTimeline={props.onOpenChildTimeline} />
  if (kind === "inline") return <InlineTool tool={props.tool} />
  if (kind === "block") return <BlockTool tool={props.tool} />
  if (kind === "diff") return <DiffTool tool={props.tool} terminalWidth={props.terminalWidth} />
  return <GenericTool tool={props.tool} />
}

function InlineTool(props: { tool: ToolCard }) {
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const summary = toolArgumentSummary(props.tool.arguments)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.muted
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={tone}>{running ? frame : failed ? "×" : "→"}</text>
      <text fg={tuiTheme.text}>{props.tool.name}</text>
      {summary ? <text fg={tuiTheme.muted}>{summary}</text> : null}
    </box>
  )
}

function BlockTool(props: { tool: ToolCard }) {
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const preview = boundVisibleText(props.tool.output, TOOL_PREVIEW_BUDGET)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.success
  return (
    <box marginTop={1} marginLeft={3} marginRight={3} backgroundColor={tuiTheme.surface} paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} flexDirection="column">
      <box flexDirection="row" gap={1}>
        <text fg={tone}>{running ? frame : failed ? "×" : "✓"}</text>
        <text fg={tuiTheme.text}>{props.tool.name}</text>
      </box>
      {preview.text ? <text content={preview.text} fg={tuiTheme.muted} /> : null}
      {preview.overflow ? <text fg={tuiTheme.subtle}>还有 {preview.hiddenLines} 行</text> : null}
    </box>
  )
}

function DiffTool(props: { tool: ToolCard; terminalWidth: number }) {
  const failed = props.tool.status === "failed"
  const running = props.tool.status === "running"
  const frame = useSpinner(running, 80)
  const mutation = parseFileMutationArgs(props.tool.arguments)
  const parsed = parseFileDiff(props.tool.output)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.muted
  const hasDiff = parsed.status === "parsed" && props.tool.output.trim() !== ""
  if (hasDiff) {
    const view = diffViewForWidth(Math.max(1, props.terminalWidth - 10))
    const pathLabel = mutation.path ? shortMutationPath(mutation.path) : toolArgumentSummary(props.tool.arguments)
    return (
      <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="column">
        <box flexDirection="row" gap={1}>
          <text fg={tone}>{running ? frame : failed ? "×" : "±"}</text>
          <text fg={tuiTheme.text}>{props.tool.name}</text>
          {pathLabel ? <text fg={tuiTheme.muted}>{pathLabel}</text> : null}
        </box>
        <diff
          width="100%"
          diff={diffTextForRenderer(props.tool.output)}
          view={view}
          syncScroll
          showLineNumbers
          wrapMode="word"
          fg={tuiTheme.text}
          lineNumberFg={tuiTheme.muted}
          lineNumberBg={tuiTheme.toolSurface}
          addedBg={tuiTheme.diffAddedBackground}
          removedBg={tuiTheme.diffRemovedBackground}
          contextBg={tuiTheme.toolSurface}
          addedSignColor={tuiTheme.diffAdd}
          removedSignColor={tuiTheme.diffRemove}
          addedLineNumberBg={tuiTheme.diffAddedBackground}
          removedLineNumberBg={tuiTheme.diffRemovedBackground}
          syntaxStyle={markdownSyntax}
          treeSitterClient={getCommonSyntaxClient()}
        />
      </box>
    )
  }
  if (mutation.content !== null) {
    return <WriteFilePreview tool={props.tool} path={mutation.path} content={mutation.content} />
  }
  return <GenericTool tool={props.tool} />
}

/** edit_file 专用：用 old/new 生成 Diff，绝不铺 JSON 参数或结果。 */
function EditTool(props: { tool: ToolCard; terminalWidth: number }) {
  const mutation = parseFileMutationArgs(props.tool.arguments)
  const result = parseToolResultPreview(props.tool.output)
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const outputDiff = parseFileDiff(props.tool.output)
  const hasOutputDiff = outputDiff.status === "parsed" && props.tool.output.trim() !== ""
  if (mutation.oldString !== null && mutation.newString !== null) {
    const path = mutation.path ?? "file"
    const unified = unifiedDiffFromReplacement(path, mutation.oldString, mutation.newString)
    const view = diffViewForWidth(Math.max(1, props.terminalWidth - 10))
    const language = resolveLanguageForPath(mutation.path).tuiParser
    const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.success
    return (
      <box
        marginTop={1}
        marginLeft={3}
        marginRight={3}
        backgroundColor={tuiTheme.surface}
        paddingLeft={1}
        paddingRight={1}
        paddingTop={1}
        paddingBottom={1}
        flexDirection="column"
      >
        <box flexDirection="row" gap={1}>
          <text fg={tone}>{running ? frame : failed ? "×" : "←"}</text>
          <text fg={tuiTheme.text}>{running ? "Editing" : "Edit"}</text>
          <text fg={tuiTheme.muted}>{shortMutationPath(path)}</text>
        </box>
        <diff
          width="100%"
          diff={diffTextForRenderer(unified)}
          view={view}
          syncScroll
          showLineNumbers
          wrapMode="word"
          filetype={language === "plaintext" ? undefined : language}
          fg={tuiTheme.text}
          lineNumberFg={tuiTheme.muted}
          lineNumberBg={tuiTheme.toolSurface}
          addedBg={tuiTheme.diffAddedBackground}
          removedBg={tuiTheme.diffRemovedBackground}
          contextBg={tuiTheme.toolSurface}
          addedSignColor={tuiTheme.diffAdd}
          removedSignColor={tuiTheme.diffRemove}
          addedLineNumberBg={tuiTheme.diffAddedBackground}
          removedLineNumberBg={tuiTheme.diffRemovedBackground}
          syntaxStyle={markdownSyntax}
          treeSitterClient={getCommonSyntaxClient()}
        />
      </box>
    )
  }
  if (hasOutputDiff) {
    return <DiffTool tool={props.tool} terminalWidth={props.terminalWidth} />
  }
  const previewPath = mutation.path ?? result.path
  const previewContent = result.content
  if (previewContent !== null) {
    return <WriteFilePreview tool={props.tool} path={previewPath} content={previewContent} title={running ? "Editing" : "Edit"} />
  }
  if (running) {
    return (
      <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
        <text fg={tuiTheme.thinking}>{frame}</text>
        <text fg={tuiTheme.thinking}>Preparing edit</text>
        {previewPath ? <text fg={tuiTheme.muted}>{shortMutationPath(previewPath)}</text> : null}
      </box>
    )
  }
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={failed ? tuiTheme.danger : tuiTheme.success}>{failed ? "×" : "←"}</text>
      <text fg={tuiTheme.text}>Edit</text>
      {previewPath ? <text fg={tuiTheme.muted}>{shortMutationPath(previewPath)}</text> : null}
    </box>
  )
}

/** write_file 专用：准备中只提示 Preparing write，有正文就高亮，绝不铺 JSON。 */
function WriteTool(props: { tool: ToolCard }) {
  const mutation = parseFileMutationArgs(props.tool.arguments)
  const result = parseToolResultPreview(props.tool.output)
  const path = mutation.path ?? result.path
  const content = mutation.content ?? result.content
  if (content !== null) {
    return <WriteFilePreview tool={props.tool} path={path} content={content} />
  }
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  if (running) {
    return (
      <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
        <text fg={tuiTheme.thinking}>{frame}</text>
        <text fg={tuiTheme.thinking}>Preparing write</text>
        {path ? <text fg={tuiTheme.muted}>{shortMutationPath(path)}</text> : null}
      </box>
    )
  }
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={failed ? tuiTheme.danger : tuiTheme.success}>{failed ? "×" : "←"}</text>
      <text fg={tuiTheme.text}>Wrote</text>
      {path ? <text fg={tuiTheme.muted}>{shortMutationPath(path)}</text> : null}
    </box>
  )
}

/** write_file：按路径高亮正文，不展示转义 JSON。 */
function WriteFilePreview(props: { tool: ToolCard; path: string | null; content: string; title?: string }) {
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const [expanded, setExpanded] = useState(false)
  const preview = writeFileVisibleBody(props.content, expanded)
  const language = resolveLanguageForPath(props.path).tuiParser
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.success
  const canToggle = preview.overflow || expanded
  return (
    <box
      marginTop={1}
      marginLeft={3}
      marginRight={3}
      backgroundColor={tuiTheme.surface}
      paddingLeft={1}
      paddingRight={1}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <box flexDirection="row" gap={1} onMouseUp={canToggle ? () => setExpanded(current => !current) : undefined}>
        <text fg={tone}>{running ? frame : failed ? "×" : "←"}</text>
        <text fg={tuiTheme.text}>{props.title ?? (running ? "Writing" : "Wrote")}</text>
        <text fg={tuiTheme.muted}>{props.path ? shortMutationPath(props.path) : props.tool.name}</text>
        {canToggle ? <text fg={tuiTheme.subtle}>{expanded ? "收起" : "展开"}</text> : null}
      </box>
      {preview.text ? (
        <line-number fg={tuiTheme.muted} minWidth={3} paddingRight={1}>
          <code
            content={preview.text}
            filetype={language === "plaintext" ? undefined : language}
            syntaxStyle={markdownSyntax}
            treeSitterClient={getCommonSyntaxClient()}
            conceal={false}
            fg={tuiTheme.text}
          />
        </line-number>
      ) : (
        <text fg={tuiTheme.muted}>创建空文件</text>
      )}
      {preview.overflow ? <text fg={tuiTheme.subtle}>还有 {preview.hiddenLines} 行</text> : null}
    </box>
  )
}

/** write_todos：完整清单或一行摘要，绝不铺 JSON。 */
function TodoTool(props: { tool: ToolCard; detail: boolean }) {
  const items = todosForTool(props.tool)
  if (!items.length) return <GenericTool tool={props.tool} />
  if (!props.detail) return <TodoSummary items={items} running={props.tool.status === "running"} failed={props.tool.status === "failed"} />
  return <TodoPanel items={items} />
}

/** 时间线里最后一次 write_todos 的工具 id。 */
export function latestWriteTodosId(timeline: readonly TimelineItem[]): string | null {
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const item = timeline[index]
    if (item.type !== "tool") continue
    if (canonicalizeToolName(item.tool.name) !== "write_todos") continue
    if (todosForTool(item.tool).length) return item.tool.id
  }
  return null
}

/** 运行中且该次 write_todos 后面还有内容时，才把完整清单钉到底部，避免双份。 */
export function shouldPinTodos(timeline: readonly TimelineItem[], activeRun: boolean): boolean {
  if (!activeRun) return false
  const latestId = latestWriteTodosId(timeline)
  if (!latestId) return false
  const index = timeline.findIndex(item => item.type === "tool" && item.tool.id === latestId)
  if (index < 0) return false
  return timeline.slice(index + 1).some(item => item.type !== "message" || item.message.role !== "user")
}

/** 从工具参数或结果抽出当前清单。 */
export function todosForTool(tool: ToolCard): TodoItem[] {
  const fromArgs = parseWriteTodos(tool.arguments)
  return fromArgs.length ? fromArgs : parseWriteTodos(tool.output)
}

/** 时间线里最后一次 write_todos 的清单，供运行中跟踪条使用。 */
export function latestTodos(timeline: readonly TimelineItem[]): TodoItem[] {
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const item = timeline[index]
    if (item.type !== "tool") continue
    if (canonicalizeToolName(item.tool.name) !== "write_todos") continue
    const items = todosForTool(item.tool)
    if (items.length) return items
  }
  return []
}

function TodoSummary(props: { items: readonly TodoItem[]; running: boolean; failed: boolean }) {
  const frame = useSpinner(props.running, 80)
  const tone = props.failed ? tuiTheme.danger : props.running ? tuiTheme.thinking : tuiTheme.muted
  const current = currentTodo(props.items)
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="row" gap={1}>
      <text fg={tone}>{props.running ? frame : "·"}</text>
      <text fg={tuiTheme.muted}>TODO</text>
      <text fg={tuiTheme.subtle}>{todoProgressLabel(props.items)}</text>
      {current ? <text content={current.content} fg={tuiTheme.muted} /> : null}
    </box>
  )
}

/** 完整清单：一块 surface，标题 + 进度 + 对齐的状态列。 */
export function TodoPanel(props: { items: readonly TodoItem[] }) {
  return (
    <box
      marginTop={1}
      marginLeft={3}
      marginRight={3}
      backgroundColor={tuiTheme.surface}
      paddingLeft={1}
      paddingRight={1}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <box flexDirection="row" gap={2} justifyContent="space-between">
        <text fg={tuiTheme.text}>TODO</text>
        <text fg={tuiTheme.subtle}>{todoProgressLabel(props.items)}</text>
      </box>
      {props.items.map((item, index) => {
        const active = item.status === "in_progress"
        const done = item.status === "completed"
        const mark = todoMarker(item.status)
        const markColor = active ? tuiTheme.thinking : done ? tuiTheme.success : tuiTheme.subtle
        const textColor = active ? tuiTheme.text : done ? tuiTheme.subtle : tuiTheme.muted
        return (
          <box key={`${index}:${item.content}`} flexDirection="row" gap={1}>
            <text fg={markColor}>{mark}</text>
            <text content={item.content} fg={textColor} />
          </box>
        )
      })}
    </box>
  )
}

/** task：标题是派出对象；任务与结论分区用正文色；结论可点开展开剩余行。 */
function TaskTool(props: { tool: ToolCard; onOpenChildTimeline?: (executionId: string) => void }) {
  const renderer = useRenderer()
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.success
  const [expanded, setExpanded] = useState(false)
  const view = parseTaskDispatch(props.tool.arguments)
  if (!hasTaskDispatchView(view)) return <GenericTool tool={props.tool} />
  const output = boundVisibleText(
    props.tool.output,
    expanded ? WRITE_FILE_EXPANDED_BUDGET : TOOL_PREVIEW_BUDGET,
  )
  const canToggle = output.overflow || expanded
  const toggleExpanded = (): void => setExpanded(current => !current)
  const childId = props.tool.childExecutionId
  return (
    <box
      // 卡片按内容高度进入滚动区；压缩会令标题高度归零，留下可见文字却没有命中区域。
      flexShrink={0}
      marginTop={1}
      marginLeft={3}
      marginRight={3}
      backgroundColor={tuiTheme.surface}
      paddingLeft={1}
      paddingRight={1}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <box
        flexDirection="row"
        flexShrink={0}
        minHeight={1}
        gap={1}
        onMouseDown={childId ? event => {
          if (event.button !== 0) return
          event.stopPropagation()
          // 按下时立即触发，避免 spinner/拖选自动滚动后 mouse-up 落到其他 Renderable。
          renderer.clearSelection()
          props.onOpenChildTimeline?.(childId)
        } : undefined}
        onMouseUp={childId ? event => {
          if (event.button === 0) event.stopPropagation()
        } : undefined}
      >
        <text fg={tone}>{running ? frame : failed ? "×" : "→"}</text>
        <text fg={tuiTheme.text}>{taskDispatchLabel(view)}</text>
        {childId ? <text fg={tuiTheme.primary}>进入子时间线</text> : null}
      </box>
      {view.description ? (
        <>
          <text fg={tuiTheme.text}>{TASK_DISPATCH_TASK_LABEL}</text>
          <text content={view.description} fg={tuiTheme.text} />
        </>
      ) : null}
      {output.text ? (
        <>
          <box flexDirection="row" gap={1} onMouseUp={canToggle ? toggleExpanded : undefined}>
            <text fg={tuiTheme.text}>{TASK_DISPATCH_RESULT_LABEL}</text>
            {canToggle ? <text fg={tuiTheme.muted}>{expanded ? "收起" : "展开"}</text> : null}
          </box>
          <markdown
            content={output.text}
            syntaxStyle={markdownSyntax}
            treeSitterClient={getCommonSyntaxClient()}
            streaming={false}
            fg={tuiTheme.text}
            bg={tuiTheme.surface}
            conceal
            concealCode={false}
            internalBlockMode="top-level"
            tableOptions={{ style: "columns", borders: false }}
          />
          {output.overflow ? (
            <box onMouseUp={toggleExpanded}>
              <text fg={tuiTheme.muted}>还有 {output.hiddenLines} 行</text>
            </box>
          ) : null}
        </>
      ) : null}
    </box>
  )
}

/** 从 ask_user 参数抽出问题标题，禁止把 JSON 铺开。 */
function askUserQuestionTitles(argumentsText: string): string[] {
  try {
    const parsed = JSON.parse(argumentsText) as { questions?: unknown }
    const questions = Array.isArray(parsed.questions) ? parsed.questions : []
    return questions.flatMap(item => {
      if (!item || typeof item !== "object") return []
      const question = (item as { question?: unknown }).question
      return typeof question === "string" && question.trim() ? [question.trim()] : []
    })
  } catch {
    return []
  }
}

/** ask_user 进行中只显示问题标题；选项由底部 Dock 承接。 */
function AskUserTool(props: { tool: ToolCard }) {
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const titles = askUserQuestionTitles(props.tool.arguments)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.muted
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="column">
      <box flexDirection="row" gap={1}>
        <text fg={tone}>{running ? frame : failed ? "×" : "?"}</text>
        <text fg={tuiTheme.text}>ask_user</text>
        {titles.length > 1 ? <text fg={tuiTheme.muted}>{titles.length} 个问题</text> : null}
      </box>
      {titles[0] ? <text content={titles[0]} fg={tuiTheme.muted} /> : null}
    </box>
  )
}

function GenericTool(props: { tool: ToolCard }) {
  const running = props.tool.status === "running"
  const failed = props.tool.status === "failed"
  const frame = useSpinner(running, 80)
  const input = boundVisibleText(props.tool.arguments, TOOL_PREVIEW_BUDGET)
  const output = boundVisibleText(props.tool.output, TOOL_PREVIEW_BUDGET)
  const tone = failed ? tuiTheme.danger : running ? tuiTheme.thinking : tuiTheme.muted
  return (
    <box marginTop={1} paddingLeft={3} paddingRight={3} flexDirection="column">
      <box flexDirection="row" gap={1}>
        <text fg={tone}>{running ? frame : failed ? "×" : "◇"}</text>
        <text fg={tuiTheme.text}>{props.tool.name}</text>
      </box>
      {input.text ? <text content={input.text} fg={tuiTheme.subtle} /> : null}
      {output.text ? <text content={output.text} fg={tuiTheme.muted} /> : null}
      {output.overflow ? <text fg={tuiTheme.subtle}>还有 {output.hiddenLines} 行</text> : null}
    </box>
  )
}
