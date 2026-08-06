/** Web 安全 Markdown：只把 marked token 转成 React 节点，绝不把 HTML 字符串交给 DOM。 */
/** @jsxImportSource react */

import { lexer, type Token, type Tokens } from "marked"
import type { ReactElement, ReactNode } from "react"

import { CodeBlock } from "./code-block"

/**
 * 渲染支持 GFM 的安全 Markdown。
 *
 * marked 只用于把字符串切分为 token 树；不允许的 token（raw HTML、image、非法 scheme）
 * 全部降级为纯文本或 alt 文本。模型输出不会因为 Markdown 解析而触发脚本、事件属性、
 * SVG 或远端图片请求，也不会把字符串直接交给 DOM 渲染为 HTML。
 */
export function Markdown({ text }: { text: string }): ReactElement {
  let tokens: Token[]
  try {
    tokens = lexer(text, { gfm: true })
  } catch {
    // 解析失败时仍要保留原文，避免 Agent 输出因渲染失败而丢失。
    return <div className="markdown markdown-content">{text}</div>
  }
  return <div className="markdown markdown-content">{renderBlocks(tokens, "md")}</div>
}

/** 把 block 级 token 列表逐项转换为 React 节点；空 space token 跳过以收紧 DOM。 */
function renderBlocks(tokens: readonly Token[] | undefined, prefix: string): ReactNode[] {
  if (!tokens || tokens.length === 0) return []
  const nodes: ReactNode[] = []
  tokens.forEach((token, index) => {
    const node = renderBlock(token, `${prefix}-${index}`)
    if (node !== null) nodes.push(node)
  })
  return nodes
}

