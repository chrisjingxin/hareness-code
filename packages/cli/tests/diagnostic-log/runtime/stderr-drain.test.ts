/** Sidecar stderr drain 只记计数，内存不随洪泛增长。 */

import { expect, test } from "bun:test"
import { SidecarStderrDrain } from "../../../src/diagnostic-log/runtime/stderr-drain"

test("大体积 stderr 只累计有界 bytes/lines，不保留原文", () => {
  const drain = new SidecarStderrDrain()
  const chunk = Buffer.alloc(64 * 1024, 65)
  for (let index = 0; index < 20; index += 1) drain.push(chunk)
  const snapshot = drain.snapshot()
  expect(snapshot.bytes).toBe(256 * 1024)
  expect(snapshot.truncated).toBe(true)
  expect(JSON.stringify(drain)).not.toContain("A".repeat(100))
})

test("换行计数同样有上界", () => {
  const drain = new SidecarStderrDrain()
  drain.push(Buffer.from("x\n".repeat(20_000)))
  const snapshot = drain.snapshot()
  expect(snapshot.lines).toBe(10_000)
  expect(snapshot.truncated).toBe(true)
})
