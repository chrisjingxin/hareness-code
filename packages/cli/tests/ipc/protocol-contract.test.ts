/** TypeScript 与 Python 消费同一份 v3 contract fixture。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import {
  assertEventEnvelope,
  validateInteractionParams,
  validateInteractionResult,
  validateOperationParams,
  validateOperationResult,
  validateProtocolErrorData,
  type InteractionMethod,
  type OperationName,
} from "@za38/protocol"

type Fixture = {
  kind: "operation.params" | "operation.result" | "event" | "interaction.params" | "interaction.result" | "error"
  name: string
  value: unknown
}

const fixtures = JSON.parse(
  await readFile(resolve(import.meta.dir, "../../../protocol/fixtures/v3-contract.json"), "utf8"),
) as { valid: Fixture[]; invalid: Fixture[] }

test("TypeScript 接受全部共享有效 fixture", () => {
  for (const fixture of fixtures.valid) expect(() => validate(fixture)).not.toThrow()
})

test("TypeScript 拒绝全部共享无效 fixture", () => {
  for (const fixture of fixtures.invalid) expect(() => validate(fixture)).toThrow()
})

test("Browser CSP 禁止动态代码时仍可校验 initialize", () => {
  const nativeFunction = globalThis.Function
  globalThis.Function = function () {
    throw new EvalError("unsafe-eval blocked by CSP")
  } as FunctionConstructor
  try {
    expect(() => validateOperationParams("initialize", {
      protocol: { major: 3, min_minor: 0, max_minor: 1 },
      client: { name: "csp-test", version: "0", kind: "web" },
      capabilities: { requests: [], handles: [] },
    })).not.toThrow()
  } finally {
    globalThis.Function = nativeFunction
  }
})

test("run.start 必填工作模式且拒绝未知模式", () => {
  const base = { message: "检查", thread_id: "thread-1", run_id: "run-1" }
  expect(() => validateOperationParams("run.start", base)).toThrow()
  expect(() => validateOperationParams("run.start", { ...base, mode: "yolo" })).toThrow()
  expect(() => validateOperationParams("run.start", { ...base, mode: "compose" })).not.toThrow()
})

test("run.started 回传实际工作模式", () => {
  const envelope = (payload: Record<string, unknown>) => ({
    event_id: "e-started",
    type: "run.started",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    payload,
  })
  expect(() => assertEventEnvelope(envelope({ resumed: false }))).toThrow()
  expect(() => assertEventEnvelope(envelope({ resumed: false, mode: "compose" }))).not.toThrow()
})

test("compose.state projection 严格有界且 revision 单调", () => {
  const payload = {
    revision: 3,
    stage: "build",
    status: "running",
    stages: [{ id: "understand", status: "passed", attempts: 1 }],
    tasks: [{ id: "task-1", title: "实现搜索", status: "running" }],
    evidence: [{ label: "pytest -q tests/foo", status: "passed" }],
  }
  const envelope = (value: unknown) => ({
    event_id: "e-compose",
    type: "compose.state",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    payload: value,
  })
  expect(() => assertEventEnvelope(envelope(payload))).not.toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, extra: true }))).toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, stage: "deploy" }))).toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, revision: -1 }))).toThrow()
  expect(() => assertEventEnvelope(envelope({
    ...payload,
    stages: [{ id: "understand", status: "passed", attempts: 1, extra: true }],
  }))).toThrow()
})

function validate(fixture: Fixture): void {
  if (fixture.kind === "operation.params") {
    validateOperationParams(fixture.name as OperationName, fixture.value)
  } else if (fixture.kind === "operation.result") {
    validateOperationResult(fixture.name as OperationName, fixture.value)
  } else if (fixture.kind === "event") {
    assertEventEnvelope(fixture.value)
  } else if (fixture.kind === "interaction.params") {
    validateInteractionParams(fixture.name as InteractionMethod, fixture.value)
  } else if (fixture.kind === "interaction.result") {
    validateInteractionResult(fixture.name as InteractionMethod, fixture.value)
  } else {
    validateProtocolErrorData(fixture.value)
  }
}