function renderBlock(token: Token, key: string): ReactNode {
  switch (token.type) {
    case "space":
      return null
    case "heading": {
      const heading = token as Tokens.Heading
      const Tag = headingTag(heading.depth)
      return <Tag key={key}>{renderInline(heading.tokens, key, heading.text)}</Tag>
    }
    case "paragraph": {
      const para = token as Tokens.Paragraph
      return <p key={key}>{renderInline(para.tokens, key, para.text)}</p>
    }
    case "text": {
      const textToken = token as Tokens.Text
      return <p key={key}>{renderInline(textToken.tokens, key, textToken.text)}</p>
    }
    case "blockquote": {
      const quote = token as Tokens.Blockquote
      return <blockquote key={key}>{renderBlocks(quote.tokens, key)}</blockquote>
    }
    case "code": {
      const code = token as Tokens.Code
      return <CodeBlock key={key} code={code.text} language={code.lang} />
    }
    case "hr":
      return <hr key={key} />
    case "list": {
      const list = token as Tokens.List
      const ListTag = list.ordered ? "ol" : "ul"
      const startAttr = list.ordered && typeof list.start === "number" ? list.start : undefined
      return (
        <ListTag key={key} start={startAttr}>
          {list.items.map((item, index) => renderListItem(item, `${key}-${index}`))}
        </ListTag>
      )
    }
    case "table": {
      const table = token as Tokens.Table
      return (
        <div key={key} className="markdown-table-wrap">
          <table>
            <thead>
              <tr>
                {table.header.map((cell, index) => renderTableCell(cell, `${key}-head-${index}`, true))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, rowIndex) => (
                <tr key={`${key}-row-${rowIndex}`}>
                  {row.map((cell, cellIndex) => renderTableCell(cell, `${key}-${rowIndex}-${cellIndex}`, false))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    case "html": {
      const html = token as Tokens.HTML
      // raw HTML 永远按纯文本渲染，绝不会成为 DOM 节点。
      return <span key={key} className="markdown-raw">{html.text}</span>
    }
    case "def":
      return null
    default:
      return <span key={key}>{safeTokenText(token)}</span>
  }
}

/** list_item 单独抽出来以便把任务复选框 token 提升为可读控件。 */
function renderListItem(item: Tokens.ListItem, key: string): ReactNode {
  if (item.task) {
    const checked = item.checked === true
    return (
      <li key={key} className="markdown-task">
        <input type="checkbox" checked={checked} disabled readOnly aria-label={checked ? "已完成" : "未完成"} />
        <span>{renderInline(item.tokens, key, item.text)}</span>
      </li>
    )
  }
  return <li key={key}>{renderInline(item.tokens, key, item.text)}</li>
}

function renderTableCell(cell: Tokens.TableCell, key: string, header: boolean): ReactNode {
  const Tag = header ? "th" : "td"
  return (
    <Tag key={key} data-align={cell.align ?? undefined}>
      {renderInline(cell.tokens, key, cell.text)}
    </Tag>
  )
}

function renderInline(tokens: readonly Token[] | undefined, prefix: string, fallback = ""): ReactNode[] {
  if (!tokens || tokens.length === 0) return fallback ? [fallback] : []
  return tokens.map((token, index) => renderInlineToken(token, `${prefix}-inline-${index}`))
}

function renderInlineToken(token: Token, key: string): ReactNode {
  switch (token.type) {
    case "strong": {
      const strong = token as Tokens.Strong
      return <strong key={key}>{renderInline(strong.tokens, key, strong.text)}</strong>
    }
    case "em": {
      const em = token as Tokens.Em
      return <em key={key}>{renderInline(em.tokens, key, em.text)}</em>
    }
    case "del": {
      const del = token as Tokens.Del
      return <del key={key}>{renderInline(del.tokens, key, del.text)}</del>
    }
    case "codespan": {
      const code = token as Tokens.Codespan
      return <code key={key}>{code.text}</code>
    }
    case "br":
      return <br key={key} />
    case "checkbox": {
      const checkbox = token as Tokens.Checkbox
      return (
        <input
          key={key}
          type="checkbox"
          checked={checkbox.checked === true}
          disabled
          readOnly
          aria-label={checkbox.checked ? "已完成" : "未完成"}
        />
      )
    }
    case "link": {
      const link = token as Tokens.Link
      const children = renderInline(link.tokens, key, link.text)
      const href = safeLink(link.href)
      if (!href) return <span key={key} className="markdown-unsafe-link">{children}</span>
      return (
        <a key={key} href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      )
    }
    case "image": {
      // 图片只显示 alt 文本；不渲染 <img>，不发起任何网络请求。
      const image = token as Tokens.Image
      return <span key={key} className="markdown-image-alt">{image.text}</span>
    }
    case "html": {
      const html = token as Tokens.HTML
      return <span key={key} className="markdown-raw">{html.text}</span>
    }
    case "escape": {
      const escape = token as Tokens.Escape
      return <span key={key}>{escape.text}</span>
    }
    case "text": {
      const textToken = token as Tokens.Text
      return textToken.tokens && textToken.tokens.length > 0
        ? <span key={key}>{renderInline(textToken.tokens, key, textToken.text)}</span>
        : <span key={key}>{textToken.text}</span>
    }
    default:
      return <span key={key}>{safeTokenText(token)}</span>
  }
}

/** heading depth 固定到 h2~h6；h1 升级为 h2 避免和页面主标题重复。 */
function headingTag(depth: number): "h2" | "h3" | "h4" | "h5" | "h6" {
  if (depth <= 2) return "h2"
  if (depth === 3) return "h3"
  if (depth === 4) return "h4"
  if (depth === 5) return "h5"
  return "h6"
}

/**
 * 校验链接 scheme：只允许 http(s) 与 mailto；其它协议（如 javascript:、data:、file:）
 * 一律降级为普通文本，避免模型输出触发脚本或本地文件读取。
 */
function safeLink(value: string): string | undefined {
  const trimmed = value.trim()
  if (!trimmed) return undefined
  try {
    const url = new URL(trimmed)
    if (url.protocol !== "http:" && url.protocol !== "https:" && url.protocol !== "mailto:") return undefined
    return url.href
  } catch {
    // 相对路径或无法解析的 URL 视作不安全；只渲染文本，不生成 <a>。
    return undefined
  }
}

function safeTokenText(token: Token): string {
  const value = token as { text?: unknown; raw?: unknown }
  return typeof value.text === "string" ? value.text : typeof value.raw === "string" ? value.raw : ""
}
