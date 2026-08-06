/** 文件元信息格式化：大小与语言标签（语言标签语义与 workspace/file-language 一致）。 */

/** 文件大小格式化：KiB 单精度（设计口径）。 */
export function formatFileSize(sizeBytes: number): string {
  return `${(sizeBytes / 1024).toFixed(1)} KiB`
}

/**
 * 语言展示标签：canonical 语言 id 原样展示，未知（null/plaintext）显示"文本"。
 * 与 workspace/file-language 的 fileLanguageLabel 行为一致；表现层不直接 import workspace。
 */
export function fileLanguageDisplayLabel(language: string | null): string {
  return language ?? "文本"
}
