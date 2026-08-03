/** Harness Code 的沉浸式首页视图。 */

import { supportsHomeDecoration } from "../../interactive/runtime"
import { Composer, FooterRail } from "./composer"
import { HarnessCodeLogo } from "./harness-logo"
import { StarryBackground } from "./starry-background"
import { tuiTheme } from "./theme"
import type { SharedViewProps } from "./types"

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
      <FooterRail interactive={props.interactive} terminalWidth={props.terminalWidth} />
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
