/** Status 面板：只读展示共享 runtime / connection / 当前模型摘要（迁移自 panels.tsx）。 */
/** @jsxImportSource react */

import {
  approvalModeLabel,
  executionStatusLabel,
  workspaceLabel,
} from "../../../interactive/runtime"
import { gitWorkspaceLabel } from "../../../presentation-shared"

import type { WebAdapterSnapshot } from "../../application/adapter"

/** Status 面板：只读，不生成第二份状态叙事；Git 行保留 gitWorkspaceLabel。 */
export function StatusPanel({
  snapshot,
}: {
  snapshot: WebAdapterSnapshot
}): React.ReactElement {
  const interactive = snapshot.interactive
  const runtime = interactive.runtime
  const connection = interactive.connection
  const capabilities = runtime.capabilities ?? []

  // Timeline 统计
  let messagesCount = 0
  let toolCallsCount = 0
  for (const item of interactive.timeline) {
    if (item.type === "message") messagesCount++
    else if (item.type === "tool") toolCallsCount++
  }

  // MCP 统计
  const mcpItems = interactive.catalogs.mcp.items
  let totalTools = 0
  for (const s of mcpItems) {
    totalTools += s.tool_names?.length ?? 0
  }
  const mcpText = mcpItems.length > 0
    ? `${mcpItems.length} 个服务 (${totalTools} 工具)`
    : (runtime.mcpSummary ?? "未配置")

  // Token 用量
  const lastRun = interactive.lastRun
  const inputTokens = lastRun?.usage?.inputTokens ?? 0
  const outputTokens = lastRun?.usage?.outputTokens ?? 0
  const totalTokens = inputTokens + outputTokens
  const cachedTokens = lastRun?.usage?.cachedTokens
  const cacheHitRate = cachedTokens !== undefined && inputTokens > 0
    ? Math.round((cachedTokens / inputTokens) * 100)
    : null

  return (
    <div className="panel status-view">
      {/* 模块 1: 工作区与环境 */}
      <div className="status-section">
        <h4 className="panel-section-title">工作区与环境</h4>
        <dl className="status-list">
          <dt>目录</dt>
          <dd>{workspaceLabel(runtime.workspace)}</dd>
          {gitWorkspaceLabel(runtime.gitWorkspace) ? (
            <>
              <dt>Git 分支</dt>
              <dd>{gitWorkspaceLabel(runtime.gitWorkspace)}</dd>
            </>
          ) : null}
          <dt>CLI 版本</dt>
          <dd>za38-cli {runtime.cliVersion ?? "0.1.0"}</dd>
        </dl>
      </div>

      {/* 模块 2: 运行模式与模型 */}
      <div className="status-section">
        <h4 className="panel-section-title">运行模式与模型</h4>
        <dl className="status-list">
          <dt>工作模式</dt>
          <dd>{interactive.workMode === "compose" ? "Compose 模式" : "Build 模式"}</dd>
          <dt>当前模型</dt>
          <dd>{describeCurrentModel(snapshot)}</dd>
          <dt>执行环境</dt>
          <dd>{executionStatusLabel(runtime)}</dd>
          <dt>审批模式</dt>
          <dd>{approvalModeLabel(runtime)}</dd>
          {runtime.approvalModeWarning ? (
            <>
              <dt>提示</dt>
              <dd className="status-warning">{runtime.approvalModeWarning}</dd>
            </>
          ) : null}
        </dl>
      </div>

      {/* 模块 3: 会话与上下文健康度 */}
      <div className="status-section">
        <h4 className="panel-section-title">会话与上下文</h4>
        <dl className="status-list">
          <dt>当前 Thread</dt>
          <dd>{interactive.currentThreadId ? interactive.currentThreadId.slice(0, 16) : "新会话"}</dd>
          <dt>对话统计</dt>
          <dd>{messagesCount} 条消息 · {toolCallsCount} 次工具调用</dd>
          {totalTokens > 0 ? (
            <>
              <dt>本轮用量</dt>
              <dd>{totalTokens.toLocaleString()} tokens {cacheHitRate !== null ? `(${cacheHitRate}% 缓存命中)` : ""}</dd>
            </>
          ) : (
            <>
              <dt>本轮用量</dt>
              <dd>0 tokens (新会话)</dd>
            </>
          )}
        </dl>
      </div>

      {/* 模块 4: 扩展生态与连接 */}
      <div className="status-section">
        <h4 className="panel-section-title">扩展生态与连接</h4>
        <dl className="status-list">
          <dt>服务连接</dt>
          <dd>{describeConnection(connection)}</dd>
          <dt>MCP 服务</dt>
          <dd>{mcpText}</dd>
          <dt>技能与代理</dt>
          <dd>{interactive.catalogs.skills.items.length} 个 Skill · {interactive.catalogs.agents.items.length} 个 Agent</dd>
          <dt>已协商能力</dt>
          <dd>{capabilities.length === 0 ? "未协商" : capabilities.join("、")}</dd>
          {runtime.startupError ? (
            <>
              <dt>启动错误</dt>
              <dd className="status-danger">{runtime.startupError}</dd>
            </>
          ) : null}
        </dl>
      </div>
    </div>
  )
}

function describeConnection(connection: WebAdapterSnapshot["interactive"]["connection"]): string {
  if (connection.status === "open") return "🟢 正常 (Connected)"
  return `🔴 异常 (${connection.message})`
}

/** 把 actualModel 与 requestedModelProfileId 收敛成可见字符串；不渲染 ID 之外的 protocol 字段。 */
function describeCurrentModel(snapshot: WebAdapterSnapshot): string {
  const actual = snapshot.interactive.selection.actualModel
  if (actual) return `${actual.id} · ${actual.model}`
  const requested = snapshot.interactive.selection.requestedModelProfileId
  if (requested) return requested
  return snapshot.interactive.runtime.modelName ?? "未选择"
}
