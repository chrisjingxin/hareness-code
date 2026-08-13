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

test("interaction.approval 接受严格 file_diff presentation 并拒绝未知字段", () => {
  const params = {
    thread_id: "thread",
    run_id: "run",
    timeout_ms: 1_000,
    payload: {
      interrupt_id: "approval",
      description: "文件变更需要审批",
      requests: null,
      decisions: ["approve_once", "reject"],
      presentation: {
        kind: "file_diff",
        operation: "edit",
        path: "/src/a.ts",
        added_lines: 1,
        removed_lines: 1,
        truncated: false,
        unified_diff: "+new",
      },
    },
  }
  expect(() => validateInteractionParams("interaction.approval", params)).not.toThrow()
  expect(() => validateInteractionParams("interaction.approval", {
    ...params,
    payload: { ...params.payload, presentation: { ...params.payload.presentation, unknown: true } },
  })).toThrow()
})

test("interaction.directory_trust 校验独立请求/响应，approval 不再承载目录信任", () => {
  const params = {
    thread_id: "thread",
    run_id: "run",
    timeout_ms: 1_000,
    payload: {
      interrupt_id: "trust",
      directory: "D:/data",
      target_path: "D:/data/app.toml",
      tool_name: "read_file",
      access: "read",
      shadows_workspace: false,
      decisions: ["allow_session", "deny"],
    },
  }
  expect(() => validateInteractionParams("interaction.directory_trust", params)).not.toThrow()
  expect(() => validateInteractionResult("interaction.directory_trust", { decision: "allow_session" })).not.toThrow()
  expect(() => validateInteractionResult("interaction.directory_trust", { decision: "approve_thread" })).toThrow()
  expect(() => validateInteractionResult("interaction.directory_trust", { decision: "allow_once" })).toThrow()
  expect(() => validateInteractionParams("interaction.directory_trust", {
    ...params,
    payload: { ...params.payload, directory: "" },
  })).toThrow()
  // approval presentation 仅保留 file_diff，旧 directory_trust presentation 被拒绝
  expect(() => validateInteractionParams("interaction.approval", {
    thread_id: "thread",
    run_id: "run",
    timeout_ms: 1_000,
    payload: {
      interrupt_id: "approval",
      description: "需要信任目录",
      requests: null,
      decisions: ["approve_once", "reject"],
      presentation: {
        kind: "directory_trust",
        directory: "D:/data",
        target_path: "D:/data/app.toml",
        tool_name: "read_file",
        access: "read",
        shadows_workspace: false,
      },
    },
  })).toThrow()
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
