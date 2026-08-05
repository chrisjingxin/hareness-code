/** Web 工具面板：model / skills / mcp / status / help；只渲染共享 snapshot 与 WebIntent。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import {
  Cpu,
  Info,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Trash2,
  Wrench,
  X,
} from "lucide-react"
import type { McpServerStatus, ModelProfile } from "@za38/protocol"
import { selectNavigationView, type FeatureAvailability } from "../../interactive/selectors"

import type { CommandMenuItem } from "../../interactive/commands"
import {
  approvalModeLabel,
  executionStatusLabel,
  workspaceLabel,
} from "../../interactive/runtime"
import type {
  InteractiveMcpInput,
  McpServerSummary,
  SkillSummary,
} from "../../interactive/types"
import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/** 工作台主 tab：只有 capability 允许的 tab 可见；Help 与 Threads 不属于主 tab。 */
type MainTab = "models" | "skills" | "mcp" | "status"

const MAIN_TABS: readonly MainTab[] = ["models", "skills", "mcp", "status"]

/**
 * 渲染稳定的 Utility workspace：右侧工作台壳 + Model/Skills/MCP/Status 主 tab。
 *
 * `activePanel` 为 null 时返回空节点。主 tab 切换复用 `panel-open` 意图（Adapter 负责
 * catalog refresh）；Help/Threads 复用同一 372px 外壳但隐藏主 tab。移动端（`narrow`）
 * 该外壳变为从右侧进入的抽屉，带 scrim 与焦点限制。用户动作只回传稳定 ID 或 typed
 * intent；不直接访问 AgentClient，也不重新计算 capability/busy 状态。
 */
export function UtilityPanels({
  snapshot,
  dispatch,
  narrow = false,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  narrow?: boolean
  disabled?: boolean
}): React.ReactElement {
  const panel = snapshot.activePanel
  if (panel === null) return <></>
  const interactive = snapshot.interactive
  const busy = Boolean(interactive.activeRun) || Boolean(interactive.interaction)
  const busyReason = busy ? "当前任务结束后可用" : null
  const { availability } = selectNavigationView(interactive)
  const isMainTab = panel === "models" || panel === "skills" || panel === "mcp" || panel === "status"
  const title = panel === "help" ? "帮助" : panel === "threads" ? "Threads" : "工作台"
  const drawerRef = useRef<HTMLDivElement | null>(null)

  /** tablist 内使用方向键/Home/End 循环移动并立即激活对应面板。 */
  const handleTabListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)'))
    if (tabs.length === 0) return
    const currentIndex = Math.max(0, tabs.indexOf(document.activeElement as HTMLButtonElement))
    let nextIndex: number | null = null
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = tabs.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    const next = tabs[nextIndex]
    next?.focus()
    const nextPanel = next?.dataset.panel as MainTab | undefined
    if (nextPanel) dispatch({ type: "panel-open", panel: nextPanel })
  }

  // 移动端抽屉打开时限制 Tab 焦点在抽屉内并聚焦第一项；关闭后由 WebApp 恢复触发器焦点。
  useEffect(() => {
    if (!narrow || panel === null) return
    const drawer = drawerRef.current
    if (!drawer) return
    const focusables = drawer.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    focusables[0]?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    drawer.addEventListener("keydown", onKeyDown)
    return () => drawer.removeEventListener("keydown", onKeyDown)
  }, [narrow, panel])

  return (
    <>
      {narrow ? <button type="button" className="drawer-scrim utility-drawer-scrim" aria-label="关闭工作台面板" onClick={() => dispatch({ type: "panel-close" })} /> : null}
      <aside
        ref={drawerRef}
        className="utility-drawer"
        data-open="true"
        role={narrow ? "dialog" : undefined}
        aria-modal={narrow ? true : undefined}
        aria-label={title}
      >
        <header className="utility-drawer-header">
          <h2 className="utility-drawer-title">{title}</h2>
          <button
            type="button"
            className="icon-button panel-close"
            onClick={() => dispatch({ type: "panel-close" })}
            disabled={disabled}
            aria-label="关闭面板"
          >
            <X aria-hidden="true" />
          </button>
        </header>
        {isMainTab ? (
          <div className="workspace-tabs" role="tablist" aria-label="工作台面板" onKeyDown={handleTabListKeyDown}>
            {MAIN_TABS.filter(tab => tabVisible(tab, availability)).map(tab => (
              <button
                type="button"
                key={tab}
                role="tab"
                id={`workspace-tab-${tab}`}
                aria-selected={panel === tab}
                aria-controls={`workspace-panel-${tab}`}
                data-panel={tab}
                className={panel === tab ? "workspace-tab is-selected" : "workspace-tab"}
                disabled={disabled}
                onClick={() => dispatch({ type: "panel-open", panel: tab })}
              >
                {tabLabel(tab)}
              </button>
            ))}
          </div>
        ) : null}
        <div className="utility-drawer-body" role="tabpanel" id={isMainTab ? `workspace-panel-${panel}` : undefined}>
          {panel === "threads" ? (
            <ThreadsPanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
          ) : null}
          {panel === "models" ? (
            <ModelsPanel snapshot={snapshot} busyReason={busyReason} disabled={disabled} dispatch={dispatch} />
          ) : null}
          {panel === "skills" ? (
            <SkillsPanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
          ) : null}
          {panel === "mcp" ? (
            <McpPanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
          ) : null}
          {panel === "status" ? <StatusPanel snapshot={snapshot} /> : null}
          {panel === "help" ? <HelpPanel snapshot={snapshot} /> : null}
        </div>
      </aside>
      {narrow ? (
        <div className="drawer-scrim" onClick={() => dispatch({ type: "panel-close" })} />
      ) : null}
    </>
  )
}

