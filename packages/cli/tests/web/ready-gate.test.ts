/** ready 上报门测试：只有"首帧视图 + opening-web"才发送 handoff.ready（P0 回归）。 */

import { expect, test } from "bun:test"

import { createReadyGate } from "../../src/web/app"
import type { PresentationState } from "../../src/presentation-coordinator"

function makeFakeClient() {
  const holder = { handoffState: { phase: "tui-active" } as PresentationState }
  const client = {
    readyCalls: 0,
    ready() {
      this.readyCalls += 1
    },
    getHandoffState() {
      return holder.handoffState
    },
  }
  return { client, setHandoff: (next: PresentationState) => { holder.handoffState = next } }
}

test("收到首帧视图且处于 opening-web 时发送一次 handoff.ready", () => {
  const { client, setHandoff } = makeFakeClient()
  const gate = createReadyGate(client)
  gate.onState()
  expect(client.readyCalls).toBe(0)
  setHandoff({ phase: "opening-web", handoffId: "h1" })
  gate.onHandoffState()
  expect(client.readyCalls).toBe(1)
  // 幂等：重复通知不再发送。
  gate.onHandoffState()
  gate.onState()
  expect(client.readyCalls).toBe(1)
})

test("重连（web-active）不发送 ready；状态先到也不发送", () => {
  const { client, setHandoff } = makeFakeClient()
  const gate = createReadyGate(client)
  setHandoff({ phase: "web-active", handoffId: "h1" })
  gate.onHandoffState()
  gate.onState()
  expect(client.readyCalls).toBe(0)
})

test("视图未就绪时即使处于 opening-web 也不发送 ready", () => {
  const { client, setHandoff } = makeFakeClient()
  const gate = createReadyGate(client)
  setHandoff({ phase: "opening-web", handoffId: "h1" })
  gate.onHandoffState()
  expect(client.readyCalls).toBe(0)
  gate.onState()
  expect(client.readyCalls).toBe(1)
})

test("returning-tui / tui-active 状态不发送 ready", () => {
  const { client, setHandoff } = makeFakeClient()
  const gate = createReadyGate(client)
  gate.onState()
  setHandoff({ phase: "returning-tui", handoffId: "h1", reason: "returned" })
  gate.onHandoffState()
  expect(client.readyCalls).toBe(0)
})
