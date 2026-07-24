/** OpenTUI 浮层基础组件：供 Picker 与简单确认 Dialog 复用一致的尺寸、遮罩和焦点行为。 */

import { RGBA, type OptimizedBuffer, type TextareaRenderable } from "@opentui/core"
import { useEffect, useRef, type ReactNode, type RefObject } from "react"

import { tuiTheme } from "./theme"

/** SearchPicker 行渲染器可使用的稳定布局数据，未来 Model Picker 不需要重写终端尺寸逻辑。 */
export type SearchPickerRenderContext = {
  compact: boolean
  width: number
  selected: boolean
}

/** 可复用搜索选择器的最小输入契约；领域状态、过滤和键盘导航仍由调用方管理。 */
export type SearchPickerProps<T> = {
  visible: boolean
  loading: boolean
  error?: string
  items: readonly T[]
  query: string
  selectedIndex: number
  terminalWidth: number
  terminalHeight: number
  searchRef: RefObject<TextareaRenderable | null>
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  searchId: string
  title: string
  searchPlaceholder: string
  emptyMessage: string
  loadingMessage?: string
  itemKey: (item: T) => string
  renderItem: (item: T, context: SearchPickerRenderContext) => ReactNode
  onSearch: (query: string) => void
  onSelect: (item: T) => void
  onHover: (index: number) => void
  onClose: () => void
}

/** 最小确认 Dialog：统一遮罩、尺寸、按钮鼠标回调，键盘确认和取消由全局 Shortcut 绑定。 */
export type DialogShellProps = {
  visible: boolean
  title: string
  message?: string
  terminalWidth: number
  terminalHeight: number
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  confirmLabel?: string
  cancelLabel?: string
  onConfirm?: () => void
  onCancel: () => void
  children?: ReactNode
}

/**
 * 统一渲染可搜索的单列 Picker。Enter 通过 textarea 的 submit 选择当前行，Esc 则由
 * 调用方已有的全局 Shortcut 转发到 onClose，避免每个领域 Picker 再维护自己的输入生命周期。
 */
