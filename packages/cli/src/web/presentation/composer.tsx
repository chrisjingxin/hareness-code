/** Web Composer：textarea 自动增长、Skill chip、命令菜单、rail 状态控件（审批/模型下拉）、发送/取消按钮。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import { AlertTriangle, Bot, ChevronDown, Loader2, Lock, Send, ShieldCheck, Wrench, X } from "lucide-react"

import {
  commandMenuItemDescription,
  commandMenuItemLabel,
  type CommandMenuItem,
} from "../../interactive/commands"
import { APPROVAL_MODE_CYCLE, approvalModeLabel } from "../../interactive/runtime"
import { selectNavigationView } from "../../interactive/selectors"
import { modelSelectionLabel } from "../../presentation-shared"
import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/** Composer 自动增长行数上限；超过后由 CSS 控制内部滚动。 */
const COMPOSER_MAX_ROWS = 8
/** 单行最小可见高度；用于初始空 draft 的高度。 */
const COMPOSER_MIN_ROWS = 1
/** 字符数估算到行数的换算系数；不追求精确，符合 Composer 表现。 */
const CHARS_PER_ROW = 48

/**
 * Web Composer：textarea + 底部 action rail（左：审批模式下拉 + 工作模式 chip + Skill chip；右：模型下拉 + 发送/取消）。
 * 审批/模型下拉替代原顶栏 chip 与只读 mode-chip：显示当前值，点击就地切换（参考 DSH rail 布局，2026-08-18 与用户确认）。
 * 工作模式 chip 是界面唯一模式展示：未锁定提示 Tab 切换，锁定显示锁图标与冻结模式。
 *
 * - 受控输入：textarea 的值始终从 snapshot.draft 读取，onChange 派发 draft-change。
 * - 自动增长：按字符数估算行数，clamp 到 [1, 8]；Esc/Ctrl+K 走对应意图。
 * - 命令菜单：snapshot.commandMenuOpen 为真且 snapshot.commandOptions 非空时渲染。
 * - 发送/取消：activeRun 时显示取消按钮；其他时候显示发送按钮，不同时展示两个主动作。
 */
