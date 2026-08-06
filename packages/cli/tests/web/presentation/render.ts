/** 共享 DOM 渲染工具：包装 createRoot + act + unmount，统一清空 document.body。 */

import { GlobalRegistrator } from "@happy-dom/global-registrator"
import { act, createElement } from "react"
import type { ReactElement } from "react"
import { createRoot, type Root } from "react-dom/client"

/**
 * 按测试文件显式注册 happy-dom 全局环境（document/window/WebSocket 等），返回解绑函数。
 * 必须在文件顶层调用并把返回值交给 afterAll：只在渲染期间替换全局，避免污染同进程内
 * 依赖真实 WebSocket / DOM 的其它测试（如 loopback 集成测试）。
 */
export function registerTestDom(): () => void {
  GlobalRegistrator.register()
  // React 19 的 act 需要显式声明测试环境，否则异步更新不会被等待。
  globalThis.IS_REACT_ACT_ENVIRONMENT = true as boolean
  return () => {
    try {
      GlobalRegistrator.unregister()
    } catch {
      // 解绑失败不阻断后续测试。
    }
  }
}

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
