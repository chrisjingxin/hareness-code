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
