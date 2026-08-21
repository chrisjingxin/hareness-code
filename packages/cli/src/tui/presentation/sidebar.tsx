import type { ScrollBoxRenderable } from "@opentui/core"
import type { ReactNode, RefObject } from "react"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { SidebarState } from "../application/adapter"
import type { WorkspacePreviewState } from "../../workspace/types"
import { ContextWidget } from "./sidebar/context-widget"
import { CwdWidget } from "./sidebar/cwd-widget"
import { FileTreeWidget, fileTypeBadge } from "./sidebar/file-tree-widget"
import { McpWidget } from "./sidebar/mcp-widget"
import { createScrollAcceleration } from "./scroll"
import { markdownSyntax, tuiTheme } from "./theme"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"

export const SIDEBAR_BREAKPOINT_WIDTH = 116
export const SIDEBAR_FILE_COLUMNS_BREAKPOINT_WIDTH = 150
const SIDEBAR_FULL_WIDTH_BREAKPOINT = 72
const WORKSPACE_CHANGE_RENDER_LIMIT = 200

export type SidebarLayout = {
  /** 中窄终端是否覆盖主区；宽屏直接进入根布局并压缩对话宽度。 */
  isOverlay: boolean
  sidebarWidth: number
  compactHeight: boolean
  fileTreeHeight: number
  filePaneDirection: "rows" | "columns"
}

/** 根据终端宽高计算检查器宽度与文件树/预览的纵向分配。 */
export function computeSidebarLayout(terminalWidth: number, terminalHeight: number): SidebarLayout {
  const compactHeight = terminalHeight < 28
  const fileContentHeight = Math.max(12, terminalHeight - 5)
  const fileTreeHeight = Math.max(compactHeight ? 7 : 10, Math.floor(fileContentHeight * 0.35))
  const filePaneDirection = terminalWidth >= SIDEBAR_FILE_COLUMNS_BREAKPOINT_WIDTH
    ? "columns"
    : "rows"

  if (terminalWidth <= SIDEBAR_FULL_WIDTH_BREAKPOINT) {
    return {
      isOverlay: true,
      sidebarWidth: terminalWidth,
      compactHeight,
      fileTreeHeight,
      filePaneDirection,
    }
  }

  if (terminalWidth < SIDEBAR_BREAKPOINT_WIDTH) {
    return {
      isOverlay: true,
      sidebarWidth: Math.min(56, Math.max(42, Math.floor(terminalWidth * 0.55))),
      compactHeight,
      fileTreeHeight,
      filePaneDirection,
    }
  }

  return {
    isOverlay: false,
    sidebarWidth: filePaneDirection === "columns"
      ? Math.max(76, Math.min(104, Math.floor(terminalWidth * 0.5)))
      : Math.max(52, Math.min(76, Math.floor(terminalWidth * 0.42))),
    compactHeight,
    fileTreeHeight,
    filePaneDirection,
  }
}

export type SidebarVisibility = {
  /** 侧边栏抽屉是否可见 */
  visible: boolean
  /** 是否覆盖主区；宽屏停靠时为 false。 */
  isOverlay: boolean
  /** 侧边栏主体宽度 */
  sidebarWidth: number
}

/** 计算侧边栏在当前终端尺寸与配置下的可见性。首页不展示侧边栏，仅在聊天/会话页面展示。 */
export function computeSidebarVisibility(
  state: SidebarState,
  terminalWidth: number,
  isHome = false,
): SidebarVisibility {
  const layout = computeSidebarLayout(terminalWidth, 40)
  if (isHome || state.mode === "hide") {
    return { visible: false, isOverlay: layout.isOverlay, sidebarWidth: 0 }
  }

  if (state.mode === "show" || state.drawerOpen) {
    return { visible: true, isOverlay: layout.isOverlay, sidebarWidth: layout.sidebarWidth }
  }

  return { visible: false, isOverlay: layout.isOverlay, sidebarWidth: 0 }
}

