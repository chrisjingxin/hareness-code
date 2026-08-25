/** OpenTUI 表现 adapter：把终端输入映射为 TuiIntent，并渲染 Adapter snapshot。 */

import { createCliRenderer, MouseButton, type CliRendererConfig, type KeyEvent, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { createRoot, useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/react"
import { useCallback, useEffect, useRef, useSyncExternalStore, type ReactNode } from "react"
import type { ModelProfile } from "@za38/protocol"

import type { InteractiveController } from "../interactive/types"
import type { AgentClient } from "../ipc/client"
import type { InteractiveRuntime } from "../interactive/runtime"
import {
  createTuiAdapter,
  type ApprovalDecision,
  type DirectoryTrustDecision,
  type TuiAdapter,
  type TuiAdapterOptions,
  type TuiAdapterSnapshot,
} from "./application/adapter"
import type { CommandMenuItem } from "../interactive/commands"
import { isHomeState } from "../interactive/state"
import type { InteractiveSnapshot } from "../interactive/types"
import { resolveShortcut, type ScrollIntent } from "./application/shortcuts"
import { TuiErrorBoundary } from "./presentation/error-boundary"
import { HomeView } from "./presentation/home"
import { DialogShell, SearchPicker, type SearchPickerRenderContext } from "./presentation/overlays"
import { AgentPicker, SkillPicker, ThreadPicker } from "./presentation/pickers"
import { BtwModal } from "./presentation/btw-modal"
import { copyCurrentSelection, shouldAttemptSelectionCopy } from "./presentation/selection-copy"
import { Sidebar, computeSidebarVisibility } from "./presentation/sidebar"
import { ToastContainer } from "./presentation/toast"
import { tuiTheme } from "./presentation/theme"
import { ThreadView } from "./presentation/thread"
import { WebTakeoverView } from "./presentation/web-takeover"
import { copyToClipboard } from "./platform/clipboard"
import { registerCommonSyntaxParsers, shutdownCommonSyntaxClient } from "./platform/syntax-parsers"
import { win32InstallVtInputGuard } from "./platform/terminal-win32"
import {
  tuiLocked,
  type PresentationCoordinator,
  type PresentationState,
} from "../presentation-coordinator"
import type { AgentGateway } from "../interactive/ports"
import type { WorkspaceExplorer } from "../workspace/types"
import { detectGitChangedFiles } from "../infrastructure/git-workspace"

/** 正式 TUI 的启动参数；Controller 由 CLI Composition Root 创建并注入。 */
export type TuiOptions = {
  controller: InteractiveController
  gateway?: AgentGateway
  workspaceExplorer?: WorkspaceExplorer
  resume?: boolean
  promptHistoryFile?: string
  openWeb?: (threadId: string | null) => Promise<void>
  webHandoff?: PresentationCoordinator
}

/** runTui 渲染树内部使用的完整选项：adapter 由 runTui 创建一次并注入，跨 handoff 复用。 */
export type RenderedTuiOptions = TuiOptions & { adapter: TuiAdapter; onRequestExit: () => void }

/** Harness 接管 Ctrl+C 的清空、取消与退出语义，renderer 不得自行销毁。 */
export const TUI_RENDERER_OPTIONS = {
  exitOnCtrlC: false,
  externalOutputMode: "passthrough",
  targetFps: 60,
  maxFps: 60,
  gatherStats: false,
  clearOnShutdown: true,
  useKittyKeyboard: {},
  autoFocus: false,
  openConsoleOnError: false,
  useMouse: true,
} as const satisfies CliRendererConfig

/** 根层接管切换：Web 持有输入权时卸载 TUI 渲染，归还后复用同一 Controller/Adapter。 */
export function WebAwareRoot(options: RenderedTuiOptions) {
  const coordinator = options.webHandoff
  const subscribe = useCallback(
    (listener: (state: PresentationState) => void) =>
      coordinator?.subscribe(listener) ?? noOpUnsubscribe,
    [coordinator],
  )
  const getSnapshot = useCallback(
    (): PresentationState | null => coordinator?.getSnapshot() ?? null,
    [coordinator],
  )
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  if (snapshot && tuiLocked(snapshot)) {
    return <WebTakeoverView state={snapshot} onExit={options.onRequestExit} />
  }
  return <Za38Tui {...options} />
}

function noOpUnsubscribe(): void {
  // 无 Coordinator 时订阅为空操作。
}

/** 正式 OpenTUI 根组件：所有业务状态来自 Adapter snapshot；Controller/Adapter 由宿主注入。 */
export function Za38Tui(options: RenderedTuiOptions) {
  const adapter = options.adapter
  const subscribe = useCallback((listener: (snapshot: TuiAdapterSnapshot) => void) => adapter.subscribe(listener), [adapter])
  const getSnapshot = useCallback(() => adapter.getSnapshot(), [adapter])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const interactive = snapshot.interactive

  const inputRef = useRef<TextareaRenderable | null>(null)
  const conversationScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const statusScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const skillSearchRef = useRef<TextareaRenderable | null>(null)
  const threadSearchRef = useRef<TextareaRenderable | null>(null)
  const modelSearchRef = useRef<TextareaRenderable | null>(null)
  const agentSearchRef = useRef<TextareaRenderable | null>(null)
  const renderer = useRenderer()
  const terminal = useTerminalDimensions()
  const lastScrollRequestRef = useRef(snapshot.scrollRequest)

  // Controller/Adapter 由 CLI Composition Root 统一关闭；handoff 往返不销毁实例。

  const syncInputBuffer = useCallback((draft: string, cursor: "start" | "end" | undefined) => {
    const input = inputRef.current
    if (!input || input.plainText === draft) return
    input.setText(draft)
    if (cursor === "start") input.gotoBufferHome()
    else input.gotoBufferEnd()
  }, [])

  /** OpenTUI textarea 不是受控输入框；Adapter draft 变化时同步其内部缓冲区。 */
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

  const mcpAutoRefreshedRef = useRef(false)
  useEffect(() => {
    // 首次进入聊天会话且侧边栏显示时，自动同步已在后台连接完毕的 MCP 服务器状态
    if (!isHomeState(interactive) && !mcpAutoRefreshedRef.current) {
      mcpAutoRefreshedRef.current = true
      void options.controller.dispatch({ type: "catalog.refresh", catalog: "mcp" })
    }
  }, [interactive, options.controller])

  const fileTreeRefreshedRef = useRef(false)
  useEffect(() => {
    // 首次进入聊天会话且侧边栏显示时，确保工作区文件树触发刷新
    if (!isHomeState(interactive) && !fileTreeRefreshedRef.current) {
      fileTreeRefreshedRef.current = true
      void options.workspaceExplorer?.dispatch({ type: "workspace.refresh" })
    }
  }, [interactive, options.workspaceExplorer])

  /** 将 renderer 内部选区收敛为纯复制 module，避免渲染状态进入 Adapter 或 IPC。 */
  const copySelectedText = useCallback(() => copyCurrentSelection({
    getSelectedText: () => renderer.getSelection()?.getSelectedText(),
    clearSelection: () => renderer.clearSelection(),
    writeClipboard: copyToClipboard,
    showToast: (message, variant) => adapter.showToast(message, variant),
  }), [adapter, renderer])

  /** 根层统一处理普通内容的选区复制；空选区由纯 module 无副作用地忽略。 */
  const handleSelectionMouseUp = useCallback((event: { button: number }) => {
    if (event.button !== MouseButton.LEFT && event.button !== MouseButton.RIGHT) return
    if (!shouldAttemptSelectionCopy(process.platform, { type: "mouse-up", button: event.button })) return
    const copying = copySelectedText()
    if (copying) void copying
  }, [copySelectedText])

  /** textarea 只负责光标边界和滚动 ref，历史业务交给 Adapter。 */
  const handleInputBarKeyDown = useCallback((key: KeyEvent) => {
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
    const currentSnapshot = adapter.getSnapshot()

    const atStart = input.cursorOffset === 0
    const atEnd = input.cursorOffset === input.plainText.length

    // 输入框为空时键入 ! 瞬间进入 Shell 模式（阻止 ! 写入输入框）
    if (input.plainText === "" && currentSnapshot.inputMode === "chat" && (key.sequence === "!" || key.name === "!")) {
      key.preventDefault()
      void adapter.dispatch({ type: "input-mode-change", mode: "shell" })
      return
    }

    // Shell 模式下 Esc 退出 Shell 模式
    if (currentSnapshot.inputMode === "shell" && key.name === "escape") {
      key.preventDefault()
      void adapter.dispatch({ type: "input-mode-change", mode: "chat" })
      syncInputBuffer("", undefined)
      return
    }

    // Shell 模式下当输入为空时按 Backspace 退出 Shell 模式
    if (currentSnapshot.inputMode === "shell" && key.name === "backspace" && input.plainText === "") {
      key.preventDefault()
      void adapter.dispatch({ type: "input-mode-change", mode: "chat" })
      return
    }

    // 存在草稿时 Ctrl+C 立即同步清空输入框与底层原生缓冲区
    if (key.ctrl && key.name === "c" && (input.plainText !== "" || currentSnapshot.draft !== "")) {
      if (shouldAttemptSelectionCopy(process.platform, { type: "key-down", name: key.name, ctrl: key.ctrl })) {
        const copying = copySelectedText()
        if (copying) {
          key.preventDefault()
          void copying
          return
        }
      }
      key.preventDefault()
      syncInputBuffer("", undefined)
      void adapter.dispatch({ type: "shortcut", action: "clear-draft" })
      return
    }


    if (key.name === "up" && (atStart || currentSnapshot.draftCursor === "start")) {
      void adapter.dispatch({ type: "history", direction: "previous" })
      const next = adapter.getSnapshot()
      syncInputBuffer(next.draft, next.draftCursor)
      key.preventDefault()
      return
    }
    if (key.name === "down" && (atEnd || currentSnapshot.draftCursor === "start")) {
      void adapter.dispatch({ type: "history", direction: "next" })
      const next = adapter.getSnapshot()
      syncInputBuffer(next.draft, next.draftCursor)
      key.preventDefault()
      return
    }

    if (!input.plainText && !isHomeState(interactive)) {
      const scrollAction = key.name === "up" ? "line-up" : key.name === "down" ? "line-down" : undefined
      if (scrollAction && scrollConversation(scrollAction)) key.preventDefault()
    }
  }, [adapter, snapshot.commandDialog, snapshot.commandMenu.visible, snapshot.modelBindingDialog, snapshot.models.visible, snapshot.skills.visible, snapshot.agents.visible, interactive, snapshot.threads.visible, syncInputBuffer, snapshot.btw.visible])

  /** 通过 ref 滚动当前时间线；不把终端尺寸或 OpenTUI 对象带入 Adapter。 */
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

  /** 全局快捷键只负责识别动作；具体状态转换由 Adapter 处理。 */
  useKeyboard(key => {
    if (shouldAttemptSelectionCopy(process.platform, { type: "key-down", name: key.name, ctrl: key.ctrl })) {
      const copying = copySelectedText()
      if (copying) {
        key.preventDefault()
        void copying
        return
      }
    }

    const isHome = isHomeState(interactive)
    const sidebarVisibility = computeSidebarVisibility(snapshot.sidebar, terminal.width, isHome)

    if (sidebarVisibility.visible) {
      if (key.name === "escape" || key.name === "tab") {
        key.preventDefault()
        void adapter.dispatch({ type: "sidebar-toggle", target: "hide" })
        return
      }
      if (key.sequence === "[" || key.sequence === "1") {
        key.preventDefault()
        void adapter.dispatch({ type: "sidebar-tab-switch", tab: "files" })
        return
      }
      if (key.sequence === "]" || key.sequence === "2") {
        key.preventDefault()
        void adapter.dispatch({ type: "sidebar-tab-switch", tab: "status" })
        return
      }
      if (snapshot.sidebar.activeTab === "status") {
        const statusScroll = statusScrollRef.current
        const lineDelta = key.name === "up" || key.name === "k" ? -1
          : key.name === "down" || key.name === "j" ? 1
            : undefined
        const pageDelta = key.name === "pageup" ? -1 : key.name === "pagedown" ? 1 : undefined
        if (lineDelta !== undefined || pageDelta !== undefined || key.name === "home" || key.name === "end") {
          key.preventDefault()
          if (statusScroll && !statusScroll.isDestroyed) {
            if (key.name === "home") statusScroll.scrollTo(0)
            else if (key.name === "end") statusScroll.scrollTo(statusScroll.scrollHeight)
            else if (pageDelta !== undefined) {
              statusScroll.scrollBy(pageDelta * Math.max(1, Math.floor(statusScroll.height / 2)))
            } else if (lineDelta !== undefined) {
              statusScroll.scrollBy(lineDelta)
            }
          }
          return
        }
      }
      if (snapshot.sidebar.activeTab === "files" && key.sequence === "@") {
        key.preventDefault()
        const current = snapshot.sidebar.fileTree.rows[snapshot.sidebar.fileTree.selectedIndex]
        if (current && current.kind !== "directory") {
          void adapter.dispatch({ type: "file-preview-insert-ref", path: current.path })
        }
        return
      }
      if (snapshot.sidebar.activeTab === "files" && (key.name === "up" || key.name === "k")) {
        key.preventDefault()
        void adapter.dispatch({ type: "file-tree-navigate", direction: "up" })
        return
      }
      if (snapshot.sidebar.activeTab === "files" && (key.name === "down" || key.name === "j")) {
        key.preventDefault()
        void adapter.dispatch({ type: "file-tree-navigate", direction: "down" })
        return
      }
      if (snapshot.sidebar.activeTab === "files" && (key.name === "left" || key.name === "h")) {
        key.preventDefault()
        void adapter.dispatch({ type: "file-tree-navigate", direction: "parent" })
        return
      }
      if (snapshot.sidebar.activeTab === "files" && (key.name === "right" || key.name === "l")) {
        key.preventDefault()
        void adapter.dispatch({ type: "file-tree-navigate", direction: "child" })
        return
      }
      if (snapshot.sidebar.activeTab === "files" && (key.name === "return" || key.name === "space")) {
        key.preventDefault()
        const current = snapshot.sidebar.fileTree.rows[snapshot.sidebar.fileTree.selectedIndex]
        if (current) {
          if (current.kind === "directory") {
            void adapter.dispatch({ type: "file-tree-toggle-expand", path: current.path })
          } else {
            void adapter.dispatch({ type: "file-tree-preview", path: current.path })
          }
        }
        return
      }
    }

    const action = resolveShortcut(key, {
      commandDialogVisible: Boolean(snapshot.commandDialog || snapshot.modelBindingDialog),
      btwModalVisible: snapshot.btw.visible,
      skillPickerVisible: snapshot.skills.visible,
      skillOptionCount: snapshot.skills.items.length,
      threadPickerVisible: snapshot.threads.visible,
      threadOptionCount: snapshot.threads.items.length,
      modelPickerVisible: snapshot.models.visible,
      modelOptionCount: snapshot.models.items.length,
      agentPickerVisible: snapshot.agents.visible,
      agentOptionCount: snapshot.agents.items.length,
      commandMenuVisible: snapshot.commandMenu.visible,
      commandOptionCount: snapshot.commandOptions.length,
      activeRun: Boolean(interactive.activeRun),
      hasDraft: Boolean(snapshot.draft),
      inputMode: snapshot.inputMode,
      childTimelineActive: Boolean(interactive.childTimelineExecutionId),
    })
    if (action === "none") return
    key.preventDefault()

    if (action === "clear-draft" || action === "exit-shell-mode") {
      syncInputBuffer("", undefined)
    }

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
    void adapter.dispatch({ type: "shortcut", action })
  })

  const isHome = isHomeState(interactive)
  const sidebarVisibility = computeSidebarVisibility(snapshot.sidebar, terminal.width, isHome)
  const viewProps = {
    interactive,
    transientNotice: snapshot.transientNotice,
    terminalWidth: terminal.width,
    terminalHeight: terminal.height,
    inputRef,
    conversationScrollRef,
    value: snapshot.draft,
    onInput: (value: string) => { void adapter.dispatch({ type: "draft-input", value }) },
    onInputBarKeyDown: handleInputBarKeyDown,
    onSubmit: () => { void adapter.dispatch({ type: "submit", value: inputRef.current?.plainText ?? snapshot.draft }) },
    commandMenu: snapshot.commandMenu,
    commandOptions: snapshot.commandOptions,
    onSelectCommand: (item: CommandMenuItem) => { void adapter.dispatch({ type: "command-menu-select", item }) },
    onHoverCommand: (selectedIndex: number) => { void adapter.dispatch({ type: "command-menu-hover", selectedIndex }) },
    selectedSkill: snapshot.selectedSkill,
    pickerVisible: Boolean(snapshot.commandDialog || snapshot.modelBindingDialog) || snapshot.skills.visible || snapshot.threads.visible || snapshot.models.visible || snapshot.agents.visible || snapshot.btw.visible,
    onClearSelectedSkill: () => { void adapter.dispatch({ type: "clear-selected-skill" }) },
    showToolDetails: snapshot.showToolDetails,
    expandedTools: snapshot.expandedTools,
    onToggleTool: (toolId: string) => { void adapter.dispatch({ type: "tool-toggle", toolId }) },
    onApproval: (decision: ApprovalDecision) => { void adapter.dispatch({ type: "approval", decision }) },
    onDirectoryTrust: (decision: DirectoryTrustDecision) => { void adapter.dispatch({ type: "directory-trust", decision }) },
    onQuestion: (answers: Record<string, string[]>) => { void adapter.dispatch({ type: "question", answers }) },
    onOpenChildTimeline: (executionId: string) => { void adapter.dispatch({ type: "child-timeline-open", executionId }) },
    sidebarVisible: sidebarVisibility.visible,
    inputMode: snapshot.inputMode,
    onToggleSidebar: () => {
      void adapter.dispatch({
        type: "sidebar-toggle",
        target: sidebarVisibility.visible ? "hide" : "show",
      })
    },
    modelName: displayedModelName(interactive),
  }

  return (
    <box width="100%" height="100%" flexDirection="row" onMouseUp={handleSelectionMouseUp}>
      <box flexGrow={1} height="100%" flexDirection="column">
        {isHome ? <HomeView {...viewProps} /> : <ThreadView {...viewProps} />}
      </box>
      {sidebarVisibility.visible ? (
        <Sidebar
          sidebar={snapshot.sidebar}
          interactive={interactive}
          terminalWidth={terminal.width}
          terminalHeight={terminal.height}
          isHome={isHome}
          onToggle={() => { void adapter.dispatch({ type: "sidebar-toggle", target: "hide" }) }}
          onSwitchTab={tab => { void adapter.dispatch({ type: "sidebar-tab-switch", tab }) }}
          onSelectFileTreeNode={index => { void adapter.dispatch({ type: "file-tree-select", index }) }}
          onToggleFileTreeExpand={path => { void adapter.dispatch({ type: "file-tree-toggle-expand", path }) }}
          onOpenFile={path => { void adapter.dispatch({ type: "file-tree-preview", path }) }}
          onInsertRef={path => { void adapter.dispatch({ type: "file-preview-insert-ref", path }) }}
          onClosePreview={() => { void adapter.dispatch({ type: "file-preview-close" }) }}
          onSelectionMouseUp={handleSelectionMouseUp}
          statusScrollRef={statusScrollRef}
        />
      ) : null}
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
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}
        onSearch={query => { void adapter.dispatch({ type: "picker-search", picker: "skills", query }) }}
        onSelect={skill => { void adapter.dispatch({ type: "picker-select-skill", skill }) }}
        onHover={selectedIndex => { void adapter.dispatch({ type: "picker-hover", picker: "skills", selectedIndex }) }}
        onClose={() => { void adapter.dispatch({ type: "picker-close", picker: "skills" }) }}
        workMode={interactive.workMode}
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
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}
        onSearch={query => { void adapter.dispatch({ type: "picker-search", picker: "threads", query }) }}
        onSelect={thread => { void adapter.dispatch({ type: "picker-select-thread", thread }) }}
        onHover={selectedIndex => { void adapter.dispatch({ type: "picker-hover", picker: "threads", selectedIndex }) }}
        onClose={() => { void adapter.dispatch({ type: "picker-close", picker: "threads" }) }}
        workMode={interactive.workMode}
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
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}        searchId="model-search"
        title={interactive.currentThreadId ? "选择当前 Thread 下一次运行的模型" : "选择下一次新 Thread 运行的模型"}
        searchPlaceholder="按 Profile、模型或 Provider 搜索"
        emptyMessage="没有匹配的模型 Profile"
        loadingMessage={snapshot.models.syncingDefault ? "正在同步后续新 Thread 默认模型…" : undefined}
        footer="选择后同时更新后续新 Thread 默认模型"
        itemKey={model => model.id}
        renderItem={(model, context) => modelPickerRow(model, context)}
        onSearch={query => { void adapter.dispatch({ type: "picker-search", picker: "models", query }) }}
        onSelect={model => { void adapter.dispatch({ type: "picker-select-model", model }) }}
        onHover={selectedIndex => { void adapter.dispatch({ type: "picker-hover", picker: "models", selectedIndex }) }}
        onClose={() => { void adapter.dispatch({ type: "picker-close", picker: "models" }) }}
        workMode={interactive.workMode}
      />
      <AgentPicker
        visible={snapshot.agents.visible}
        loading={snapshot.agents.loading}
        error={snapshot.agents.error}
        agents={snapshot.agents.items}
        query={snapshot.agents.query}
        selectedIndex={snapshot.agents.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={agentSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}
        onSearch={query => { void adapter.dispatch({ type: "picker-search", picker: "agents", query }) }}
        onSelect={() => { void adapter.dispatch({ type: "picker-close", picker: "agents" }) }}
        onHover={selectedIndex => { void adapter.dispatch({ type: "picker-hover", picker: "agents", selectedIndex }) }}
        onClose={() => { void adapter.dispatch({ type: "picker-close", picker: "agents" }) }}
        workMode={interactive.workMode}
      />
      <DialogShell
        visible={snapshot.commandDialog?.kind === "confirm-new-thread"}
        title={snapshot.commandDialog?.title ?? ""}
        message={snapshot.commandDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}
        onConfirm={() => { void adapter.dispatch({ type: "dialog-resolve", kind: "command", confirmed: true }) }}
        onCancel={() => { void adapter.dispatch({ type: "dialog-resolve", kind: "command", confirmed: false }) }}      />
      <DialogShell
        visible={Boolean(snapshot.modelBindingDialog)}
        title={snapshot.modelBindingDialog?.title ?? ""}
        message={snapshot.modelBindingDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!interactive.activeRun && interactive.activity.kind !== "compacting"}        confirmLabel="新建 Thread"
        cancelLabel="保留当前 Thread"
        onConfirm={() => { void adapter.dispatch({ type: "dialog-resolve", kind: "model-binding", confirmed: true }) }}
        onCancel={() => { void adapter.dispatch({ type: "dialog-resolve", kind: "model-binding", confirmed: false }) }}
      />
      <BtwModal
        visible={snapshot.btw.visible}
        question={snapshot.btw.question}
        answer={snapshot.btw.answer}
        modelProfileId={snapshot.btw.modelProfileId}
        status={snapshot.btw.status}
        error={snapshot.btw.error}
        copied={snapshot.btw.copied}
        workMode={interactive.workMode}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        onClose={() => { void adapter.dispatch({ type: "btw-close" }) }}
        onCopy={() => { void adapter.dispatch({ type: "btw-copy" }) }}
      />
      <ToastContainer toasts={snapshot.toasts} terminalWidth={terminal.width} />
    </box>
  )
}

