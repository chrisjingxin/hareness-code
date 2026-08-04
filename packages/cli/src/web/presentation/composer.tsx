/** Web Composer：textarea 自动增长、Skill chip、命令菜单、发送/取消按钮。 */
/** @jsxImportSource react */

import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { AlertTriangle, ChevronDown, Loader2, Send, Wrench, X } from "lucide-react"

import {
  commandMenuItemDescription,
  commandMenuItemLabel,
  type CommandMenuItem,
} from "../../interactive/commands"
import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/** Composer 自动增长行数上限；超过后由 CSS 控制内部滚动。 */
const COMPOSER_MAX_ROWS = 8
/** 单行最小可见高度；用于初始空 draft 的高度。 */
const COMPOSER_MIN_ROWS = 1
/** 字符数估算到行数的换算系数；不追求精确，符合 Composer 表现。 */
const CHARS_PER_ROW = 48

/**
 * Web Composer：textarea + 底部 action rail（Skill chip / 键盘提示 / 发送-取消）。
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
  const connectionOpen = interactive.connection.status === "open"
  const composedDisabled = Boolean(disabled) || snapshot.leaving || !connectionOpen || snapshot.composerSubmitting

  const draft = snapshot.draft
  const armedSkill = interactive.selection.armedSkill
  const menuVisible = snapshot.commandMenuOpen && draft.length > 0
  const items = snapshot.commandOptions
  const selectedIndex = clampIndex(snapshot.commandMenuIndex, items.length)

  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const isComposingRef = useRef(false)
  const [rows, setRows] = useState(COMPOSER_MIN_ROWS)

  useEffect(() => {
    setRows(estimateRows(draft))
  }, [draft])

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
    if (menuVisible && handleMenuKeyDown(event, items, selectedIndex, dispatch)) {
      event.preventDefault()
      return
    }
    const isComposing = event.nativeEvent.isComposing || isComposingRef.current || event.keyCode === 229
    if (event.key === "Enter" && !event.shiftKey && !isComposing) {
      event.preventDefault()
      if (!composedDisabled && draft.trim().length > 0) {
        dispatch({ type: "submit" })
      }
      return
    }
    if (event.key === "Escape") {
      if (menuVisible) {
        event.preventDefault()
        dispatch({ type: "command-menu-close" })
        return
      }
      if (activeRun) {
        event.preventDefault()
        dispatch({ type: "cancel-run" })
      }
      return
    }
    if (isCmdOrCtrl(event) && (event.key === "k" || event.key === "K")) {
      event.preventDefault()
      dispatch({ type: "command-menu-open" })
    }
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
              <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>
            </div>
            <div className="composer-rail-right">
              {activeRun ? (
                <button
                  type="button"
                  className="cancel-button"
                  aria-label="取消当前任务"
                  onClick={() => dispatch({ type: "cancel-run" })}
                  disabled={snapshot.leaving}
                >
                  <Loader2 aria-hidden="true" className="cancel-button-spinner" />
                  <span>取消</span>
                </button>
              ) : (
                <button
                  type="submit"
                  className="send-button"
                  aria-label="发送消息"
                  disabled={composedDisabled || draft.trim().length === 0}
                >
                  {snapshot.composerSubmitting ? (
                    <Loader2 aria-hidden="true" className="cancel-button-spinner" />
                  ) : (
                    <Send aria-hidden="true" />
                  )}
                  <span>发送</span>
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

function handleMenuKeyDown(
  event: React.KeyboardEvent<HTMLTextAreaElement>,
  items: readonly CommandMenuItem[],
  selectedIndex: number,
  dispatch: (intent: WebIntent) => void,
): boolean {
  if (items.length === 0) return false
  if (event.key === "ArrowDown") {
    const next = selectedIndex + 1 >= items.length ? 0 : selectedIndex + 1
    dispatch({ type: "command-menu-hover", selectedIndex: next })
    return true
  }
  if (event.key === "ArrowUp") {
    const next = selectedIndex - 1 < 0 ? items.length - 1 : selectedIndex - 1
    dispatch({ type: "command-menu-hover", selectedIndex: next })
    return true
  }
  if (event.key === "Enter") {
    const item = items[selectedIndex]
    if (item && !isItemDisabled(item)) {
      dispatch({ type: "command-menu-select", item })
    }
    return true
  }
  if (event.key === "Escape") {
    dispatch({ type: "command-menu-close" })
    return true
  }
  return false
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

function isCmdOrCtrl(event: React.KeyboardEvent<HTMLTextAreaElement>): boolean {
  return event.metaKey || event.ctrlKey
}

function placeholderFor(snapshot: WebAdapterSnapshot, activeRun: boolean): string {
  if (snapshot.leaving) return "正在归还或退出，输入已锁定"
  if (snapshot.interactive.connection.status !== "open") return "等待连接…"
  if (activeRun) return "正在执行；Esc 可中断"
  return "输入消息…（输入 / 唤起命令）"
}

function disabledReason(snapshot: WebAdapterSnapshot, propDisabled: boolean): string {
  if (propDisabled) return "接管尚未完成，请稍候"
  if (snapshot.leaving) return "正在归还控制权"
  return "连接尚未就绪"
}