export function Composer(props: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const { snapshot, dispatch, disabled } = props
  const interactive = snapshot.interactive
  const activeRun = Boolean(interactive.activeRun)
  const compacting = interactive.activity.kind === "compacting"
  const connectionOpen = interactive.connection.status === "open"
  const composedDisabled = Boolean(disabled) || snapshot.leaving || !connectionOpen || snapshot.composerSubmitting || compacting
  // 工作模式 chip 是 rail 中唯一模式展示：Thread 冻结（threadMode 非空）时锁定为冻结模式，
  // 否则跟随当前 workMode 并在 title 提示 Tab 可切换。
  const workModeLocked = interactive.threadMode !== null
  const displayedWorkMode = workModeLocked ? interactive.threadMode : interactive.workMode
  const workModeLabel = displayedWorkMode === "compose" ? "Compose" : "Build"

  // Rail 下拉：审批模式在待处理交互或压缩中锁定；运行中仍可改下一轮档位。
  const { availability } = selectNavigationView(interactive)
  const modelsCatalog = interactive.catalogs.models
  const modelSelectedId = interactive.selection.requestedModelProfileId ?? interactive.selection.actualModel?.id ?? null
  const approvalDisabled = composedDisabled || Boolean(interactive.interaction)
  const [approvalMenuOpen, setApprovalMenuOpen] = useState(false)
  const [modelMenuOpen, setModelMenuOpen] = useState(false)
  const approvalControlRef = useRef<HTMLDivElement | null>(null)
  const approvalMenuRef = useRef<HTMLDivElement | null>(null)
  const modelControlRef = useRef<HTMLDivElement | null>(null)
  const modelMenuRef = useRef<HTMLDivElement | null>(null)

  // 打开下拉后焦点进入当前选项（无当前项则第一个可用项），键盘用户直接上下选择。
  useEffect(() => {
    if (!approvalMenuOpen) return
    approvalMenuRef.current?.querySelector<HTMLElement>('[role="menuitemradio"][aria-checked="true"]')?.focus()
  }, [approvalMenuOpen])

  useEffect(() => {
    if (!modelMenuOpen) return
    const current = modelMenuRef.current?.querySelector<HTMLElement>('[role="menuitemradio"][aria-checked="true"]')
    const fallback = modelMenuRef.current?.querySelector<HTMLElement>('[role="menuitemradio"]:not(:disabled), [role="menuitem"]:not(:disabled)')
    ;(current ?? fallback)?.focus()
  }, [modelMenuOpen])

  // 点击菜单外任意位置关闭下拉。
  useEffect(() => {
    if (!approvalMenuOpen) return
    const onPointerDown = (event: MouseEvent) => {
      if (!approvalControlRef.current?.contains(event.target as Node)) setApprovalMenuOpen(false)
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [approvalMenuOpen])

  useEffect(() => {
    if (!modelMenuOpen) return
    const onPointerDown = (event: MouseEvent) => {
      if (!modelControlRef.current?.contains(event.target as Node)) setModelMenuOpen(false)
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [modelMenuOpen])

  // 只读/锁定时收起下拉。
  useEffect(() => {
    if (composedDisabled) {
      setApprovalMenuOpen(false)
      setModelMenuOpen(false)
    }
  }, [composedDisabled])

  /** 打开/关闭模型下拉；打开时请求刷新 models 目录（不开 Dock）。 */
  const toggleModelMenu = () => {
    const next = !modelMenuOpen
    setModelMenuOpen(next)
    if (next) dispatch({ type: "models-catalog-refresh" })
  }

  /** 两个下拉共用 APG 习惯：方向键在可用项间循环，Escape 关闭并焦点回到触发按钮。 */
  const handleRailMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault()
      event.stopPropagation()
      const inModelMenu = modelMenuRef.current?.contains(event.currentTarget) ?? false
      setApprovalMenuOpen(false)
      setModelMenuOpen(false)
      const trigger = inModelMenu
        ? modelControlRef.current?.querySelector<HTMLButtonElement>(".rail-chip")
        : approvalControlRef.current?.querySelector<HTMLButtonElement>(".rail-chip")
      trigger?.focus()
      return
    }
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]:not(:disabled), [role="menuitem"]:not(:disabled)'))
    if (items.length === 0) return
    const currentIndex = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement))
    let nextIndex: number | null = null
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = items.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    items[nextIndex]?.focus()
  }

  const draft = snapshot.draft
  const armedSkill = interactive.selection.armedSkill
  const menuVisible = snapshot.commandMenuOpen && draft.length > 0
  const items = snapshot.commandOptions
  const selectedIndex = clampIndex(snapshot.commandMenuIndex, items.length)

  const isComposingRef = useRef(false)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [rows, setRows] = useState(COMPOSER_MIN_ROWS)

  useEffect(() => {
    setRows(estimateRows(draft))
  }, [draft])

  // 新建 Thread 成功后 Adapter 递增 composerFocusRequest，焦点回到输入框。
  useEffect(() => {
    if (snapshot.composerFocusRequest > 0) textareaRef.current?.focus()
  }, [snapshot.composerFocusRequest])

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    dispatch({ type: "draft-change", value: event.target.value })
  }

  const handleCompositionStart = () => {
    isComposingRef.current = true
  }

  const handleCompositionEnd = () => {
    isComposingRef.current = false
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const isComposing = event.nativeEvent.isComposing || isComposingRef.current || event.keyCode === 229
    const resolved = resolveComposerKeyboardIntent({
      key: event.key,
      shiftKey: event.shiftKey,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      isComposing,
      menuVisible,
      items,
      selectedIndex,
      draft,
      composedDisabled,
      activeRun,
    })
    if (resolved.preventDefault) {
      event.preventDefault()
    }
    if (resolved.intent) dispatch(resolved.intent)
  }

  const handleFormSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!composedDisabled && draft.trim().length > 0) {
      dispatch({ type: "submit" })
    }
  }

  return (
    <div className="composer-bar">
      <form className="composer-inner" onSubmit={handleFormSubmit}>
        {menuVisible ? (
          <CommandMenu
            items={items}
            selectedIndex={selectedIndex}
            dispatch={dispatch}
          />
        ) : null}
        <div className="composer-box">
          <div className="composer-tabs" aria-hidden="true">
            <span className="composer-tab is-active">聊天</span>
          </div>
          <textarea
            ref={textareaRef}
            className="composer-textarea"
            rows={rows}
            value={draft}
            onChange={handleChange}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            onKeyDown={handleKeyDown}
            disabled={composedDisabled && !activeRun}
            placeholder={placeholderFor(snapshot, activeRun)}
            aria-label="消息，Enter 发送，Shift+Enter 换行"
          />
          <div className="composer-rail">
            <div className="composer-rail-left">
              {/* 审批模式下拉：rail 左端的状态切换控件，替代原只读 mode-chip。 */}
              <div ref={approvalControlRef} className="composer-rail-control">
                <button
                  type="button"
                  className="rail-chip composer-approval"
                  disabled={approvalDisabled}
                  title={`选择审批模式（${APPROVAL_MODE_CYCLE.join("、")}）`}
                  aria-label={`选择审批模式，当前：${approvalModeLabel(interactive.runtime)}`}
                  aria-haspopup="menu"
                  aria-expanded={approvalMenuOpen}
                  aria-controls="composer-approval-menu"
                  onClick={() => { setApprovalMenuOpen(open => !open) }}
                >
                  <ShieldCheck aria-hidden="true" size={15} />
                  <span className="rail-chip-label">{approvalModeLabel(interactive.runtime)}</span>
                  <ChevronDown aria-hidden="true" className="rail-chip-chevron" size={13} />
                </button>
                {approvalMenuOpen ? (
                  <div ref={approvalMenuRef} id="composer-approval-menu" className="composer-menu composer-menu-start" role="menu" aria-label="选择审批模式" onKeyDown={handleRailMenuKeyDown}>
                    {APPROVAL_MODE_CYCLE.map(mode => (
                      <button
                        key={mode}
                        type="button"
                        role="menuitemradio"
                        className="composer-menu-option"
                        aria-checked={approvalModeLabel(interactive.runtime) === mode}
                        onClick={() => {
                          setApprovalMenuOpen(false)
                          void dispatch({ type: "approval-mode-select", mode })
                        }}
                      >
                        <span>{mode}</span>
                        {approvalModeLabel(interactive.runtime) === mode ? <span aria-hidden="true">✓</span> : null}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
              <span
                className={`mode-chip${displayedWorkMode === "compose" ? " mode-chip-active" : ""}`}
                role="status"
                title={workModeLocked ? `工作模式已锁定为 ${workModeLabel}，Thread 内不可切换` : "工作模式：空闲时 Tab 切换"}
              >
                {workModeLocked ? <Lock aria-hidden="true" /> : null}
                {workModeLabel}
              </span>
              {armedSkill ? (
                <span className="skill-chip" role="status">
                  <Wrench aria-hidden="true" className="skill-chip-icon" />
                  <span className="skill-chip-label">Skill</span>
                  <span className="skill-chip-name">{armedSkill.name}</span>
                  <button
                    type="button"
                    className="skill-chip-clear"
                    aria-label="取消已选择 Skill"
                    disabled={composedDisabled}
                    onClick={() => dispatch({ type: "skill-clear" })}
                  >
                    <X aria-hidden="true" />
                  </button>
                </span>
              ) : null}
            </div>
            <div className="composer-rail-right">
              {/* 模型下拉：发送键旁的就地切换（参考 DSH）；管理仍由 Dock Models 面板承担。 */}
              {availability.canOpenModelsPanel ? (
                <div ref={modelControlRef} className="composer-rail-control">
                  <button
                    type="button"
                    className="rail-chip composer-model"
                    disabled={composedDisabled}
                    title="选择模型"
                    aria-label={`选择模型，当前：${modelSelectionLabel(interactive)}`}
                    aria-haspopup="menu"
                    aria-expanded={modelMenuOpen}
                    aria-controls="composer-model-menu"
                    onClick={toggleModelMenu}
                  >
                    <Bot aria-hidden="true" size={15} />
                    <span className="rail-chip-label">{modelSelectionLabel(interactive)}</span>
                    <ChevronDown aria-hidden="true" className="rail-chip-chevron" size={13} />
                  </button>
                  {modelMenuOpen ? (
                    <div ref={modelMenuRef} id="composer-model-menu" className="composer-menu composer-menu-end" role="menu" aria-label="选择模型" onKeyDown={handleRailMenuKeyDown}>
                      {modelsCatalog.status === "loading" && modelsCatalog.items.length === 0 ? (
                        <p className="composer-menu-status">正在读取 Model…</p>
                      ) : modelsCatalog.status === "error" && modelsCatalog.items.length === 0 ? (
                        <p className="composer-menu-status">{modelsCatalog.message || "读取 Model 失败"}</p>
                      ) : modelsCatalog.items.length === 0 ? (
                        <p className="composer-menu-status">暂无可用模型</p>
                      ) : (
                        modelsCatalog.items.map(profile => {
                          const isCurrent = profile.id === modelSelectedId
                          const optionDisabled = composedDisabled || activeRun || !profile.available
                          return (
                            <button
                              key={profile.id}
                              type="button"
                              role="menuitemradio"
                              className="composer-menu-option model-option"
                              aria-checked={isCurrent}
                              disabled={optionDisabled}
                              title={!profile.available ? (profile.unavailable_reason ?? "当前不可用") : undefined}
                              onClick={() => {
                                setModelMenuOpen(false)
                                void dispatch({ type: "model-select", profileId: profile.id })
                              }}
                            >
                              <span className="model-option-copy">
                                <span className="model-option-id">{profile.id}</span>
                                <span className="model-option-sub">{profile.provider_label} · {profile.model}</span>
                              </span>
                              {isCurrent ? <span aria-hidden="true" className="model-option-check">✓</span> : null}
                            </button>
                          )
                        })
                      )}
                      <button
                        type="button"
                        role="menuitem"
                        className="composer-menu-option composer-menu-manage"
                        onClick={() => {
                          setModelMenuOpen(false)
                          dispatch({ type: "dock-open", panel: "models" })
                        }}
                      >
                        管理模型…
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : null}
              {activeRun ? (
                <button
                  type="button"
                  className="cancel-button"
                  aria-label="取消当前任务"
                  title="取消当前任务"
                  onClick={() => dispatch({ type: "cancel-run" })}
                  disabled={snapshot.leaving}
                >
                  {/* 设计稿停止形态：实心圆角方块（2026-08-18 用户设计稿），非 lucide 描边 Square。 */}
                  <svg aria-hidden="true" className="cancel-stop-icon" viewBox="0 0 24 24">
                    <rect x="5" y="5" width="14" height="14" rx="3.5" fill="currentColor" />
                  </svg>
                </button>
              ) : (
                <button
                  type="submit"
                  className="send-button"
                  aria-label="发送消息"
                  title="发送消息"
                  disabled={composedDisabled || draft.trim().length === 0}
                >
                  {snapshot.composerSubmitting ? (
                    <Loader2 aria-hidden="true" className="cancel-button-spinner" />
                  ) : (
                    <Send aria-hidden="true" />
                  )}
                </button>
              )}
            </div>
          </div>
        </div>
        {snapshot.composerError ? (
          <p className="composer-status composer-status-error" role="alert">
            <AlertTriangle aria-hidden="true" />
            <span>{snapshot.composerError}</span>
          </p>
        ) : composedDisabled && !activeRun ? (
          <p className="composer-status" role="status">
            <AlertTriangle aria-hidden="true" />
            <span>{disabledReason(snapshot, Boolean(disabled))}</span>
          </p>
        ) : null}
      </form>
    </div>
  )
}

/** 渲染命令菜单；list 为空时显示“无可用命令”提示。 */
function CommandMenu(props: {
  items: readonly CommandMenuItem[]
  selectedIndex: number
  dispatch: (intent: WebIntent) => void
}): React.ReactElement {
  const { items, selectedIndex, dispatch } = props
  if (items.length === 0) {
    return (
      <div className="command-menu" role="listbox" aria-label="命令菜单">
        <p className="command-menu-empty">无匹配命令</p>
      </div>
    )
  }
  return (
    <div className="command-menu" role="listbox" aria-label="命令菜单">
      {items.map((item, index) => {
        const selected = index === selectedIndex
        const disabled = isItemDisabled(item)
        const label = commandMenuItemLabel(item)
        const description = commandMenuItemDescription(item)
        return (
          <button
            type="button"
            key={commandItemKey(item, index)}
            role="option"
            aria-selected={selected}
            aria-disabled={disabled}
            data-selected={selected}
            data-disabled={disabled}
            className="command-item"
            onMouseEnter={() => dispatch({ type: "command-menu-hover", selectedIndex: index })}
            onClick={() => {
              if (disabled) return
              dispatch({ type: "command-menu-select", item })
            }}
          >
            <span className="command-item-label">{label}</span>
            <span className="command-item-description">{description}</span>
            {disabled ? (
              <span className="command-item-warning" aria-hidden="true">
                <AlertTriangle />
              </span>
            ) : null}
            {selected ? (
              <span className="command-item-caret" aria-hidden="true">
                <ChevronDown />
              </span>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}

function clampIndex(index: number, length: number): number {
  if (length === 0) return 0
  if (index < 0) return 0
  if (index >= length) return length - 1
  return index
}

function isItemDisabled(item: CommandMenuItem): boolean {
  return item.kind === "command" && item.availability.state === "disabled"
}

function commandItemKey(item: CommandMenuItem, index: number): string {
  if (item.kind === "command") return `cmd-${item.command.id}-${index}`
  return `skill-${item.skill.id}-${index}`
}

/** Composer 键盘状态机：独立于 DOM，保证 IME 的 Enter 永远不提交或选中命令。 */
export function resolveComposerKeyboardIntent(
  input: {
    key: string
    shiftKey: boolean
    ctrlKey: boolean
    metaKey: boolean
    isComposing: boolean
    menuVisible: boolean
    items: readonly CommandMenuItem[]
    selectedIndex: number
    draft: string
    composedDisabled: boolean
    activeRun: boolean
  },
): { preventDefault: boolean; intent: WebIntent | null } {
  if (input.isComposing) return { preventDefault: false, intent: null }
  if (input.menuVisible) {
    const menuResolution = resolveMenuKeyboardIntent(
      input.key,
      input.items,
      input.selectedIndex,
    )
    if (menuResolution.handled) return { preventDefault: true, intent: menuResolution.intent }
  }
  if (input.key === "Enter" && !input.shiftKey) {
    return {
      preventDefault: true,
      intent: !input.composedDisabled && input.draft.trim().length > 0 ? { type: "submit" } : null,
    }
  }
  if (input.key === "Escape" && input.activeRun) {
    return { preventDefault: true, intent: { type: "cancel-run" } }
  }
  if ((input.metaKey || input.ctrlKey) && (input.key === "k" || input.key === "K")) {
    return { preventDefault: true, intent: { type: "command-menu-open" } }
  }
  // 空闲无浮层且未输入时 Tab 切换 Work Mode；运行中/输入中保留默认行为。
  if (input.key === "Tab" && !input.shiftKey && !input.activeRun && input.draft.trim().length === 0) {
    return { preventDefault: true, intent: { type: "work-mode-cycle" } }
  }
  return { preventDefault: false, intent: null }
}

/** 命令菜单已打开时的键盘解析；无候选项则交还给 textarea 默认行为。 */
function resolveMenuKeyboardIntent(
  key: string,
  items: readonly CommandMenuItem[],
  selectedIndex: number,
): { handled: boolean; intent: WebIntent | null } {
  if (items.length === 0) return { handled: false, intent: null }
  if (key === "ArrowDown") {
    const next = selectedIndex + 1 >= items.length ? 0 : selectedIndex + 1
    return { handled: true, intent: { type: "command-menu-hover", selectedIndex: next } }
  }
  if (key === "ArrowUp") {
    const next = selectedIndex - 1 < 0 ? items.length - 1 : selectedIndex - 1
    return { handled: true, intent: { type: "command-menu-hover", selectedIndex: next } }
  }
  if (key === "Enter" || key === "Tab") {
    const item = items[selectedIndex]
    return {
      handled: true,
      intent: item && !isItemDisabled(item) ? { type: "command-menu-select", item } : null,
    }
  }
  if (key === "Escape") {
    return { handled: true, intent: { type: "command-menu-close" } }
  }
  return { handled: false, intent: null }
}

function estimateRows(draft: string): number {
  if (draft.length === 0) return COMPOSER_MIN_ROWS
  const lineBreaks = draft.split("\n").length
  const width = Math.ceil(draft.length / CHARS_PER_ROW)
  return Math.max(
    COMPOSER_MIN_ROWS,
    Math.min(COMPOSER_MAX_ROWS, Math.max(lineBreaks, width)),
  )
}

function placeholderFor(snapshot: WebAdapterSnapshot, activeRun: boolean): string {
  if (snapshot.leaving) return "正在归还或退出，输入已锁定"
  if (snapshot.interactive.connection.status !== "open") return "等待连接…"
  if (snapshot.interactive.activity.kind === "compacting") return "正在压缩上下文…"
  if (activeRun) return "正在执行；Esc 可中断"
  return "输入消息…（输入 / 唤起命令）"
}

function disabledReason(snapshot: WebAdapterSnapshot, propDisabled: boolean): string {
  if (propDisabled) return "接管尚未完成，请稍候"
  if (snapshot.leaving) return "正在归还控制权"
  if (snapshot.interactive.activity.kind === "compacting") return "上下文压缩中，请稍候"
  return "连接尚未就绪"
}