/** 从共享 snapshot 派生当前展示模型名；实际绑定优先于下一次运行的选择。 */
function displayedModelName(snapshot: InteractiveSnapshot): string | undefined {
  return snapshot.selection.actualModel?.model ?? snapshot.selection.requestedModelProfileId ?? undefined
}

/** 创建 OpenTUI renderer、挂载错误边界；退出时将控制权交回 CLI 关闭 Python sidecar。 */
export async function runTui(options: TuiOptions): Promise<void> {
  registerCommonSyntaxParsers()
  const gitWorkspace = options.controller.getSnapshot().runtime.gitWorkspace
  const gitRoot = gitWorkspace?.kind === "branch" || gitWorkspace?.kind === "detached"
    ? gitWorkspace.root
    : undefined
  const renderer = await createCliRenderer(TUI_RENDERER_OPTIONS)
  const uninstallVtGuard = win32InstallVtInputGuard()
  const root = createRoot(renderer)
  await new Promise<void>(resolve => {
    let closed = false
    let adapter: TuiAdapter
    let unregisterExit: (() => void) | undefined
    const close = () => {
      if (closed) return
      closed = true
      unregisterExit?.()
      uninstallVtGuard?.()
      root.unmount()
      void adapter.close()
      void shutdownCommonSyntaxClient().finally(() => {
        renderer.destroy()
        resolve()
      })
    }
    // Adapter 在 CLI 层创建一次：handoff 往返复用同一实例，本地表现状态跨会话保留。
    // 快捷键、Slash Command、错误边界与 Web 退出必须共享此处唯一关闭路径。
    adapter = createTuiAdapter({
      controller: options.controller,
      gateway: options.gateway,
      workspaceExplorer: options.workspaceExplorer,
      promptHistoryFile: options.promptHistoryFile,
      resume: options.resume,
      onRequestExit: close,
      openWeb: options.openWeb,
      workspaceChangeProbe: gitRoot ? () => detectGitChangedFiles(gitRoot) : undefined,
      dispatchGate: options.webHandoff ? (intent) => options.webHandoff!.tuiDispatch(intent) : undefined,
    })
    unregisterExit = options.webHandoff?.registerExitHandler(close)
    const renderedOptions: RenderedTuiOptions = { ...options, adapter, onRequestExit: close }
    root.render(
      <TuiErrorBoundary onRequestExit={close}>
        <WebAwareRoot {...renderedOptions} />
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
