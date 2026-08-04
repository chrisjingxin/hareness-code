/** Web Syntax Worker 消息通信契约与 Token 范围定义。 */

export type SyntaxScope =
  | "comment"
  | "keyword"
  | "function"
  | "variable"
  | "string"
  | "number"
  | "type"
  | "operator"
  | "punctuation"
  | "tag"
  | "attribute"
  | "constant"
  | "plain"

export type SyntaxSpan = {
  readonly startByte: number
  readonly endByte: number
  readonly scope: SyntaxScope
}

export type SyntaxWorkerRequest =
  | { type: "highlight"; requestId: number; language: string; code: string }
  | { type: "dispose" }

export type SyntaxWorkerResponse =
  | { type: "highlighted"; requestId: number; language: string; spans: readonly SyntaxSpan[] }
  | { type: "plain"; requestId: number; reason: "unknown-language" | "too-large" | "load-failed" | "parse-failed" | "timeout" }
