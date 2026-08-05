/** PromptHistoryStore Port：定义提示词历史加载与追加的可注入抽象。 */

export interface PromptHistoryStore {
  /** 加载有序历史提示词列表。 */
  load(): Promise<string[]>
  /** 追加一条新的提示词到历史。 */
  append(entry: string): Promise<void>
}
