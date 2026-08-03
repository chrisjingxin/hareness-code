/** Web 接管/回收期间的静态全屏视图；不展示内部 ID、URL 或凭据。 */

import { useKeyboard } from "@opentui/react"

import type { WebHandoffSnapshot } from "../../web/handoff-coordinator"
import { tuiTheme } from "./theme"

/** 只显示 phase 与恢复提示；用户可通过浏览器返回或 Ctrl+C 退出。 */
export function WebTakeoverView(props: {
  snapshot: WebHandoffSnapshot
  onExit: () => void
}) {
  const { snapshot, onExit } = props
  useKeyboard(key => {
    if (key.ctrl && key.name === "c") {
      key.preventDefault()
      onExit()
    }
  })
  const title = snapshot.phase === "active" ? "已移交 Web" : "正在回收控制权"
  const detail = snapshot.phase === "active"
    ? "当前会话已由浏览器接管。完成操作后点击页面中的“返回 TUI”，或直接关闭浏览器窗口。"
    : snapshot.phase === "returning"
      ? snapshot.tuiLocked
        ? "浏览器已归还控制权，正在等待 Host 确认后恢复终端输入。"
        : "Web 会话正在回收，稍候将恢复终端。"
      : "正在启动 Web 会话。"
  return (
    <box
      flexGrow={1}
      flexDirection="column"
      alignItems="center"
      justifyContent="center"
      backgroundColor={tuiTheme.background}
    >
      <text fg={tuiTheme.primary}>{title}</text>
      <box height={1} />
      <text fg={tuiTheme.muted}>{detail}</text>
      <box height={1} />
      <text fg={tuiTheme.muted}>按 Ctrl+C 退出 za38</text>
    </box>
  )
}
