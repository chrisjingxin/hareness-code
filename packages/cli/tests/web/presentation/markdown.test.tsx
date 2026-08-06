/** Markdown：GFM token 渲染、raw HTML 惰性化、非法 scheme 文本化、图片仅 alt。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"

import { Markdown } from "../../../src/web/presentation/markdown"
import { registerTestDom, render } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


describe("Markdown", () => {
  test("渲染 GFM 标题、强调、表格、任务列表、围栏代码、内联代码、链接", () => {
    const handle = render(
      <Markdown
        text={"## 标题\n\n**粗体** 与 *斜体*\n\n| 列1 | 列2 |\n| --- | --- |\n| a | b |\n\n- [x] 任务一\n- [ ] 任务二\n\n```ts\nconst x = 1\n```\n\n使用 `inline` 与 [文档](https://example.com)。"}
      />,
    )
    try {
      const html = handle.container
      expect(html.querySelector("h2")?.textContent).toBe("标题")
      expect(html.querySelector("strong")?.textContent).toBe("粗体")
      expect(html.querySelector("em")?.textContent).toBe("斜体")
      expect(html.querySelectorAll("th").length).toBe(2)
      expect(html.querySelectorAll("td").length).toBe(2)
      const taskList = html.querySelectorAll("li.markdown-task")
      expect(taskList.length).toBe(2)
      const taskInputs = html.querySelectorAll<HTMLInputElement>("li.markdown-task > input[type=checkbox]")
      expect(taskInputs.length).toBe(2)
      expect(taskInputs[0]?.checked).toBe(true)
      expect(taskInputs[1]?.checked).toBe(false)
      const fence = html.querySelector("pre.code-block-pre code")
      expect(fence?.textContent).toBe("const x = 1")
      expect(fence?.getAttribute("data-language")).toBe("ts")
      expect(html.querySelector(".markdown-code")).toBeNull()
      expect(html.querySelector("p > code")?.textContent).toBe("inline")
      const link = html.querySelector<HTMLAnchorElement>("a[href^='https://']")
      expect(link).not.toBeNull()
      expect(link?.target).toBe("_blank")
      expect(link?.rel).toContain("noopener")
      expect(link?.rel).toContain("noreferrer")
    } finally {
      handle.unmount()
    }
  })

  test("raw HTML 作为 inert 文本出现，script 与 img 永远不进入 DOM", () => {
    const handle = render(
      <Markdown
        text={"行内 HTML：<script>alert(1)</script> 与 <img src=x onerror=alert(1)> 与 <b>bold</b>。"}
      />,
    )
    try {
      const html = handle.container
      expect(html.querySelector("script")).toBeNull()
      expect(html.querySelector("img")).toBeNull()
      expect(html.querySelector("[onerror]")).toBeNull()
      // 包含 "bold" 的 DOM 节点是 span 而非 b，b 不会以元素形式出现。
      expect(html.querySelector("b")).toBeNull()
      expect(html.textContent).toContain("<script>alert(1)</script>")
      expect(html.textContent).toContain("<img src=x onerror=alert(1)>")
      expect(html.textContent).toContain("<b>bold</b>")
    } finally {
      handle.unmount()
    }
  })

  test("javascript: 与 data: 等非法 scheme 渲染为纯文本，不生成 <a>", () => {
    const handle = render(
      <Markdown
        text={"点我 [恶意](javascript:alert(1)) 或 [本地](file:///etc/passwd) 或 [数据](data:text/html,<h1>x</h1>)，再看 [合法](https://example.com)。"}
      />,
    )
    try {
      const html = handle.container
      expect(html.querySelector('a[href^="javascript:"]')).toBeNull()
      expect(html.querySelector('a[href^="file:"]')).toBeNull()
      expect(html.querySelector('a[href^="data:"]')).toBeNull()
      const allLinks = html.querySelectorAll("a")
      expect(allLinks.length).toBe(1)
      expect(allLinks[0]?.getAttribute("href")).toMatch(/^https:\/\//)
      expect(html.textContent).toContain("恶意")
      expect(html.querySelector(".markdown-unsafe-link")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("图片只渲染 alt 文本，不生成 <img> 也不发起 fetch", () => {
    const handle = render(
      <Markdown text={"![替代文本](https://example.com/secret.png)"} />,
    )
    try {
      const html = handle.container
      expect(html.querySelector("img")).toBeNull()
      const altSpan = html.querySelector(".markdown-image-alt")
      expect(altSpan).not.toBeNull()
      expect(altSpan?.textContent).toBe("替代文本")
    } finally {
      handle.unmount()
    }
  })

  test("纯文本内容始终出现在 DOM 中", () => {
    const handle = render(<Markdown text={"普通文本与\n多行\n内容。"} />)
    try {
      expect(handle.container.textContent).toContain("普通文本与")
      expect(handle.container.textContent).toContain("多行")
      expect(handle.container.textContent).toContain("内容。")
    } finally {
      handle.unmount()
    }
  })
})
