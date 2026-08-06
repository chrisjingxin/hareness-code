/** MCP 面板：连接状态、脱敏错误与添加表单（迁移自 panels.tsx，dispatch 语义不变）。 */
/** @jsxImportSource react */

import { useState } from "react"
import { Plus, Trash2 } from "lucide-react"

import type { McpServerStatus } from "@za38/protocol"

import type { InteractiveMcpInput, McpServerSummary } from "../../../interactive/types"
import { selectNavigationView } from "../../../interactive/selectors"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { PanelEmpty, PanelError, PanelToolbar } from "./panel-common"

/** MCP 面板：服务器状态列表 + 添加表单；管理能力缺失时只读。 */
export function McpPanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.mcp
  const items = filterMcp(catalog.items, snapshot.panelSearch.mcp.query)
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  const panelState = snapshot.panelSearch.mcp
  const manageAllowed = selectNavigationView(snapshot.interactive).availability.hasMcpManage
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  return (
    <div className="panel panel-mcp">
      <PanelToolbar
        query={snapshot.panelSearch.mcp.query}
        placeholder="搜索 MCP…"
        onSearch={value => dispatch({ type: "panel-search", panel: "mcp", query: value })}
        onRefresh={() => dispatch({ type: "dock-panel-select", panel: "mcp" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "dock-panel-select", panel: "mcp" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 MCP…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有已配置的 MCP 服务器" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(server => (
            <li key={server.name} className="panel-item-row">
              <div className="panel-item" data-status={server.status}>
                <span className="panel-item-title">{server.name}</span>
                <span className="panel-item-sub">
                  {server.transport} · {server.tool_names.length} 个工具 · {describeMcpStatus(server.status)}
                </span>
                {server.error ? (
                  <span className="panel-item-note">{server.error}</span>
                ) : null}
              </div>
              {manageAllowed ? <button
                type="button"
                className="icon-button"
                disabled={disabled || busy}
                onClick={() => dispatch({ type: "mcp-remove", name: server.name })}
                aria-label={`移除 ${server.name}`}
                title="移除"
              >
                <Trash2 aria-hidden="true" />
              </button> : null}
            </li>
          ))}
        </ul>
      )}
      {manageAllowed ? <McpAddForm
          error={panelState.error}
          submitting={panelState.submitting}
          disabled={disabled || busy}
          dispatch={dispatch}
        /> : <p className="panel-status">当前连接没有 mcp.manage，只能查看状态。</p>}
    </div>
  )
}

function filterMcp(
  items: readonly McpServerSummary[],
  query: string,
): readonly McpServerSummary[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(server => {
    const haystack = `${server.name} ${server.transport} ${server.error ?? ""}`.toLowerCase()
    return haystack.includes(needle)
  })
}

function describeMcpStatus(status: McpServerStatus["status"]): string {
  switch (status) {
    case "connected": return "已连接"
    case "failed": return "连接失败"
    case "skipped": return "已跳过"
    default: return "未知"
  }
}

/** MCP 添加表单：本地持有输入草稿，仅在提交时构造 typed `mcp.add` intent。 */
function McpAddForm({
  error,
  submitting,
  disabled,
  dispatch,
}: {
  error: string | null
  submitting: boolean
  disabled: boolean
  dispatch: (intent: WebIntent) => void
}): React.ReactElement {
  const [name, setName] = useState("")
  const [transport, setTransport] = useState<"stdio" | "url">("stdio")
  const [command, setCommand] = useState("")
  const [args, setArgs] = useState("")
  const [url, setUrl] = useState("")
  const [urlKind, setUrlKind] = useState<"http" | "sse">("http")
  const [localError, setLocalError] = useState<string | null>(null)

  const reset = (): void => {
    setName("")
    setCommand("")
    setArgs("")
    setUrl("")
    setLocalError(null)
  }

  const submit = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (disabled || submitting) return
    const trimmedName = name.trim()
    if (!trimmedName) {
      setLocalError("请填写服务器名称")
      return
    }
    let input: InteractiveMcpInput
    if (transport === "stdio") {
      const trimmedCommand = command.trim()
      if (!trimmedCommand) {
        setLocalError("请填写 stdio 命令")
        return
      }
      input = {
        name: trimmedName,
        transport: "stdio",
        command: trimmedCommand,
        ...(args.trim() ? { args: args.trim().split(/\s+/).filter(Boolean) } : {}),
      }
    } else {
      const trimmedUrl = url.trim()
      if (!trimmedUrl) {
        setLocalError("请填写 URL")
        return
      }
      if (!/^https?:\/\//i.test(trimmedUrl)) {
        setLocalError("URL 必须以 http:// 或 https:// 开头")
        return
      }
      input = { name: trimmedName, transport: urlKind, url: trimmedUrl }
    }
    setLocalError(null)
    dispatch({ type: "mcp-add", input })
    reset()
  }

  return (
    <form className="mcp-add-form" onSubmit={submit}>
      <h3 className="mcp-add-form-title">添加 MCP 服务器</h3>
      <label className="mcp-field">
        <span>名称</span>
        <input
          type="text"
          value={name}
          onChange={event => setName(event.currentTarget.value)}
          disabled={disabled || submitting}
          required
          autoComplete="off"
        />
      </label>
      <label className="mcp-field">
        <span>传输方式</span>
        <select
          value={transport}
          onChange={event => setTransport(event.currentTarget.value === "url" ? "url" : "stdio")}
          disabled={disabled || submitting}
        >
          <option value="stdio">stdio</option>
          <option value="url">URL（HTTP / SSE）</option>
        </select>
      </label>
      {transport === "stdio" ? (
        <>
          <label className="mcp-field">
            <span>命令</span>
            <input
              type="text"
              value={command}
              onChange={event => setCommand(event.currentTarget.value)}
              disabled={disabled || submitting}
              placeholder="例如：npx"
              required
              autoComplete="off"
            />
          </label>
          <label className="mcp-field">
            <span>参数（按空白分隔）</span>
            <input
              type="text"
              value={args}
              onChange={event => setArgs(event.currentTarget.value)}
              disabled={disabled || submitting}
              placeholder="例如：-y @example/mcp"
              autoComplete="off"
            />
          </label>
        </>
      ) : (
        <>
          <label className="mcp-field">
            <span>URL</span>
            <input
              type="url"
              value={url}
              onChange={event => setUrl(event.currentTarget.value)}
              disabled={disabled || submitting}
              placeholder="https://example.com/mcp"
              required
              autoComplete="off"
            />
          </label>
          <label className="mcp-field">
            <span>URL 类型</span>
            <select
              value={urlKind}
              onChange={event => setUrlKind(event.currentTarget.value === "sse" ? "sse" : "http")}
              disabled={disabled || submitting}
            >
              <option value="http">HTTP</option>
              <option value="sse">SSE</option>
            </select>
          </label>
        </>
      )}
      {(localError ?? error) ? (
        <p className="mcp-add-form-error" role="alert">{localError ?? error}</p>
      ) : null}
      <div className="mcp-add-form-actions">
        <button
          type="submit"
          className="button button-primary"
          disabled={disabled || submitting}
        >
          <Plus aria-hidden="true" />
          <span>{submitting ? "正在添加…" : "添加"}</span>
        </button>
      </div>
    </form>
  )
}