export function SearchPicker<T>(props: SearchPickerProps<T>) {
  const wasVisible = useRef(props.visible)
  useEffect(() => {
    if (props.visible) {
      wasVisible.current = true
      props.searchRef.current?.focus()
      return
    }
    const shouldRestore = wasVisible.current && props.shouldRestoreFocus !== false
    wasVisible.current = false
    if (!shouldRestore) return
    // OpenTUI 在销毁已聚焦 textarea 的同一提交中还未完成 focus graph 更新，延后一个 tick
    // 才能稳定把焦点交回 Composer；仅在真正从 visible 关闭时执行，避免干扰其他浮层。
    const timer = setTimeout(() => props.restoreFocusRef?.current?.focus(), 0)
    return () => clearTimeout(timer)
  }, [props.restoreFocusRef, props.searchRef, props.shouldRestoreFocus, props.visible])
  if (!props.visible) return null

  return (
    <OverlayShell terminalWidth={props.terminalWidth} terminalHeight={props.terminalHeight} placement="picker">
      {({ compact, width, maxRows }) => {
        const rows = props.loading || props.error
          ? 1
          : Math.max(1, Math.min(maxRows, props.items.length))
        const selectedIndex = Math.min(props.selectedIndex, Math.max(0, props.items.length - 1))
        const itemRows = props.items.map((item, index) => {
          const selected = index === selectedIndex
          return (
            <box
              key={props.itemKey(item)}
              backgroundColor={selected ? tuiTheme.pickerActive : tuiTheme.menu}
              height={1}
              paddingLeft={3}
              paddingRight={3}
              flexDirection="row"
              gap={2}
              onMouseOver={() => props.onHover(index)}
              onMouseUp={() => props.onSelect(item)}
            >
              {props.renderItem(item, { compact, width, selected })}
            </box>
          )
        })

        return (
          <box width={width} maxWidth="100%" backgroundColor={tuiTheme.menu} flexDirection="column" zIndex={1}>
            <box paddingLeft={4} paddingRight={4} paddingTop={2} paddingBottom={1} flexDirection="column">
              <box flexDirection="row" justifyContent="space-between">
                <text fg={tuiTheme.text}><strong>{props.title}</strong></text>
                <text fg={tuiTheme.muted} onMouseUp={props.onClose}>esc</text>
              </box>
              <box marginTop={1}>
                <textarea
                  id={props.searchId}
                  ref={props.searchRef}
                  placeholder={props.searchPlaceholder}
                  placeholderColor={tuiTheme.muted}
                  textColor={tuiTheme.text}
                  focusedTextColor={tuiTheme.text}
                  backgroundColor={tuiTheme.menu}
                  focusedBackgroundColor={tuiTheme.menu}
                  cursorColor={tuiTheme.primary}
                  minHeight={1}
                  maxHeight={1}
                  keyBindings={PICKER_SEARCH_KEY_BINDINGS}
                  focused
                  onContentChange={() => props.onSearch(props.searchRef.current?.plainText ?? "")}
                  onSubmit={() => {
                    const selected = props.items[selectedIndex]
                    if (selected) props.onSelect(selected)
                  }}
                />
              </box>
            </box>
            <box paddingLeft={4} paddingRight={4} paddingTop={1} paddingBottom={1}>
              <text fg={tuiTheme.primary}><strong>{props.title}</strong></text>
            </box>
            {props.loading ? (
              <box height={rows} paddingLeft={4} paddingRight={4} paddingBottom={2}>
                <text fg={tuiTheme.muted}>{props.loadingMessage ?? `正在读取 ${props.title}…`}</text>
              </box>
            ) : props.error ? (
              <box height={rows} paddingLeft={4} paddingRight={4} paddingBottom={2}>
                <text fg={tuiTheme.danger}>{shorten(props.error, width - 8)}</text>
              </box>
            ) : props.items.length ? (
              props.items.length > maxRows ? (
                <scrollbox height={rows} paddingLeft={1} paddingRight={1} paddingBottom={2} viewportOptions={{ paddingRight: 1 }}>{itemRows}</scrollbox>
              ) : <box flexDirection="column" paddingLeft={1} paddingRight={1} paddingBottom={2}>{itemRows}</box>
            ) : (
              <box paddingLeft={4} paddingRight={4} paddingBottom={2}>
                <text fg={tuiTheme.muted}>{props.emptyMessage}</text>
              </box>
            )}
          </box>
        )
      }}
    </OverlayShell>
  )
}

/** Dialog 复用与 Picker 相同的遮罩和窄终端约束，避免每种确认操作重复搭建浮层。 */
export function DialogShell(props: DialogShellProps) {
  const wasVisible = useRef(props.visible)
  useEffect(() => {
    if (props.visible) {
      wasVisible.current = true
      return
    }
    const shouldRestore = wasVisible.current && props.shouldRestoreFocus !== false
    wasVisible.current = false
    if (!shouldRestore) return
    const timer = setTimeout(() => props.restoreFocusRef?.current?.focus(), 0)
    return () => clearTimeout(timer)
  }, [props.restoreFocusRef, props.shouldRestoreFocus, props.visible])
  if (!props.visible) return null
  return (
    <OverlayShell terminalWidth={props.terminalWidth} terminalHeight={props.terminalHeight} placement="dialog" zIndex={101}>
      {({ width }) => (
        <box width={width} maxWidth="100%" backgroundColor={tuiTheme.menu} flexDirection="column" zIndex={1} paddingLeft={4} paddingRight={4} paddingTop={2} paddingBottom={2}>
          <text fg={tuiTheme.text}><strong>{props.title}</strong></text>
          {props.message ? (
            <box paddingTop={1}>
              <text fg={tuiTheme.muted} wrapMode="word">{props.message}</text>
            </box>
          ) : null}
          {props.children ? <box paddingTop={1}>{props.children}</box> : null}
          <box paddingTop={2} flexDirection="row" gap={2}>
            {props.onConfirm ? (
              <box backgroundColor={tuiTheme.primarySoft} paddingLeft={2} paddingRight={2} onMouseUp={props.onConfirm}>
                <text fg={tuiTheme.text}>{props.confirmLabel ?? "Enter 确认"}</text>
              </box>
            ) : null}
            <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} onMouseUp={props.onCancel}>
              <text fg={tuiTheme.muted}>{props.cancelLabel ?? "Esc 取消"}</text>
            </box>
          </box>
        </box>
      )}
    </OverlayShell>
  )
}

