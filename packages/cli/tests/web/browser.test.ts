/** 系统 Browser opener：spawn/error 传播与平台参数测试。 */

import { expect, test } from "bun:test"
import { EventEmitter } from "node:events"
import type { ChildProcess } from "node:child_process"

import {
  createSystemBrowserOpener,
  type SpawnAdapter,
} from "../../src/web/browser"

function fakeChild(): ChildProcess & EventEmitter {
  const child = new EventEmitter() as ChildProcess & EventEmitter
  child.unref = () => child
  return child
}

function captureSpawn(): {
  adapter: SpawnAdapter
  calls: Array<{ command: string; args: readonly string[] }>
} {
  const calls: Array<{ command: string; args: readonly string[] }> = []
  return {
    calls,
    adapter: (command, args, _options) => {
      calls.push({ command, args })
      const child = fakeChild()
      process.nextTick(() => child.emit("spawn"))
      return child
    },
  }
}

test("spawn 成功后 opener resolve，并按平台选择命令与参数", async () => {
  const darwin = captureSpawn()
  const macOpener = createSystemBrowserOpener({
    spawn: darwin.adapter,
    platform: "darwin",
  })
  await macOpener("http://127.0.0.1:8123/web/h/handoff")
  expect(darwin.calls).toEqual([
    { command: "open", args: ["http://127.0.0.1:8123/web/h/handoff"] },
  ])

  const win = captureSpawn()
  const winOpener = createSystemBrowserOpener({ spawn: win.adapter, platform: "win32" })
  await winOpener("http://127.0.0.1:8123")
  expect(win.calls).toEqual([
    { command: "cmd", args: ["/c", "start", "", "http://127.0.0.1:8123"] },
  ])

  const linux = captureSpawn()
  const linuxOpener = createSystemBrowserOpener({ spawn: linux.adapter, platform: "linux" })
  await linuxOpener("http://127.0.0.1:8123")
  expect(linux.calls).toEqual([
    { command: "xdg-open", args: ["http://127.0.0.1:8123"] },
  ])
})

test("异步 error 事件把 opener 的 Promise reject", async () => {
  const adapter: SpawnAdapter = (_command, _args, _options) => {
    const child = fakeChild()
    process.nextTick(() => child.emit("error", new Error("ENOENT")))
    return child
  }
  const opener = createSystemBrowserOpener({ spawn: adapter, platform: "darwin" })
  await expect(opener("http://127.0.0.1:8123")).rejects.toThrow("ENOENT")
})

test("同步抛错也把 opener 的 Promise reject，且错误不含 URL", async () => {
  const adapter: SpawnAdapter = () => {
    throw new Error("spawn blocked")
  }
  const opener = createSystemBrowserOpener({ spawn: adapter, platform: "darwin" })
  const error = await opener("http://127.0.0.1:8123/web/h/secret-handoff#token=secret").catch(value => value)
  expect(error).toBeInstanceOf(Error)
  expect((error as Error).message).toBe("spawn blocked")
  expect((error as Error).message).not.toContain("8123")
  expect((error as Error).message).not.toContain("token")
})
