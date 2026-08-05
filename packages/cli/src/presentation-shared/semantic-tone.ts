/** 跨端共享语义 tone：只有语义枚举与中文名，具体色值由 TUI/Web 各自实现。 */

/** 两端统一的语义色枚举；TUI theme 与 Web CSS token 都从这里取语义名。 */
export type SemanticTone = "default" | "muted" | "accent" | "success" | "warning" | "danger"

export const SEMANTIC_TONES: readonly SemanticTone[] = ["default", "muted", "accent", "success", "warning", "danger"]

const TONE_LABELS: Readonly<Record<SemanticTone, string>> = {
  default: "默认",
  muted: "次要",
  accent: "强调",
  success: "成功",
  warning: "警告",
  danger: "危险",
}

/** 语义 tone 的中文名；未知语义安全回退到默认。 */
export function toneLabel(tone: SemanticTone): string {
  return TONE_LABELS[tone] ?? TONE_LABELS.default
}
