/** 共享 DOM 渲染工具：包装 createRoot + act + unmount，统一清空 document.body。 */

import { GlobalRegistrator } from "@happy-dom/global-registrator"
import { act, createElement } from "react"
import type { ReactElement } from "react"
import { createRoot, type Root } from "react-dom/client"

try {
  GlobalRegistrator.register()
} catch {
  // happy-dom 已由其它测试文件注册；全局只允许注册一次。
}

// React 19 的 act 需要显式声明测试环境，否则异步更新不会被等待。
globalThis.IS_REACT_ACT_ENVIRONMENT = true as boolean

export type RenderHandle = {
  container: HTMLDivElement
  root: Root
  unmount(): void
}

/** 挂载组件到一个干净的容器，调用方负责 unmount。 */
export function render(element: ReactElement): RenderHandle {
  const container = document.createElement("div")
  document.body.replaceChildren(container)
  const root = createRoot(container)
  act(() => {
    root.render(element)
  })
  return {
    container,
    root,
    unmount(): void {
      act(() => {
        root.unmount()
      })
      document.body.replaceChildren()
    },
  }
}

/**
 * 派发浏览器原生的受控输入事件。
 *
 * Happy DOM 与 React 19 对 textarea/input 的委托实现存在已知不兼容；调用方只能把
 * 此函数当作环境能力探针，不能以其结果替代真实浏览器输入验收。
 */
export function setControlledValue(
  element: HTMLInputElement | HTMLTextAreaElement,
  value: string,
): void {
  const proto = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set
  if (setter) setter.call(element, value)
  else element.value = value
  element.dispatchEvent(new Event("input", { bubbles: true }))
  element.dispatchEvent(new Event("change", { bubbles: true }))
}

/** 通过 createElement + 任意 props 渲染一个组件。 */
export function mount<T extends object>(component: (props: T) => ReactElement, props: T): RenderHandle {
  return render(createElement(component, props))
}
