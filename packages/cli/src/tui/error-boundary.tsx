import { Component, type ErrorInfo, type ReactNode } from "react"
import { useKeyboard } from "@opentui/react"

import { tuiTheme } from "./theme"

type ErrorBoundaryProps = {
  children: ReactNode
  onRequestExit: () => void
}

type ErrorBoundaryState = {
  failed: boolean
}

/**
 * 渲染异常不能让终端停留在半绘制状态。这里故意不显示原始异常文本，避免把模型配置、
 * 工作区路径或工具输出意外回显到终端；详细诊断仍可由启动层写入 stderr。
 */
export class TuiErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true }
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // React 要求保留错误边界钩子；不向 stdout/stderr写原始错误以维持敏感信息边界。
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children
    return <ErrorFallback onRequestExit={this.props.onRequestExit} />
  }
}

function ErrorFallback(props: { onRequestExit: () => void }) {
  useKeyboard(key => {
    if ((key.ctrl && (key.name === "c" || key.name === "d")) || key.name === "escape") {
      key.preventDefault()
      props.onRequestExit()
    }
  })
  return (
    <box flexGrow={1} backgroundColor={tuiTheme.background} paddingLeft={2} paddingRight={2} justifyContent="center" flexDirection="column">
      <text fg={tuiTheme.danger}>Harness Code 界面发生错误</text>
      <text fg={tuiTheme.muted}>按 Ctrl+C、Ctrl+D 或 Esc 退出后重试；如问题持续，请提供 stderr 日志。</text>
    </box>
  )
}
