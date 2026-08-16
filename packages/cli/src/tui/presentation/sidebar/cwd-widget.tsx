/** TUI 侧边栏工作目录小部件。 */

import type { GitWorkspaceState } from "../../../interactive/runtime"
import { gitWorkspaceLabel } from "../../../presentation-shared"
import { tuiTheme } from "../theme"

export type CwdWidgetProps = {
  workspace: string
  gitWorkspace?: GitWorkspaceState
}

/** 格式化工作区路径为简短易读形式，并提取目录名。 */
export function formatWorkspacePath(fullPath: string): { basename: string; displayPath: string } {
  if (!fullPath) return { basename: "-", displayPath: "-" }
  const normalized = fullPath.replace(/\\/g, "/")
  const home = (process.env.HOME || "").replace(/\\/g, "/")
  
  let displayPath = normalized
  if (home && normalized.startsWith(home)) {
    displayPath = `~${normalized.slice(home.length)}`
  }

  const parts = normalized.split("/").filter(Boolean)
  const basename = parts[parts.length - 1] || normalized

  return { basename, displayPath }
}

export function CwdWidget(props: CwdWidgetProps) {
  const { basename, displayPath } = formatWorkspacePath(props.workspace)
  const branch = gitWorkspaceLabel(props.gitWorkspace)

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1} border={["bottom"]} borderColor={tuiTheme.border}>
      <box flexDirection="row" justifyContent="space-between" paddingBottom={0}>
        <text fg={tuiTheme.subtle}>
          <b>工作目录</b>
        </text>
        {branch ? (
          <text fg={tuiTheme.muted}>
            ⎇ {branch}
          </text>
        ) : null}
      </box>
      <text fg={tuiTheme.text}>
        <b>{basename}</b>
      </text>
      <text fg={tuiTheme.muted}>
        {displayPath}
      </text>
    </box>
  )
}
