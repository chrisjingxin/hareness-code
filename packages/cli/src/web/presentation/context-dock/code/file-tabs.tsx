/** 文件 Tab 条：横向滚动；点击激活，× 关闭；空列表不渲染。 */
/** @jsxImportSource react */

import { X } from "lucide-react"

import type { WorkspaceFileTab, WebIntent } from "../../../application/adapter"

/** 渲染已打开文件的 Tab 条；激活 Tab 高亮，关闭按钮 dispatch workspace-file-tab-close。 */
export function FileTabs({
  tabs,
  activePath,
  dispatch,
  disabled = false,
}: {
  tabs: readonly WorkspaceFileTab[]
  activePath: string | null
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  if (tabs.length === 0) return <></>
  return (
    <div className="file-tabs" role="tablist" aria-label="已打开的文件">
      {tabs.map(tab => {
        const isActive = tab.path === activePath
        return (
          <div
            key={tab.path}
            role="tab"
            aria-selected={isActive}
            tabIndex={disabled ? -1 : 0}
            className={`file-tab${isActive ? " is-active" : ""}`}
            data-path={tab.path}
            title={tab.path}
            onClick={event => {
              event.stopPropagation()
              if (!disabled) dispatch({ type: "workspace-file-tab-select", path: tab.path })
            }}
            onKeyDown={event => {
              if (disabled) return
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault()
                dispatch({ type: "workspace-file-tab-select", path: tab.path })
              }
            }}
          >
            <span className="file-tab-name">{tab.name}</span>
            <button
              type="button"
              className="file-tab-close"
              aria-label={`关闭 ${tab.name}`}
              title="关闭"
              disabled={disabled}
              onClick={event => {
                event.stopPropagation()
                dispatch({ type: "workspace-file-tab-close", path: tab.path })
              }}
            >
              <X aria-hidden="true" size={12} />
            </button>
          </div>
        )
      })}
    </div>
  )
}
