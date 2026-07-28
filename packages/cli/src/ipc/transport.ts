/** transport-neutral JSON-RPC seam 与异步消息队列。 */

import type { JsonRpcMessage } from "@za38/protocol"

export interface RpcTransport {
  readonly messages: AsyncIterable<unknown>
  send(message: JsonRpcMessage): Promise<void>
  close(): Promise<void>
}

/** transport 与语义 client 共用的最小异步队列。 */
export class AsyncQueue<T> implements AsyncIterable<T> {
  private readonly values: T[] = []
  private readonly waiters: Array<{
    resolve: (result: IteratorResult<T>) => void
    reject: (error: Error) => void
  }> = []
  private ended = false
  private failure: Error | undefined

  push(value: T): void {
    if (this.ended) return
    const waiter = this.waiters.shift()
    if (waiter) waiter.resolve({ value, done: false })
    else this.values.push(value)
  }

  end(): void {
    if (this.ended) return
    this.ended = true
    for (const waiter of this.waiters.splice(0)) waiter.resolve({ value: undefined, done: true })
  }

  fail(error: Error): void {
    if (this.ended) return
    this.failure = error
    this.ended = true
    for (const waiter of this.waiters.splice(0)) waiter.reject(error)
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: async () => {
        if (this.values.length > 0) return { value: this.values.shift()!, done: false }
        if (this.failure) throw this.failure
        if (this.ended) return { value: undefined, done: true }
        return new Promise<IteratorResult<T>>((resolve, reject) => this.waiters.push({ resolve, reject }))
      },
    }
  }
}