/** 主 tab 可见性：只显示 capability 允许的 tab；Status 始终可见。 */
function tabVisible(tab: MainTab, availability: Pick<FeatureAvailability, "canOpenModelsPanel" | "canOpenSkillsPanel" | "canOpenMcpPanel">): boolean {
  switch (tab) {
    case "models": return availability.canOpenModelsPanel
    case "skills": return availability.canOpenSkillsPanel
    case "mcp": return availability.canOpenMcpPanel
    case "status": return true
  }
}

function tabLabel(tab: MainTab): string {
  switch (tab) {
    case "models": return "Model"
    case "skills": return "Skills"
    case "mcp": return "MCP"
    case "status": return "Status"
  }
}

function PanelToolbar({
  query,
  placeholder,
  onSearch,
  onRefresh,
  disabled = false,
}: {
  query: string
  placeholder: string
  onSearch: (value: string) => void
  onRefresh: () => void
  disabled?: boolean
}): React.ReactElement {
  return (
    <div className="panel-toolbar">
      <label className="panel-search-input">
        <Search aria-hidden="true" />
        <input
          type="search"
          value={query}
          placeholder={placeholder}
          aria-label={placeholder}
          onChange={event => onSearch(event.currentTarget.value)}
          disabled={disabled}
        />
      </label>
      <button
        type="button"
        className="icon-button"
        onClick={onRefresh}
        disabled={disabled}
        aria-label="刷新"
        title="刷新"
      >
        <RefreshCw aria-hidden="true" />
      </button>
    </div>
  )
}

function PanelError({
  message,
  onRetry,
  disabled = false,
}: {
  message: string
  onRetry: () => void
  disabled?: boolean
}): React.ReactElement {
  return (
    <div className="panel-error" role="alert">
      <p>{message}</p>
      <button
        type="button"
        className="button button-secondary"
        onClick={onRetry}
        disabled={disabled}
      >
        重试
      </button>
    </div>
  )
}

function PanelEmpty({ message }: { message: string }): React.ReactElement {
  return <p className="panel-empty">{message}</p>
}

