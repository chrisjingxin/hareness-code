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
  const runtime = snapshot.interactive.runtime
  const connection = snapshot.interactive.connection
  const capabilities = runtime.capabilities ?? []
  return (
    <div className="panel status-view">
      <dl className="status-list">
        <dt>工作区</dt>
        <dd>{workspaceLabel(runtime.workspace)}</dd>
        {gitWorkspaceLabel(runtime.gitWorkspace) ? (
          <>
            <dt>分支</dt>
            <dd>{gitWorkspaceLabel(runtime.gitWorkspace)}</dd>
          </>
        ) : null}
        <dt>当前模型</dt>
        <dd>{describeCurrentModel(snapshot)}</dd>
        <dt>审批模式</dt>
        <dd>{approvalModeLabel(runtime)}</dd>
        <dt>执行</dt>
        <dd>{executionStatusLabel(runtime)}</dd>
        <dt>连接</dt>
        <dd>{describeConnection(connection)}</dd>
        <dt>能力</dt>
        <dd>{capabilities.length === 0 ? "未协商" : capabilities.join("、")}</dd>
        {runtime.approvalModeWarning ? (
          <>
            <dt>提示</dt>
            <dd>{runtime.approvalModeWarning}</dd>
          </>
        ) : null}
        {runtime.startupError ? (
          <>
            <dt>启动错误</dt>
            <dd>{runtime.startupError}</dd>
          </>
        ) : null}
        {runtime.mcpSummary ? (
          <>
            <dt>MCP</dt>
            <dd>{runtime.mcpSummary}</dd>
          </>
        ) : null}
      </dl>
    </div>
  )
}

function describeConnection(connection: WebAdapterSnapshot["interactive"]["connection"]): string {
  if (connection.status === "open") return "已连接"
  return connection.message
}

/** 把 actualModel 与 requestedModelProfileId 收敛成可见字符串；不渲染 ID 之外的 protocol 字段。 */
function describeCurrentModel(snapshot: WebAdapterSnapshot): string {
  const actual = snapshot.interactive.selection.actualModel
  if (actual) return `${actual.id} · ${actual.model}`
  const requested = snapshot.interactive.selection.requestedModelProfileId
  if (requested) return requested
  return snapshot.interactive.runtime.modelName ?? "未选择"
}
