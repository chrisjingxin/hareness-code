/** TypeScript 使用协议包断言消费跨语言共享 fixture。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { assertContextCompactParams, assertEventEnvelope, assertInitializeParams, assertInteractionRequest, assertThreadsListParams, assertThreadsOpenParams } from "@za38/protocol"

type Fixture = { kind: "initialize" | "event" | "request" | "threads.list" | "threads.open"; value: unknown }
const fixtures = JSON.parse(await readFile(resolve(import.meta.dir, "../../../protocol/fixtures/v2-contract.json"), "utf8")) as { valid: Fixture[]; invalid: Fixture[] }

test("TypeScript 接受全部共享有效 fixture", () => {
  for (const fixture of fixtures.valid) expect(() => validate(fixture)).not.toThrow()
})

test("TypeScript 拒绝全部共享无效 fixture", () => {
  for (const fixture of fixtures.invalid) expect(() => validate(fixture)).toThrow()
})

test("TypeScript 接受 v2.4 context.updated 和手动压缩参数", () => {
  expect(() => assertEventEnvelope({
    event_id: "context-1",
    type: "context.updated",
    thread_id: "thread-1",
    run_id: "run-1",
    sequence: 1,
    timestamp_ms: 1,
    payload: {
      action: "summary",
      estimated_tokens: 10,
      input_cap_tokens: 100,
      context_window_tokens: 128,
      dynamic_tokens: 10,
      cache_status: "unknown",
      artifact_ids: [],
    },
  })).not.toThrow()
  expect(() => assertContextCompactParams({ thread_id: "thread-1" })).not.toThrow()
  expect(() => assertContextCompactParams({ thread_id: "", ignored: true })).toThrow()
})

function validate(fixture: Fixture): void {
  if (fixture.kind === "initialize") assertInitializeParams(fixture.value)
  else if (fixture.kind === "event") assertEventEnvelope(fixture.value)
  else if (fixture.kind === "request") assertInteractionRequest(fixture.value)
  else if (fixture.kind === "threads.list") assertThreadsListParams(fixture.value)
  else assertThreadsOpenParams(fixture.value)
}
