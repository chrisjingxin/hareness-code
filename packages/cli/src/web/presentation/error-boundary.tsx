/** React presentation Error Boundary：捕获渲染异常并交给 composition root 收敛资源。 */
/** @jsxImportSource react */

import { Component, type ErrorInfo, type ReactNode } from "react"

type PresentationErrorBoundaryProps = {
  children: ReactNode
  onError: (error: Error, info: ErrorInfo) => void
}

type PresentationErrorBoundaryState = {
  failed: boolean
}

/**
 * 只包住 Web presentation；错误详情不进入页面，composition root 负责关闭
 * Controller、AgentClient 和 lifecycle 后展示统一脱敏状态。
 */
export class PresentationErrorBoundary extends Component<
  PresentationErrorBoundaryProps,
  PresentationErrorBoundaryState
> {
  state: PresentationErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): PresentationErrorBoundaryState {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError(error, info)
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <main className="web-static-state closed" role="alert">
          <h1>Web 工作台暂时不可用</h1>
          <p>本次接管正在关闭，请返回 TUI 后重新执行 /web。</p>
        </main>
      )
    }
    return this.props.children
  }
}
