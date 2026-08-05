/** FilePromptHistoryStore 基础设施：基于 JSONL 磁盘文件实现 PromptHistoryStore 接口。 */

import { loadPromptHistory, persistPromptHistory, promptHistoryPath } from "../tui/application/prompt-history"
import type { PromptHistoryStore } from "../interactive/ports/prompt-history-store"

export class FilePromptHistoryStore implements PromptHistoryStore {
  private currentHistory: string[] = []

  constructor(private readonly path = promptHistoryPath()) {}

  async load(): Promise<string[]> {
    this.currentHistory = await loadPromptHistory(this.path)
    return [...this.currentHistory]
  }

  async append(entry: string): Promise<void> {
    const normalized = entry.trim()
    if (!normalized) return
    const previous = [...this.currentHistory]
    if (previous.at(-1) === normalized) return
    const next = [...previous, normalized].slice(-50)
    await persistPromptHistory(previous, next, this.path)
    this.currentHistory = next
  }
}
