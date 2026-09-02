/** TypeScript 与 Python 消费同一份 v3 contract fixture。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import {
  assertEventEnvelope,
  OPERATION_MIN_MINOR,
  PROTOCOL_VERSION,
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

test("Settings 与 Plugin RPC 在 canonical v3 contract 中要求 minor 8", () => {
  expect(PROTOCOL_VERSION).toEqual({ major: 3, minor: 8 })
  expect(OPERATION_MIN_MINOR["commands.bind"]).toBe(6)
})

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

test("Plugin Command provenance 只接受稳定四字段，普通事件 shape 不变", () => {
  const provenance = {
    plugin_id: "local/ZA38",
    package_digest: "a".repeat(64),
    command_id: "plugin/local/ZA38/command/za38-sdd",
    snapshot_id: "snapshot-1",
  }
  const base = {
    event_id: "e-provenance",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
  }
  expect(() => assertEventEnvelope({
    ...base,
    type: "run.started",
    payload: { mode: "build", resumed: false, command_provenance: provenance },
  })).not.toThrow()
  expect(() => assertEventEnvelope({
    ...base,
    type: "skill.loaded",
    payload: {
      skill_id: "plugin/local/ZA38/command/za38-sdd",
      source: "plugin:local/ZA38",
      version: "0.2.0",
      snapshot_id: "snapshot-1",
      provenance,
    },
  })).not.toThrow()
  expect(() => assertEventEnvelope({
    ...base,
    type: "skill.loaded",
    payload: {
      skill_id: "plugin/local/ZA38/skill/za38-framework",
      source: "plugin:local/ZA38",
      version: "0.2.0",
      snapshot_id: "snapshot-1",
      provenance: { ...provenance, command_id: null },
    },
  })).not.toThrow()
  // Non-plugin/non-command 的旧 payload 仍不需要伪造 provenance。
  expect(() => assertEventEnvelope({
    ...base,
    type: "run.started",
    payload: { mode: "build", resumed: false },
  })).not.toThrow()
  expect(() => assertEventEnvelope({
    ...base,
    type: "skill.loaded",
    payload: { skill_id: "builtin/review", source: "builtin", version: null, snapshot_id: "snapshot-1" },
  })).not.toThrow()
  expect(() => assertEventEnvelope({
    ...base,
    type: "run.started",
    payload: { mode: "build", resumed: false, command_provenance: { ...provenance, body: "secret" } },
  })).toThrow()
})

test("compose_scope 与 compose.summary 合法；非法 scope 与越界摘要被拒绝", () => {
  const scope = {
    activity_id: "act-understand-1",
    stage: "understand",
    attempt: 1,
    task_id: "task-1",
    task_title: "梳理需求",
  }
  const progress = (compose_scope?: Record<string, unknown>) => ({
    event_id: "e-scope",
    type: "run.progress",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    payload: { phase: "model", elapsed_ms: 10 },
    ...(compose_scope ? { compose_scope } : {}),
  })
  expect(() => assertEventEnvelope(progress(scope))).not.toThrow()
  expect(() => assertEventEnvelope(progress())).not.toThrow()
  expect(() => assertEventEnvelope(progress({ activity_id: "", stage: "understand", attempt: 1 }))).toThrow()
  expect(() => assertEventEnvelope(progress({ activity_id: "a1", stage: "deploy", attempt: 1 }))).toThrow()
  expect(() => assertEventEnvelope(progress({ activity_id: "a1", stage: "plan", attempt: 0 }))).toThrow()
  expect(() => assertEventEnvelope({
    event_id: "e-sum",
    type: "compose.summary",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    compose_scope: scope,
    payload: { status: "passed", text: "阶段完成" },
  })).not.toThrow()
  expect(() => assertEventEnvelope({
    event_id: "e-sum-long",
    type: "compose.summary",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    compose_scope: scope,
    payload: { status: "passed", text: "x".repeat(1001) },
  })).toThrow()
})

test("scoped Interaction 可携带 execution provenance 与 compose_scope", () => {
  expect(() => validateInteractionParams("interaction.approval", {
    thread_id: "t",
    run_id: "r",
    timeout_ms: 30_000,
    execution_id: "child-1",
    parent_execution_id: "root-1",
    agent_id: "builder",
    compose_scope: { activity_id: "act-1", stage: "build", attempt: 2, task_id: "t1" },
    payload: {
      interrupt_id: "int-1",
      description: "run tests",
      requests: { action_requests: [] },
      decisions: ["approve_once", "reject"],
    },
  })).not.toThrow()
})

test("compose.progress projection 严格有界且 revision 单调", () => {
  const payload = {
    thread_id: "t",
    slug: "jsondiff",
    complexity: "simple",
    status: "active",
    current_stage: "grill",
    waiting: "ask_user",
    stages: [{ id: "requirement", state: "current" }],
    documents: [{ kind: "task", path: "docs/compose/jsondiff/task.md", confirmed: false }],
    fix_rounds: 0,
    revision: 3,
  }
  const envelope = (value: unknown) => ({
    event_id: "e-compose",
    type: "compose.progress",
    thread_id: "t",
    run_id: "r",
    sequence: 1,
    timestamp_ms: 1,
    payload: value,
  })
  expect(() => assertEventEnvelope(envelope(payload))).not.toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, extra: true }))).toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, current_stage: "deploy" }))).toThrow()
  expect(() => assertEventEnvelope(envelope({ ...payload, revision: -1 }))).toThrow()
  expect(() => assertEventEnvelope(envelope({
    ...payload,
    stages: [{ id: "requirement", state: "current", extra: true }],
  }))).toThrow()
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
