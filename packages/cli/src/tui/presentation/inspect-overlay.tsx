/** 执行中 Goal/Plan/MCP 只读查看浮层；不占审批底部槽。 */

import type { ScrollBoxRenderable } from "@opentui/core"
import { useKeyboard } from "@opentui/react"
import { useRef, type ReactNode } from "react"

import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { OverlayShell } from "./overlays"
import { createScrollAcceleration } from "./scroll.js"
import { markdownSyntax, tuiTheme } from "./theme"

export type InspectOverlayProps = {
  visible: boolean
  title: string
  body: string
  terminalWidth: number
  terminalHeight: number
  onClose: () => void
}

/** 盖在时间线之上的只读查看浮层：Markdown 高亮、过长正文可滚，Esc 关闭后主 Run 继续。 */
export function InspectOverlay(props: InspectOverlayProps): ReactNode {
  const scrollRef = useRef<ScrollBoxRenderable | null>(null)
  useKeyboard(key => {
    if (!props.visible) return
    const scroll = scrollRef.current
    if (!scroll || scroll.isDestroyed) return
    const page = Math.max(1, Math.floor(scroll.height / 2))
    if (key.name === "up" || key.sequence === "k") {
      key.preventDefault()
      scroll.scrollBy(-1)
      return
    }
    if (key.name === "down" || key.sequence === "j") {
      key.preventDefault()
      scroll.scrollBy(1)
      return
    }
    if (key.name === "pageup") {
      key.preventDefault()
      scroll.scrollBy(-page)
      return
    }
    if (key.name === "pagedown") {
      key.preventDefault()
      scroll.scrollBy(page)
      return
    }
    if (key.name === "home") {
      key.preventDefault()
      scroll.scrollTo(0)
      return
    }
    if (key.name === "end") {
      key.preventDefault()
      scroll.scrollTo(scroll.scrollHeight)
    }
  })
  if (!props.visible) return null
  return (
    <OverlayShell terminalWidth={props.terminalWidth} terminalHeight={props.terminalHeight} placement="viewer" zIndex={104}>
      {({ width, maxRows }: { width: number; maxRows: number }) => (
        <box
          width={width}
          height={Math.max(10, maxRows + 5)}
          maxWidth="100%"
          overflow="hidden"
          backgroundColor={tuiTheme.menu}
          flexDirection="column"
          zIndex={1}
          paddingLeft={3}
          paddingRight={3}
          paddingTop={1}
          paddingBottom={1}
        >
          <text fg={tuiTheme.text}>
            <strong>{props.title}</strong>
          </text>
          <scrollbox
            ref={scrollRef}
            flexGrow={1}
            minHeight={0}
            marginTop={1}
            stickyScroll={false}
            scrollAcceleration={createScrollAcceleration()}
            viewportOptions={{ paddingRight: 1 }}
          >
            <box width="100%" flexDirection="column">
              <markdown
                content={props.body}
                syntaxStyle={markdownSyntax}
                treeSitterClient={getCommonSyntaxClient()}
                streaming={false}
                fg={tuiTheme.text}
                bg={tuiTheme.menu}
                conceal
                concealCode={false}
                internalBlockMode="top-level"
              />
            </box>
          </scrollbox>
          <text fg={tuiTheme.muted}>Esc 关闭 · ↑↓ 滚动</text>
        </box>
      )}
    </OverlayShell>
  )
}
