/** OpenTUI 表现 adapter：把终端输入映射为 TuiIntent，并渲染 Controller snapshot。 */

import { createCliRenderer, type KeyEvent, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { createRoot, useKeyboard, useTerminalDimensions } from "@opentui/react"
import { useCallback, useEffect, useRef, useSyncExternalStore, type ReactNode } from "react"
import type { ModelProfile } from "@za38/protocol"

import {
  createTuiController,
  type ApprovalDecision,
  type TuiController,
  type TuiControllerOptions,
} from "./application/controller"
import type { CommandMenuItem } from "./application/commands"
import { isHomeState } from "./application/state"
import { resolveShortcut, type ScrollIntent } from "./application/shortcuts"
import { TuiErrorBoundary } from "./presentation/error-boundary"
import { HomeView } from "./presentation/home"
import { DialogShell, SearchPicker, type SearchPickerRenderContext } from "./presentation/overlays"
import { SkillPicker, ThreadPicker } from "./presentation/pickers"
import { tuiTheme } from "./presentation/theme"
import { ThreadView } from "./presentation/thread"
import { registerCommonSyntaxParsers, shutdownCommonSyntaxClient } from "./platform/syntax-parsers"
import { win32InstallVtInputGuard } from "./platform/terminal-win32"

/** 正式 TUI 的启动参数；Controller 和 OpenTUI 共用同一组生命周期选项。 */
export type TuiOptions = TuiControllerOptions

/** 正式 OpenTUI 根组件：所有业务状态来自 Controller snapshot。 */
export function Za38Tui(options: TuiOptions) {
  const controllerRef = useRef<TuiController | null>(null)
  if (!controllerRef.current) controllerRef.current = createTuiController(options)
  const controller = controllerRef.current
  const subscribe = useCallback((listener: (snapshot: ReturnType<TuiController["getSnapshot"]>) => void) => controller.subscribe(listener), [controller])
  const getSnapshot = useCallback(() => controller.getSnapshot(), [controller])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  const inputRef = useRef<TextareaRenderable | null>(null)
  const conversationScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const skillSearchRef = useRef<TextareaRenderable | null>(null)
  const threadSearchRef = useRef<TextareaRenderable | null>(null)
  const modelSearchRef = useRef<TextareaRenderable | null>(null)
  const terminal = useTerminalDimensions()
  const lastScrollRequestRef = useRef(snapshot.scrollRequest)

  useEffect(() => () => { void controller.close() }, [controller])

  const syncInputBuffer = useCallback((draft: string, cursor: "start" | "end" | undefined) => {
    const input = inputRef.current
    if (!input || input.plainText === draft) return
    input.setText(draft)
    if (cursor === "start") input.gotoBufferHome()
    else input.gotoBufferEnd()
  }, [])

  /** OpenTUI textarea 不是受控输入框；Controller draft 变化时同步其内部缓冲区。 */
  useEffect(() => {
    syncInputBuffer(snapshot.draft, snapshot.draftCursor)
  }, [snapshot.draft, snapshot.draftCursor, syncInputBuffer])

  /** Thread 恢复或提交消息后由 snapshot 请求滚动，scroll ref 仍属于表现层。 */
  const scrollToBottom = useCallback(() => {
    setTimeout(() => {
      const scroll = conversationScrollRef.current
      if (!scroll || scroll.isDestroyed) return
      scroll.scrollTo(scroll.scrollHeight)
    }, 50)
  }, [])

  useEffect(() => {
    if (lastScrollRequestRef.current === snapshot.scrollRequest) return
    lastScrollRequestRef.current = snapshot.scrollRequest
    scrollToBottom()
  }, [scrollToBottom, snapshot.scrollRequest])

  /** textarea 只负责光标边界和滚动 ref，历史业务交给 Controller。 */
  const handleComposerKeyDown = useCallback((key: KeyEvent) => {
    if (
      snapshot.commandMenu.visible
      || snapshot.commandDialog
      || snapshot.modelBindingDialog
      || snapshot.skills.visible
      || snapshot.threads.visible
      || snapshot.models.visible
    ) return
    const input = inputRef.current
    if (!input) return
    const currentSnapshot = controller.getSnapshot()

    const atStart = input.cursorOffset === 0
    const atEnd = input.cursorOffset === input.plainText.length
    if (key.name === "up" && (atStart || currentSnapshot.draftCursor === "start")) {
      void controller.dispatch({ type: "history", direction: "previous" })
      const next = controller.getSnapshot()
      syncInputBuffer(next.draft, next.draftCursor)
      key.preventDefault()
      return
    }
    if (key.name === "down" && (atEnd || currentSnapshot.draftCursor === "start")) {
      void controller.dispatch({ type: "history", direction: "next" })
      const next = controller.getSnapshot()
      syncInputBuffer(next.draft, next.draftCursor)
      key.preventDefault()
      return
    }

    if (!input.plainText && !isHomeState(snapshot.state)) {
      const scrollAction = key.name === "up" ? "line-up" : key.name === "down" ? "line-down" : undefined
      if (scrollAction && scrollConversation(scrollAction)) key.preventDefault()
    }
  }, [controller, snapshot.commandDialog, snapshot.commandMenu.visible, snapshot.modelBindingDialog, snapshot.models.visible, snapshot.skills.visible, snapshot.state, snapshot.threads.visible, syncInputBuffer])

  /** 通过 ref 滚动当前时间线；不把终端尺寸或 OpenTUI 对象带入 Controller。 */
  function scrollConversation(intent: ScrollIntent): boolean {
    const scroll = conversationScrollRef.current
    if (!scroll || scroll.isDestroyed) return false
    if (intent === "top") {
      scroll.scrollTo(0)
      return true
    }
    if (intent === "bottom") {
      scroll.scrollTo(scroll.scrollHeight)
      return true
    }
    const half = Math.max(1, Math.floor(scroll.height / 2))
    const delta = intent === "line-up" ? -1
      : intent === "line-down" ? 1
        : intent === "page-up" ? -half
          : half
    scroll.scrollBy(delta)
    return true
  }

  /** 全局快捷键只负责识别动作；具体状态转换由 Controller 处理。 */
  useKeyboard(key => {
    const action = resolveShortcut(key, {
      commandDialogVisible: Boolean(snapshot.commandDialog || snapshot.modelBindingDialog),
      skillPickerVisible: snapshot.skills.visible,
      skillOptionCount: snapshot.skills.items.length,
      threadPickerVisible: snapshot.threads.visible,
      threadOptionCount: snapshot.threads.items.length,
      modelPickerVisible: snapshot.models.visible,
      modelOptionCount: snapshot.models.items.length,
      commandMenuVisible: snapshot.commandMenu.visible,
      commandOptionCount: snapshot.commandOptions.length,
      activeRun: Boolean(snapshot.state.activeRun),
      hasDraft: Boolean(snapshot.draft),
    })
    if (action === "none") return
    key.preventDefault()

    const scrollIntent: ScrollIntent | undefined = action === "scroll-line-up" ? "line-up"
      : action === "scroll-line-down" ? "line-down"
        : action === "scroll-page-up" ? "page-up"
          : action === "scroll-page-down" ? "page-down"
            : action === "scroll-top" ? "top"
              : action === "scroll-bottom" ? "bottom"
                : undefined
    if (scrollIntent) {
      scrollConversation(scrollIntent)
      return
    }
    void controller.dispatch({ type: "shortcut", action })
  })

  const viewProps = {
    runtime: snapshot.runtime,
    state: snapshot.state,
    terminalWidth: terminal.width,
    terminalHeight: terminal.height,
    inputRef,
    conversationScrollRef,
    value: snapshot.draft,
    onInput: (value: string) => { void controller.dispatch({ type: "draft-input", value }) },
    onComposerKeyDown: handleComposerKeyDown,
    onSubmit: () => { void controller.dispatch({ type: "submit", value: inputRef.current?.plainText ?? snapshot.draft }) },
    commandMenu: snapshot.commandMenu,
    commandOptions: snapshot.commandOptions,
    onSelectCommand: (item: CommandMenuItem) => { void controller.dispatch({ type: "command-menu-select", item }) },
    onHoverCommand: (selectedIndex: number) => { void controller.dispatch({ type: "command-menu-hover", selectedIndex }) },
    selectedSkill: snapshot.selectedSkill,
    pickerVisible: Boolean(snapshot.commandDialog || snapshot.modelBindingDialog) || snapshot.skills.visible || snapshot.threads.visible || snapshot.models.visible,
    onClearSelectedSkill: () => { void controller.dispatch({ type: "clear-selected-skill" }) },
    showToolDetails: snapshot.showToolDetails,
    expandedTools: snapshot.expandedTools,
    onToggleTool: (toolId: string) => { void controller.dispatch({ type: "tool-toggle", toolId }) },
    onApproval: (decision: ApprovalDecision) => { void controller.dispatch({ type: "approval", decision }) },
    onQuestion: (answer: string) => { void controller.dispatch({ type: "question", answer }) },
  }

  return (
    <box position="relative" flexGrow={1}>
      {isHomeState(snapshot.state) ? <HomeView {...viewProps} /> : <ThreadView {...viewProps} modelName={snapshot.displayedModelName} />}
      <SkillPicker
        visible={snapshot.skills.visible}
        loading={snapshot.skills.loading}
        error={snapshot.skills.error}
        skills={snapshot.skills.items}
        query={snapshot.skills.query}
        selectedIndex={snapshot.skills.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={skillSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!snapshot.state.activeRun && !snapshot.state.pendingOperation}
        onSearch={query => { void controller.dispatch({ type: "picker-search", picker: "skills", query }) }}
        onSelect={skill => { void controller.dispatch({ type: "picker-select-skill", skill }) }}
        onHover={selectedIndex => { void controller.dispatch({ type: "picker-hover", picker: "skills", selectedIndex }) }}
        onClose={() => { void controller.dispatch({ type: "picker-close", picker: "skills" }) }}
      />
      <ThreadPicker
        visible={snapshot.threads.visible}
        loading={snapshot.threads.loading}
        error={snapshot.threads.error}
        threads={snapshot.threads.items}
        query={snapshot.threads.query}
        selectedIndex={snapshot.threads.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={threadSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!snapshot.state.activeRun && !snapshot.state.pendingOperation}
        onSearch={query => { void controller.dispatch({ type: "picker-search", picker: "threads", query }) }}
        onSelect={thread => { void controller.dispatch({ type: "picker-select-thread", thread }) }}
        onHover={selectedIndex => { void controller.dispatch({ type: "picker-hover", picker: "threads", selectedIndex }) }}
        onClose={() => { void controller.dispatch({ type: "picker-close", picker: "threads" }) }}
      />
      <SearchPicker<ModelProfile>
        visible={snapshot.models.visible}
        loading={snapshot.models.loading}
        error={snapshot.models.error}
        items={snapshot.models.items}
        query={snapshot.models.query}
        selectedIndex={snapshot.models.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={modelSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!snapshot.state.activeRun && !snapshot.state.pendingOperation}
        searchId="model-search"
        title={snapshot.state.threadId ? "选择当前 Thread 下一次运行的模型" : "选择下一次新 Thread 运行的模型"}
        searchPlaceholder="按 Profile、模型或 Provider 搜索"
        emptyMessage="没有匹配的模型 Profile"
        loadingMessage={snapshot.models.syncingDefault ? "正在同步后续新 Thread 默认模型…" : undefined}
        footer="选择后同时更新后续新 Thread 默认模型"
        itemKey={model => model.id}
        renderItem={(model, context) => modelPickerRow(model, context)}
        onSearch={query => { void controller.dispatch({ type: "picker-search", picker: "models", query }) }}
        onSelect={model => { void controller.dispatch({ type: "picker-select-model", model }) }}
        onHover={selectedIndex => { void controller.dispatch({ type: "picker-hover", picker: "models", selectedIndex }) }}
        onClose={() => { void controller.dispatch({ type: "picker-close", picker: "models" }) }}
      />
      <DialogShell
        visible={snapshot.commandDialog?.kind === "confirm-new-thread"}
        title={snapshot.commandDialog?.title ?? ""}
        message={snapshot.commandDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!snapshot.state.activeRun && !snapshot.state.pendingOperation}
        onConfirm={() => { void controller.dispatch({ type: "dialog-resolve", kind: "command", confirmed: true }) }}
        onCancel={() => { void controller.dispatch({ type: "dialog-resolve", kind: "command", confirmed: false }) }}
      />
      <DialogShell
        visible={Boolean(snapshot.modelBindingDialog)}
        title={snapshot.modelBindingDialog?.title ?? ""}
        message={snapshot.modelBindingDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!snapshot.state.activeRun && !snapshot.state.pendingOperation}
        confirmLabel="新建 Thread"
        cancelLabel="保留当前 Thread"
        onConfirm={() => { void controller.dispatch({ type: "dialog-resolve", kind: "model-binding", confirmed: true }) }}
        onCancel={() => { void controller.dispatch({ type: "dialog-resolve", kind: "model-binding", confirmed: false }) }}
      />
    </box>
  )
}

/** 创建 OpenTUI renderer、挂载错误边界；退出时将控制权交回 CLI 关闭 Python sidecar。 */
export async function runTui(options: TuiOptions): Promise<void> {
  registerCommonSyntaxParsers()
  const renderer = await createCliRenderer({
    externalOutputMode: "passthrough",
    targetFps: 60,
    maxFps: 60,
    gatherStats: false,
    clearOnShutdown: true,
    useKittyKeyboard: {},
    autoFocus: false,
    openConsoleOnError: false,
    useMouse: true,
  })
  const uninstallVtGuard = win32InstallVtInputGuard()
  const root = createRoot(renderer)
  await new Promise<void>(resolve => {
    let closed = false
    const close = () => {
      if (closed) return
      closed = true
      uninstallVtGuard?.()
      root.unmount()
      void shutdownCommonSyntaxClient().finally(() => {
        renderer.destroy()
        resolve()
      })
    }
    root.render(
      <TuiErrorBoundary onRequestExit={close}>
        <Za38Tui {...options} onRequestExit={close} />
      </TuiErrorBoundary>,
    )
  })
}

/** Model Picker 行避免展示 endpoint 或凭据，只展示已脱敏的 Profile DTO。 */
function modelPickerRow(model: ModelProfile, context: SearchPickerRenderContext): ReactNode {
  const idWidth = context.compact
    ? Math.max(16, context.width - 6)
    : Math.max(16, Math.min(26, Math.floor(context.width * 0.28)))
  const contextWindow = model.context_window_tokens >= 1_000
    ? `${Math.round(model.context_window_tokens / 1_000)}k`
    : String(model.context_window_tokens)
  const detail = model.available
    ? `${model.is_default ? "默认 · " : ""}${model.provider_label} · ${model.model} · ${contextWindow} · ${model.capabilities.join(",")}`
    : `${model.is_default ? "默认 · " : ""}${model.provider_label} · 不可用：${model.unavailable_reason ?? "配置不可用"}`
  const foreground = context.selected ? tuiTheme.background : model.available ? tuiTheme.primary : tuiTheme.muted
  const detailForeground = context.selected ? tuiTheme.background : model.available ? tuiTheme.muted : tuiTheme.danger
  return (
    <>
      <text width={idWidth} fg={foreground} wrapMode="none" overflow="hidden">{model.id}</text>
      {!context.compact ? <text flexGrow={1} fg={detailForeground} wrapMode="none" overflow="hidden">{detail}</text> : null}
    </>
  )
}
