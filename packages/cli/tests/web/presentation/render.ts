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
// @ts-expect-error 该标记是 React 测试环境约定，不在 DOM 类型中。
globalThis.IS_REACT_ACT_ENVIRONMENT = true

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

/** 触发受控 React input 的变更事件：先调用原生 setter 再 dispatch 事件。 */
export function setControlledValue(
  element: HTMLInputElement | HTMLTextAreaElement,
  value: string,
): void {
  const proto = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set
  if (!setter) {
    element.value = value
    return
  }
  setter.call(element, value)
  element.dispatchEvent(new Event("input", { bubbles: true }))
  element.dispatchEvent(new Event("change", { bubbles: true }))
}

/**
 * React 19 在 happy-dom 下的事件委托会在 value-change 检测处崩溃
 * （getInstIfValueChanged 拿不到 fiber），因此 input/keydown 统一
 * 直接调用元素上 React 注册的 handler，绕过 DOM 事件委托。
 */
type ReactProps = Record<string, unknown> & {
  onChange?: (event: ReactChangeLike) => void
  onKeyDown?: (event: ReactKeyDownLike) => void
  onClick?: () => void
}

/** 模拟 React onChange 合成事件所需的最小形状。 */
export type ReactChangeLike = {
  target: { value: string }
  currentTarget: { value: string }
}

/** 模拟 React onKeyDown 合成事件所需的最小形状。 */
export type ReactKeyDownLike = {
  key: string
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  preventDefault(): void
  nativeEvent: { isComposing: boolean }
}

function reactPropsOf(element: Element): ReactProps {
  const key = Object.keys(element).find(name => name.startsWith("__reactProps$"))
  if (!key) throw new Error(`Element has no React props: ${element.tagName}`)
  return (element as unknown as Record<string, ReactProps>)[key]
}

/** 调用元素上的 React onChange，模拟一次受控输入变更。 */
export function changeValue(element: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const props = reactPropsOf(element)
  if (props.onChange) {
    const change = { target: { value }, currentTarget: { value } }
    props.onChange(change)
    return
  }
  setControlledValue(element, value)
}

/** 调用元素上的 React onKeyDown，模拟一次按键。 */
export function pressKey(element: Element, init: KeyboardEventInit): void {
  const props = reactPropsOf(element)
  if (props.onKeyDown) {
    const event: ReactKeyDownLike = {
      key: init.key ?? "",
      shiftKey: init.shiftKey ?? false,
      ctrlKey: init.ctrlKey ?? false,
      metaKey: init.metaKey ?? false,
      preventDefault() {},
      nativeEvent: { isComposing: false },
    }
    props.onKeyDown(event)
    return
  }
  element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }))
}

/** 调用元素上的 React onClick；无 handler 时退化为原生 click。 */
export function clickElement(element: Element): void {
  const props = reactPropsOf(element)
  if (props.onClick) {
    props.onClick()
    return
  }
  element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }))
}

/** 通过 createElement + 任意 props 渲染一个组件。 */
export function mount<T extends object>(component: (props: T) => ReactElement, props: T): RenderHandle {
  return render(createElement(component, props))
}
