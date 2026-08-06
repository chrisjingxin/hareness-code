/** 代码高亮内容渲染：HighlightedCode（纯内容 + 语法 span，供行号容器包裹）。 */
/** @jsxImportSource react */

import { renderSpans } from "../code-block"
import { useSyntaxHighlight } from "./use-syntax-highlight"

/** 渲染高亮后的代码内容；加载中/plaintext/失败时直接输出纯文本（plain-first）。 */
export function HighlightedCode({
  code,
  language,
  theme,
}: {
  code: string
  language: string | null
  theme: string
}): React.ReactElement {
  const highlight = useSyntaxHighlight({ code, language, theme })
  if (highlight.status === "highlighted" && highlight.spans) {
    return <>{renderSpans(code, highlight.spans)}</>
  }
  return <>{code}</>
}
