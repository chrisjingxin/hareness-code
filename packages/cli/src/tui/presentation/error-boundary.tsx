/** TUI 错误边界：异常时降级为可退出的安全页面，避免残留损坏终端。 */

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

  /** React 捕获子树错误后切换到降级视图。 */
  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true }
  }

  /** 保留错误边界钩子，但不回显潜在包含凭据的原始异常。 */
  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // React 要求保留错误边界钩子；不向 stdout/stderr写原始错误以维持敏感信息边界。
  }

  /** 正常渲染子树；失败后只渲染受控的错误页。 */
  render(): ReactNode {
    if (!this.state.failed) return this.props.children
    return <ErrorFallback onRequestExit={this.props.onRequestExit} />
  }
}

/** 错误边界的可退出降级页，避免异常后用户被困在不可交互终端。 */
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