function formatSize(bytes?: number): string {
  if (!bytes) return "0 B"
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export type CodePreviewPaneProps = {
  preview: WorkspacePreviewState
  width: number
  height: number
  direction?: "rows" | "columns"
  onInsertRef?: (path: string) => void
  onClose?: () => void
}

/** 文件树旁边或下方的代码实时预览视口。 */
export function CodePreviewPane(props: CodePreviewPaneProps) {
  const { preview, width, height } = props
  if (preview.status === "idle") return null

  const filePath = preview.status === "ready" ? preview.file.path : preview.path
  const fileName = filePath.split("/").pop() ?? filePath
  const filetype = preview.status === "ready"
    ? (preview.file.language === "plaintext" ? undefined : (preview.file.language ?? undefined))
    : undefined
  const badge = fileTypeBadge(fileName, "file")
  const showFullMeta = width >= 58 && height >= 16
  const showFooter = height >= 13

  return (
    <box
      width={width}
      height={height}
      backgroundColor={tuiTheme.background}
      border={[props.direction === "columns" ? "left" : "top"]}
      borderColor={tuiTheme.border}
      paddingLeft={1}
      paddingRight={1}
      flexDirection="column"
    >
      {/* 头部元信息栏 */}
      <box
        flexDirection="row"
        justifyContent="space-between"
        alignItems="center"
        flexShrink={0}
        paddingBottom={1}
        border={["bottom"]}
        borderColor={tuiTheme.border}
      >
        <box flexDirection="row" gap={1} alignItems="center" flexShrink={1}>
          <text>
            {badge ? <span fg={tuiTheme.primary}>[{badge}] </span> : null}
            <span fg={tuiTheme.text}><b>{fileName}</b></span>
          </text>
          {preview.status === "ready" && showFullMeta ? (
            <text fg={tuiTheme.muted}>
              ({preview.file.language || "text"} · {preview.file.lineCount} 行 · {formatSize(preview.file.sizeBytes)}
              {preview.file.truncated ? " · 已截断" : ""})
            </text>
          ) : null}
        </box>
        <box flexDirection="row" gap={1} alignItems="center" flexShrink={0}>
          <box onMouseUp={() => props.onInsertRef?.(filePath)}>
            <text fg={tuiTheme.primary}><b>@ 引用</b></text>
          </box>
          <box onMouseUp={props.onClose}>
            <text fg={tuiTheme.muted}>x</text>
          </box>
        </box>
      </box>

      {/* 代码内容视口 */}
      <box flexGrow={1} minHeight={0} flexDirection="column" paddingTop={1}>
        {preview.status === "loading" ? (
          <box padding={height < 14 ? 0 : 2} justifyContent="center" alignItems="center">
            <text fg={tuiTheme.muted}>正在读取文件内容…</text>
          </box>
        ) : preview.status === "error" ? (
          <box padding={2} flexDirection="column">
            <text fg={tuiTheme.danger}>
              <b>读取失败：</b>{preview.message}
            </text>
          </box>
        ) : preview.status === "unsupported" ? (
          <box padding={2} flexDirection="column">
            <text fg={tuiTheme.warning}>
              <b>暂无法文本预览：</b>
            </text>
            <text fg={tuiTheme.muted}>
              {preview.reason || `该文件为二进制或大于 1MB（${formatSize(preview.sizeBytes)}），无法在终端直接预览。`}
            </text>
          </box>
        ) : preview.status === "ready" ? (
          <scrollbox flexGrow={1} contentOptions={{ flexDirection: "column" }}>
            <line-number fg={tuiTheme.muted} minWidth={3} paddingRight={1}>
              <code
                content={preview.file.content}
                filetype={filetype}
                syntaxStyle={markdownSyntax}
                treeSitterClient={getCommonSyntaxClient()}
                conceal={false}
                fg={tuiTheme.text}
              />
            </line-number>
          </scrollbox>
        ) : null}
      </box>

      {/* 底部快捷操作提示 */}
      {showFooter ? (
        <box
          flexDirection="row"
          justifyContent={showFullMeta ? "space-between" : "flex-end"}
          flexShrink={0}
          paddingTop={1}
          border={["top"]}
          borderColor={tuiTheme.border}
        >
          {showFullMeta ? (
            <text fg={tuiTheme.subtle}>↑/↓ 切换 · ←/→ 折叠</text>
          ) : null}
          <text fg={tuiTheme.muted}>@ 引用 · Esc 关闭侧栏</text>
        </box>
      ) : null}
    </box>
  )
}

export type SidebarProps = {
  sidebar: SidebarState
  interactive?: InteractiveSnapshot
  terminalWidth: number
  terminalHeight: number
  isHome?: boolean
  onToggle: () => void
  onSwitchTab?: (tab: "files" | "status") => void
  onSelectFile?: (path: string) => void
  onSelectFileTreeNode?: (index: number) => void
  onToggleFileTreeExpand?: (path: string) => void
  onOpenFile?: (path: string) => void
  onInsertRef?: (path: string) => void
  onClosePreview?: () => void
  onSelectionMouseUp?: (event: { button: number }) => void
  /** 状态页滚动视口；正常高度指向变更列表，紧凑高度指向整个状态页。 */
  statusScrollRef?: RefObject<ScrollBoxRenderable | null>
  children?: ReactNode
}

/** 侧边栏主体容器：宽屏停靠；中窄屏覆盖。文件树与代码预览按宽度横向或纵向组合。 */
export function Sidebar(props: SidebarProps) {
  const { visible } = computeSidebarVisibility(
    props.sidebar,
    props.terminalWidth,
    props.isHome,
  )

  if (!visible) return null

  const layout = computeSidebarLayout(props.terminalWidth, props.terminalHeight)
  const isSidebarFocused = props.sidebar.focus === "sidebar"
  const activeTab = props.sidebar.activeTab ?? "files"
  const panelHorizontalInset = layout.sidebarWidth >= 52 ? 5 : 3
  const fileContentWidth = Math.max(1, layout.sidebarWidth - panelHorizontalInset)
  const fileContentHeight = Math.max(6, props.terminalHeight - 5)
  const fileTreeColumnWidth = Math.max(24, Math.floor(fileContentWidth * 0.4))
  const filePreviewColumnWidth = Math.max(1, fileContentWidth - fileTreeColumnWidth)
  const workspaceChangePathWidth = Math.max(12, layout.sidebarWidth - 20)
  const compactStatusSummaryHeight = Math.max(7, Math.floor((props.terminalHeight - 5) * 0.5))
  const gitWorkspace = props.interactive?.runtime.gitWorkspace
  const workspaceChangedFiles = props.sidebar.workspaceChangedFiles
  const showWorkspaceChanges = (
    gitWorkspace?.kind === "branch" || gitWorkspace?.kind === "detached"
  ) && workspaceChangedFiles !== undefined
  const workspaceChangeRows = workspaceChangedFiles?.slice(0, WORKSPACE_CHANGE_RENDER_LIMIT).map(file => (
    <box key={`${file.status}:${file.path}`} height={1} flexShrink={0} flexDirection="row" justifyContent="space-between">
      <text width={workspaceChangePathWidth} fg={tuiTheme.text} wrapMode="none" overflow="hidden">{file.path}</text>
      <box flexShrink={0} flexDirection="row" gap={1}>
        <text fg={file.addedLines && file.addedLines > 0 ? tuiTheme.diffAdd : tuiTheme.muted}>
          +{file.addedLines ?? "–"}
        </text>
        <text fg={file.removedLines && file.removedLines > 0 ? tuiTheme.diffRemove : tuiTheme.muted}>
          -{file.removedLines ?? "–"}
        </text>
      </box>
    </box>
  ))
  const workspaceChangeContent = (
    <>
      {workspaceChangeRows}
      {workspaceChangedFiles && workspaceChangedFiles.length > WORKSPACE_CHANGE_RENDER_LIMIT ? (
        <text fg={tuiTheme.subtle}>+{workspaceChangedFiles.length - WORKSPACE_CHANGE_RENDER_LIMIT} 更多文件…</text>
      ) : null}
    </>
  )
  const renderWorkspaceChanges = (scrollable: boolean) => showWorkspaceChanges ? (
    <box flexGrow={scrollable ? 1 : 0} minHeight={scrollable ? 5 : undefined} flexDirection="column" paddingTop={1}>
      <box height={1} flexShrink={0} flexDirection="row" justifyContent="space-between">
        <text fg={tuiTheme.primary}><b>工作区变更</b></text>
        <text fg={tuiTheme.muted}>{workspaceChangedFiles?.length ?? 0} 个文件</text>
      </box>
      {workspaceChangedFiles?.length === 0 ? (
        <text fg={tuiTheme.subtle}>工作区干净</text>
      ) : scrollable ? (
        <scrollbox
          ref={props.statusScrollRef}
          flexGrow={1}
          minHeight={0}
          contentOptions={{ flexDirection: "column" }}
          paddingTop={1}
          scrollAcceleration={createScrollAcceleration()}
          viewportOptions={{ paddingRight: 1 }}
        >
          {workspaceChangeContent}
        </scrollbox>
      ) : (
        <box flexDirection="column" paddingTop={1}>
          {workspaceChangeContent}
        </box>
      )}
    </box>
  ) : null

  // 宽终端让预览成为文件树右侧独立列；较窄终端改在下方，避免横向溢出。
  const hasPreview = Boolean(
    props.sidebar.preview &&
    props.sidebar.preview.status !== "idle"
  )
  const panel = (
    <box
      key="sidebar-panel"
      width={layout.sidebarWidth}
      height="100%"
      backgroundColor={tuiTheme.background}
      border={["left"]}
      borderColor={isSidebarFocused ? tuiTheme.borderActive : tuiTheme.border}
      paddingLeft={layout.sidebarWidth >= 52 ? 2 : 1}
      paddingRight={layout.sidebarWidth >= 52 ? 2 : 1}
      flexDirection="column"
      onMouseUp={event => {
        props.onSelectionMouseUp?.(event)
        event.stopPropagation?.()
      }}
    >
      <box flexDirection="column" flexShrink={0} border={["bottom"]} borderColor={tuiTheme.border}>
        <box
          flexDirection="row"
          justifyContent="space-between"
          alignItems="center"
          paddingTop={1}
        >
          <text fg={tuiTheme.pickerActive}><b>项目检查器</b></text>
          <box onMouseUp={props.onToggle} paddingLeft={1}>
            <text fg={tuiTheme.muted}>x</text>
          </box>
        </box>
        <box flexDirection="row" gap={3} paddingTop={1}>
          <box
            border={activeTab === "files" ? ["bottom"] : undefined}
            borderColor={activeTab === "files" ? tuiTheme.pickerActive : undefined}
            onMouseUp={() => props.onSwitchTab?.("files")}
          >
            <text fg={activeTab === "files" ? tuiTheme.pickerActive : tuiTheme.muted}>
              {activeTab === "files" ? <b>文件</b> : "文件"}
            </text>
          </box>
          <box
            border={activeTab === "status" ? ["bottom"] : undefined}
            borderColor={activeTab === "status" ? tuiTheme.pickerActive : undefined}
            onMouseUp={() => props.onSwitchTab?.("status")}
          >
            <text fg={activeTab === "status" ? tuiTheme.pickerActive : tuiTheme.muted}>
              {activeTab === "status" ? <b>状态</b> : "状态"}
            </text>
          </box>
        </box>
      </box>

      {props.children ?? (
        <box flexGrow={1} flexDirection="column" minHeight={0} paddingTop={1}>
          {props.interactive ? (
            activeTab === "files" ? (
              hasPreview && props.sidebar.preview && layout.filePaneDirection === "columns" ? (
                <box flexGrow={1} minHeight={0} flexDirection="row">
                  <box width={fileTreeColumnWidth} minHeight={0} flexDirection="column">
                    <FileTreeWidget
                      fileTree={props.sidebar.fileTree}
                      focused={isSidebarFocused}
                      onSelectIndex={props.onSelectFileTreeNode ?? (() => {})}
                      onToggleExpand={props.onToggleFileTreeExpand ?? (() => {})}
                      onOpenFile={props.onOpenFile ?? props.onSelectFile}
                    />
                  </box>
                  <box flexGrow={1} minWidth={0} minHeight={0} flexDirection="column">
                    <CodePreviewPane
                      preview={props.sidebar.preview}
                      width={filePreviewColumnWidth}
                      height={fileContentHeight}
                      direction="columns"
                      onInsertRef={props.onInsertRef}
                      onClose={props.onClosePreview}
                    />
                  </box>
                </box>
              ) : (
                <>
                  <box
                    height={hasPreview ? layout.fileTreeHeight : "100%"}
                    minHeight={0}
                    flexDirection="column"
                  >
                    <FileTreeWidget
                      fileTree={props.sidebar.fileTree}
                      focused={isSidebarFocused}
                      onSelectIndex={props.onSelectFileTreeNode ?? (() => {})}
                      onToggleExpand={props.onToggleFileTreeExpand ?? (() => {})}
                      onOpenFile={props.onOpenFile ?? props.onSelectFile}
                    />
                  </box>
                  {hasPreview && props.sidebar.preview ? (
                    <box flexGrow={1} minHeight={0} flexDirection="column">
                      <CodePreviewPane
                        preview={props.sidebar.preview}
                        width={fileContentWidth}
                        height={Math.max(6, fileContentHeight - layout.fileTreeHeight)}
                        direction="rows"
                        onInsertRef={props.onInsertRef}
                        onClose={props.onClosePreview}
                      />
                    </box>
                  ) : null}
                </>
              )
            ) : layout.compactHeight ? (
              <box flexGrow={1} minHeight={0} flexDirection="column">
                <scrollbox
                  height={compactStatusSummaryHeight}
                  flexShrink={0}
                  contentOptions={{ flexDirection: "column" }}
                  scrollAcceleration={createScrollAcceleration()}
                  viewportOptions={{ paddingRight: 1 }}
                >
                  <CwdWidget
                    workspace={props.interactive.runtime.workspace}
                    gitWorkspace={props.interactive.runtime.gitWorkspace}
                  />
                  <ContextWidget interactive={props.interactive} />
                  <McpWidget mcp={props.interactive.catalogs.mcp} />
                </scrollbox>
                {renderWorkspaceChanges(true)}
              </box>
            ) : (
              <box flexGrow={1} minHeight={0} flexDirection="column">
                <box flexShrink={0} flexDirection="column">
                  <CwdWidget
                    workspace={props.interactive.runtime.workspace}
                    gitWorkspace={props.interactive.runtime.gitWorkspace}
                  />
                </box>
                <box flexShrink={0} flexDirection="column">
                  <ContextWidget interactive={props.interactive} />
                </box>
                <box flexShrink={0} flexDirection="column">
                  <McpWidget mcp={props.interactive.catalogs.mcp} />
                </box>
                {renderWorkspaceChanges(true)}
              </box>
            )
          ) : (
            <box flexGrow={1} justifyContent="center" alignItems="center">
              <text fg={tuiTheme.muted}>侧边栏内容就绪中…</text>
            </box>
          )}
        </box>
      )}
    </box>
  )

  if (!layout.isOverlay) return panel

  return (
    <box
      key="sidebar-overlay"
      position="absolute"
      top={0}
      left={0}
      right={0}
      bottom={0}
      flexDirection="row"
      justifyContent="flex-end"
      zIndex={80}
      onMouseUp={props.onToggle}
    >
      {panel}
    </box>
  )
}
