import type { ReactNode } from "react"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { SidebarState } from "../application/adapter"
import type { WorkspacePreviewState } from "../../workspace/types"
import { ContextWidget } from "./sidebar/context-widget"
import { CwdWidget } from "./sidebar/cwd-widget"
import { FileTreeWidget } from "./sidebar/file-tree-widget"
import { McpWidget } from "./sidebar/mcp-widget"
import { ModifiedFilesWidget } from "./sidebar/modified-files-widget"
import { OverlayBackdrop } from "./overlays"
import { markdownSyntax, tuiTheme } from "./theme"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"

export const DEFAULT_SIDEBAR_WIDTH = 38

export type SidebarVisibility = {
  /** 侧边栏抽屉是否可见 */
  visible: boolean
  /** 是否为全屏遮罩模式（本架构下统一为 true） */
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
  if (isHome || state.mode === "hide") {
    return { visible: false, isOverlay: true, sidebarWidth: 0 }
  }

  if (state.mode === "show" || state.drawerOpen) {
    return { visible: true, isOverlay: true, sidebarWidth: DEFAULT_SIDEBAR_WIDTH }
  }

  return { visible: false, isOverlay: true, sidebarWidth: 0 }
}

function formatSize(bytes?: number): string {
  if (!bytes) return "0 B"
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

import { getFileIconInfo } from "./sidebar/file-icons"

export type CodePreviewPaneProps = {
  preview: WorkspacePreviewState
  width: number
  height: number
  onInsertRef?: (path: string) => void
  onClose?: () => void
}

/** 右侧代码实时预览面板（Master-Detail 结构中的 Detail 视口）。 */
export function CodePreviewPane(props: CodePreviewPaneProps) {
  const { preview, width } = props
  if (preview.status === "idle") return null

  const filePath = preview.status === "ready" ? preview.file.path : preview.path
  const fileName = filePath.split("/").pop() ?? filePath
  const filetype = preview.status === "ready"
    ? (preview.file.language === "plaintext" ? undefined : (preview.file.language ?? undefined))
    : undefined
  const iconInfo = getFileIconInfo(fileName, "file")

  return (
    <box
      width={width}
      height="100%"
      backgroundColor={tuiTheme.background}
      borderStyle="single"
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
            <span fg={iconInfo.color}>{iconInfo.icon}</span>
            <span fg={tuiTheme.text}><b>{fileName}</b></span>
          </text>
          {preview.status === "ready" ? (
            <text fg={tuiTheme.muted}>
              ({preview.file.language || "text"} · {preview.file.lineCount} 行 · {formatSize(preview.file.sizeBytes)}
              {preview.file.truncated ? " · 已截断" : ""})
            </text>
          ) : null}
        </box>
        <box flexDirection="row" gap={1} alignItems="center" flexShrink={0}>
          <box onMouseUp={() => props.onInsertRef?.(filePath)}>
            <text fg={tuiTheme.primary}><b>[@ 引用路径]</b></text>
          </box>
          <box onMouseUp={props.onClose}>
            <text fg={tuiTheme.muted}>✕</text>
          </box>
        </box>
      </box>

      {/* 代码内容视口 */}
      <box flexGrow={1} minHeight={0} flexDirection="column" paddingTop={1}>
        {preview.status === "loading" ? (
          <box padding={2} justifyContent="center" alignItems="center">
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
              <b>⚠️ 暂无法文本预览：</b>
            </text>
            <text fg={tuiTheme.muted}>
              {preview.reason || `该文件为二进制或大于 1MB（${formatSize(preview.sizeBytes)}），无法在终端直接预览。`}
            </text>
          </box>
        ) : preview.status === "ready" ? (
          <scrollbox flexGrow={1} flexDirection="column">
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
      <box
        flexDirection="row"
        justifyContent="space-between"
        flexShrink={0}
        paddingTop={1}
        border={["top"]}
        borderColor={tuiTheme.border}
      >
        <text fg={tuiTheme.subtle}>[↑/↓] 切换文件  [←/→] 折叠目录</text>
        <text fg={tuiTheme.muted}>[@] 填入输入框  [Esc] 关闭</text>
      </box>
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
  children?: ReactNode
}

/** 侧边栏主体容器：全屏半透明遮罩 + 右靠齐主从双栏抽屉（文件树/状态 + 实时代码预览）。 */
export function Sidebar(props: SidebarProps) {
  const { visible } = computeSidebarVisibility(
    props.sidebar,
    props.terminalWidth,
    props.isHome,
  )

  if (!visible) return null

  const isSidebarFocused = props.sidebar.focus === "sidebar"
  const activeTab = props.sidebar.activeTab ?? "files"

  // 决定是否展开右侧实时代码预览面板
  const hasPreview = Boolean(
    props.sidebar.preview &&
    props.sidebar.preview.status !== "idle" &&
    props.terminalWidth >= 90
  )
  const masterWidth = DEFAULT_SIDEBAR_WIDTH
  const previewWidth = Math.max(45, Math.min(props.terminalWidth - masterWidth - 4, 85))

  return (
    <box
      position="absolute"
      top={0}
      left={0}
      right={0}
      bottom={0}
      flexDirection="row"
      justifyContent="flex-end"
      onMouseUp={props.onToggle}
    >
      <OverlayBackdrop />
      <box
        flexDirection="row"
        height="100%"
        onMouseUp={e => {
          props.onSelectionMouseUp?.(e)
          e.stopPropagation?.()
        }}
      >
        {/* 左侧 Master 抽屉：Tab 切换与文件树/状态 */}
        <box
          width={masterWidth}
          height="100%"
          backgroundColor={tuiTheme.surface}
          borderStyle="single"
          borderColor={isSidebarFocused ? tuiTheme.primary : tuiTheme.border}
          paddingLeft={1}
          paddingRight={1}
          flexDirection="column"
        >
          {/* 顶部 Tab 切换条与独立关闭按钮 */}
          <box
            flexDirection="row"
            justifyContent="space-between"
            alignItems="center"
            flexShrink={0}
            paddingBottom={1}
            border={["bottom"]}
            borderColor={tuiTheme.border}
          >
            {/* 左侧分段 Tab 切换 */}
            <box flexDirection="row" gap={1} alignItems="center">
              <box
                backgroundColor={activeTab === "files" ? tuiTheme.surfaceElevated : undefined}
                paddingLeft={1}
                paddingRight={1}
                onMouseUp={() => props.onSwitchTab?.("files")}
              >
                <text fg={activeTab === "files" ? tuiTheme.primary : tuiTheme.muted}>
                  {activeTab === "files" ? <b>📁 文件树</b> : "📁 文件树"}
                </text>
              </box>
              <box
                backgroundColor={activeTab === "status" ? tuiTheme.surfaceElevated : undefined}
                paddingLeft={1}
                paddingRight={1}
                onMouseUp={() => props.onSwitchTab?.("status")}
              >
                <text fg={activeTab === "status" ? tuiTheme.primary : tuiTheme.muted}>
                  {activeTab === "status" ? <b>⚡ 状态</b> : "⚡ 状态"}
                </text>
              </box>
            </box>

            {/* 右侧独立关闭按钮 */}
            <box onMouseUp={props.onToggle} paddingLeft={1} paddingRight={1}>
              <text fg={tuiTheme.muted}>✕</text>
            </box>
          </box>

          {/* 主内容区域：按 Tab 分流 */}
          {props.children ?? (
            <box flexGrow={1} flexDirection="column" minHeight={0} paddingTop={1}>
              {props.interactive ? (
                activeTab === "files" ? (
                  <FileTreeWidget
                    fileTree={props.sidebar.fileTree}
                    focused={isSidebarFocused}
                    onSelectIndex={props.onSelectFileTreeNode ?? (() => {})}
                    onToggleExpand={props.onToggleFileTreeExpand ?? (() => {})}
                    onOpenFile={props.onOpenFile ?? props.onSelectFile}
                  />
                ) : (
                  <scrollbox flexGrow={1} flexDirection="column">
                    <CwdWidget
                      workspace={props.interactive.runtime.workspace}
                      gitWorkspace={props.interactive.runtime.gitWorkspace}
                    />
                    <ContextWidget interactive={props.interactive} />
                    <McpWidget mcp={props.interactive.catalogs.mcp} />
                    <ModifiedFilesWidget
                      timeline={props.interactive.timeline}
                      onSelectFile={props.onSelectFile}
                    />
                  </scrollbox>
                )
              ) : (
                <box flexGrow={1} justifyContent="center" alignItems="center">
                  <text fg={tuiTheme.muted}>侧边栏内容就绪中…</text>
                </box>
              )}
            </box>
          )}
        </box>

        {/* 右侧 Detail 栏：代码实时预览面板 */}
        {hasPreview && props.sidebar.preview ? (
          <CodePreviewPane
            preview={props.sidebar.preview}
            width={previewWidth}
            height={props.terminalHeight}
            onInsertRef={props.onInsertRef}
            onClose={props.onClosePreview}
          />
        ) : null}
      </box>
    </box>
  )
}