/** Threads 面板：在 drawer 中提供与 sidebar 等价的搜索 + 列表入口。 */
function ThreadsPanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.threads
  const query = snapshot.panelSearch.threads.query
  const items = filterThreads(catalog.items, query)
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  return (
    <div className="panel panel-threads">
      <PanelToolbar
        query={query}
        placeholder="搜索 Thread…"
        onSearch={value => dispatch({ type: "panel-search", panel: "threads", query: value })}
        onRefresh={() => dispatch({ type: "thread-refresh" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "thread-refresh" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Thread…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Thread" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(thread => {
            const isActive = thread.thread_id === snapshot.interactive.currentThreadId
            const itemDisabled = disabled || (busy && !isActive)
            return (
              <li key={thread.thread_id}>
                <button
                  type="button"
                  className="panel-item"
                  data-active={isActive ? "true" : "false"}
                  data-disabled={itemDisabled ? "true" : "false"}
                  disabled={itemDisabled}
                  aria-current={isActive ? "true" : undefined}
                  onClick={() => dispatch({ type: "thread-select", threadId: thread.thread_id })}
                >
                  <span className="panel-item-title">{thread.first_message || thread.latest_message || "（无标题）"}</span>
                  <span className="panel-item-sub">{`${thread.message_count} 条消息`}</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/** Models 面板：选择当前 Thread 下一次运行的 Model Profile。 */
function ModelsPanel({
  snapshot,
  busyReason,
  disabled = false,
  dispatch,
}: {
  snapshot: WebAdapterSnapshot
  busyReason: string | null
  disabled?: boolean
  dispatch: (intent: WebIntent) => void
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.models
  const query = snapshot.panelSearch.models.query
  const items = filterModels(catalog.items, query)
  const selectedId = snapshot.interactive.selection.requestedModelProfileId
    ?? snapshot.interactive.selection.actualModel?.id
    ?? null
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  return (
    <div className="panel panel-models">
      <PanelToolbar
        query={query}
        placeholder="搜索 Model…"
        onSearch={value => dispatch({ type: "panel-search", panel: "models", query: value })}
        onRefresh={() => dispatch({ type: "panel-open", panel: "models" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "panel-open", panel: "models" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Model…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Model" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(profile => {
            const isCurrent = profile.id === selectedId
            const itemDisabled = disabled || Boolean(busyReason)
            const unavailReason = !profile.available
              ? (profile.unavailable_reason ?? "当前不可用")
              : null
            return (
              <li key={profile.id}>
                <button
                  type="button"
                  className="panel-item"
                  data-active={isCurrent ? "true" : "false"}
                  data-disabled={itemDisabled ? "true" : "false"}
                  disabled={itemDisabled}
                  aria-pressed={isCurrent}
                  title={busyReason ?? unavailReason ?? undefined}
                  onClick={() => dispatch({ type: "model-select", profileId: profile.id })}
                >
                  <span className="panel-item-title">
                    <Cpu aria-hidden="true" />
                    {profile.id}
                  </span>
                  <span className="panel-item-sub">
                    {profile.provider_label} · {profile.model}
                  </span>
                  {unavailReason ? (
                    <span className="panel-item-note">{unavailReason}</span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function filterModels(
  items: readonly ModelProfile[],
  query: string,
): readonly ModelProfile[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(profile => {
    const haystack = `${profile.id} ${profile.model} ${profile.provider_label}`.toLowerCase()
    return haystack.includes(needle)
  })
}

/** Skills 面板：选择 armed Skill；管理列表展示全部，并在具备 skills.manage 时显示启停控件。 */
function SkillsPanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.skills
  const query = snapshot.panelSearch.skills.query
  const items = filterSkills(catalog.items, query)
  const armedId = snapshot.interactive.selection.armedSkill?.id ?? null
  const manageAllowed = selectNavigationView(snapshot.interactive).availability.hasSkillManage
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  return (
    <div className="panel panel-skills">
      <PanelToolbar
        query={query}
        placeholder="搜索 Skill…"
        onSearch={value => dispatch({ type: "panel-search", panel: "skills", query: value })}
        onRefresh={() => dispatch({ type: "panel-open", panel: "skills" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "panel-open", panel: "skills" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Skill…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Skill" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(skill => {
            const isArmed = skill.id === armedId
            const canInvoke = skill.enabled && skill.userInvocable
            const reason = !skill.enabled
              ? "Skill 已停用"
              : !skill.userInvocable
                ? "该 Skill 不能手动调用"
                : null
            return (
              <li key={skill.id} className="panel-item-row">
                <button
                  type="button"
                  className="panel-item"
                  data-active={isArmed ? "true" : "false"}
                  data-disabled={!canInvoke ? "true" : "false"}
                  disabled={disabled || !canInvoke}
                  aria-pressed={isArmed}
                  title={reason ?? skill.description}
                  onClick={() => dispatch({ type: "skill-arm", skillId: skill.id })}
                >
                  <span className="panel-item-title">
                    <Wrench aria-hidden="true" />
                    {skill.name}
                  </span>
                  <span className="panel-item-sub">
                    {skill.source} · {skill.description}
                  </span>
                  {skill.argumentHint ? (
                    <span className="panel-item-note">参数：{skill.argumentHint}</span>
                  ) : null}
                  {reason ? <span className="panel-item-note">{reason}</span> : null}
                </button>
                {manageAllowed ? (
                  <label className="skill-toggle" title={skill.enabled ? "停用 Skill" : "启用 Skill"}>
                    <input
                      type="checkbox"
                      checked={skill.enabled}
                      disabled={disabled || busy}
                      onChange={event => dispatch({
                        type: "skill-set-enabled",
                        skillId: skill.id,
                        enabled: event.currentTarget.checked,
                      })}
                    />
                    <span>{skill.enabled ? "已启用" : "已停用"}</span>
                  </label>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function filterSkills(
  items: readonly SkillSummary[],
  query: string,
): readonly SkillSummary[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(skill => {
    const haystack = `${skill.id} ${skill.name} ${skill.description} ${skill.source}`.toLowerCase()
    return haystack.includes(needle)
  })
}

/** MCP 面板：连接状态与脱敏错误；添加表单区分 stdio 与 URL/SSE。 */
function McpPanel({
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
        onRefresh={() => dispatch({ type: "panel-open", panel: "mcp" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "panel-open", panel: "mcp" })}
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

/** Status 面板：只读展示共享 runtime / connection / 当前模型摘要，不生成第二份状态叙事。 */
function StatusPanel({
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
        {runtime.gitBranch ? (
          <>
            <dt>分支</dt>
            <dd>{runtime.gitBranch}</dd>
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

/** Help 面板：列出共享 Registry 中的命令与可调用 Skill，全部只读。 */
function HelpPanel({
  snapshot,
}: {
  snapshot: WebAdapterSnapshot
}): React.ReactElement {
  const items: readonly CommandMenuItem[] = snapshot.interactive.commands
  return (
    <div className="panel help-view">
      <ul className="panel-list help-list" role="list">
        {items.map((item, index) => {
          if (item.kind === "skill") {
            return (
              <li key={`skill-${item.skill.id}-${index}`} className="panel-item help-item">
                <span className="panel-item-title">
                  <Settings aria-hidden="true" />
                  {`/skill:${item.skill.id}`}
                </span>
                <span className="panel-item-sub">{item.skill.description}</span>
              </li>
            )
          }
          const disabled = item.availability.state === "disabled"
          const reason = item.availability.state === "disabled"
            ? item.availability.reason
            : item.availability.state === "hidden"
              ? item.availability.reason
              : null
          return (
            <li key={`cmd-${item.command.id}-${index}`} className="panel-item help-item">
              <span className="panel-item-title">
                <Info aria-hidden="true" />
                {`/${item.command.name}`}
              </span>
              <span className="panel-item-sub">{item.command.description}</span>
              {reason ? <span className="panel-item-note">{reason}</span> : null}
              {!disabled && !reason ? null : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** Thread 列表的本地过滤：与 sidebar 共用同一规则。 */
function filterThreads(
  items: readonly { thread_id: string; first_message: string; latest_message: string; message_count: number }[],
  query: string,
): readonly { thread_id: string; first_message: string; latest_message: string; message_count: number }[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(item => {
    const haystack = `${item.first_message}\n${item.latest_message}`.toLowerCase()
    return haystack.includes(needle)
  })
}