/** 浮层公共外壳：背景只调暗已渲染内容，面板维持原始前景色和可读性。 */
function OverlayShell(props: {
  terminalWidth: number
  terminalHeight: number
  placement: "picker" | "dialog"
  zIndex?: number
  children: (metrics: { compact: boolean; width: number; maxRows: number }) => ReactNode
}) {
  const compact = props.terminalWidth < 64 || props.terminalHeight < 19
  const width = props.placement === "dialog"
    ? compact
      ? Math.max(36, props.terminalWidth - 4)
      : Math.max(54, Math.min(76, Math.floor(props.terminalWidth * 0.62)))
    : compact
      ? Math.max(36, props.terminalWidth - 4)
      : Math.max(60, Math.min(108, Math.floor(props.terminalWidth * 0.76)))
  const maxRows = compact
    ? Math.max(3, Math.min(6, props.terminalHeight - 10))
    : Math.max(5, Math.min(12, props.terminalHeight - 14))
  const paddingTop = compact
    ? 1
    : props.placement === "dialog"
      ? Math.max(2, Math.floor(props.terminalHeight / 3))
      : Math.max(2, Math.floor(props.terminalHeight / 4))

  return (
    <box position="absolute" top={0} left={0} width="100%" height="100%" zIndex={props.zIndex ?? 100} alignItems="center" justifyContent="flex-start" paddingTop={paddingTop} paddingLeft={2} paddingRight={2}>
      <OverlayBackdrop />
      {props.children({ compact, width, maxRows })}
    </box>
  )
}

/** 选择器与 Dialog 共用的背景层，避免半透明 Box 覆盖中文宽字符。 */
function OverlayBackdrop() {
  return (
    <box
      position="absolute"
      top={0}
      left={0}
      width="100%"
      height="100%"
      renderAfter={dimOverlayBackdrop}
    />
  )
}

/** 用颜色矩阵压暗底层 buffer，不写入字符单元格，保持所有浮层面板清晰。 */
function dimOverlayBackdrop(buffer: OptimizedBuffer): void {
  buffer.colorMatrixUniform(OVERLAY_DIM_MATRIX)
}

/** 按字符数截断错误文字，防止窄终端中的错误行撑破浮层宽度。 */
function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}

const OVERLAY_BACKDROP_OPACITY = 0.72
const [overlayRed, overlayGreen, overlayBlue] = RGBA.fromHex(tuiTheme.overlay).toInts()
const OVERLAY_DIM_MATRIX = new Float32Array([
  1 - OVERLAY_BACKDROP_OPACITY, 0, 0, overlayRed / 255 * OVERLAY_BACKDROP_OPACITY,
  0, 1 - OVERLAY_BACKDROP_OPACITY, 0, overlayGreen / 255 * OVERLAY_BACKDROP_OPACITY,
  0, 0, 1 - OVERLAY_BACKDROP_OPACITY, overlayBlue / 255 * OVERLAY_BACKDROP_OPACITY,
  0, 0, 0, 1,
])

/** Picker 搜索框是单行选择控件；Enter 选择当前项，而不是插入换行。 */
const PICKER_SEARCH_KEY_BINDINGS: Array<{ name: string; action: "submit" }> = [
  { name: "return", action: "submit" },
  { name: "kpenter", action: "submit" },
  { name: "linefeed", action: "submit" },
]
