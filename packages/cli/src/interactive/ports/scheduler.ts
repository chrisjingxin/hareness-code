/** Scheduler Port：定义定时任务与延迟调度的可注入抽象。 */

export interface Scheduler {
  /** 安排一个在指定毫秒后执行的回调函数；返回用于取消该定时任务的取消函数。 */
  setTimeout(callback: () => void, ms: number): () => void
}
