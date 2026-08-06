/**
 * 文件语言判定：按扩展名收敛为 canonical 语言 id 与展示标签。
 *
 * 复用 presentation-shared 语言目录的纯函数（先例 timeline-presenter），
 * 未知/无扩展名统一降级为 plaintext（null / "文本"）。
 */

import { resolveLanguage } from "../presentation-shared/language-catalog"

/** canonical 语言 id；未知（plaintext）为 null，界面按纯文本渲染。 */
export function fileLanguageId(relativePath: string): string | null {
  const entry = resolveLanguage(relativePath.split(".").at(-1) ?? "")
  return entry.canonical === "plaintext" ? null : entry.canonical
}

/** 展示标签：canonical id；未知语言显示"文本"。 */
export function fileLanguageLabel(relativePath: string): string {
  return fileLanguageId(relativePath) ?? "文本"
}
