/** Harness v3 协议入口：canonical Schema、生成类型与双向运行时校验。 */

export * from "./generated"

import schema from "../schema/v3.json" with { type: "json" }
import { validatorsByRef } from "./validators.generated"
import {
  CLIENT_METHODS,
  EVENT_TYPES,
  INTERACTION_METHODS,
  type AgentEvent,
  type ApprovalRequest,
  type ApprovalResponse,
  type DirectoryTrustRequest,
  type DirectoryTrustResponse,
  type InitializeParams,
  type InteractionMap,
  type InteractionMethod,
  type JsonRpcMessage,
  type OperationMap,
  type OperationName,
  type QuestionRequest,
  type QuestionResponse,
} from "./generated"

export type InteractionRequestEnvelope =
  | ({ request_id: string; type: "approval" } & ApprovalRequest)
  | ({ request_id: string; type: "question" } & QuestionRequest)
  | ({ request_id: string; type: "directory_trust" } & DirectoryTrustRequest)

export type InteractionResponse =
  | ({ request_id: string; type: "approval" } & ApprovalResponse)
  | ({ request_id: string; type: "question" } & QuestionResponse)
  | ({ request_id: string; type: "directory_trust" } & DirectoryTrustResponse)

type ContractEntry = { params?: string; result?: string; payload?: string; min_minor?: number }
type ContractMetadata = {
  operations: Record<string, ContractEntry>
  events: Record<string, ContractEntry>
  interactions: Record<string, ContractEntry>
}
type ContractValidationError = { instancePath: string; message?: string; keyword: string }
type ContractValidator = {
  (value: unknown): boolean
  errors?: ContractValidationError[] | null
}

const metadata = (schema as unknown as { "x-harness": ContractMetadata })["x-harness"]
/** 校验并返回 operation params；默认值由 Schema 注入。 */
export function validateOperationParams<M extends OperationName>(
  method: M,
  value: unknown,
): OperationMap[M]["params"] {
  validateRef(entry(metadata.operations, method).params!, value, `${method} params`)
  return value as OperationMap[M]["params"]
}

/** 校验并返回 operation result，杜绝跨进程 `as Result`。 */
export function validateOperationResult<M extends OperationName>(
  method: M,
  value: unknown,
): OperationMap[M]["result"] {
  validateRef(entry(metadata.operations, method).result!, value, `${method} result`)
  return value as OperationMap[M]["result"]
}

/** 校验 Agent 事件信封及 type/payload 的对应关系。 */
export function assertEventEnvelope(value: unknown): asserts value is AgentEvent {
  validateRef("#/$defs/eventBase", value, "event")
  const event = value as { type: string; payload: unknown }
  const contract = metadata.events[event.type]
  if (!contract?.payload) throw new Error(`未知 event type：${event.type}`)
  validateRef(contract.payload, event.payload, `${event.type} payload`)
}

/** 校验反向 Interaction 参数。 */
export function validateInteractionParams<M extends InteractionMethod>(
  method: M,
  value: unknown,
): InteractionMap[M]["params"] {
  validateRef(entry(metadata.interactions, method).params!, value, `${method} params`)
  return value as InteractionMap[M]["params"]
}

/** 校验反向 Interaction 响应。 */
export function validateInteractionResult<M extends InteractionMethod>(
  method: M,
  value: unknown,
): InteractionMap[M]["result"] {
  validateRef(entry(metadata.interactions, method).result!, value, `${method} result`)
  return value as InteractionMap[M]["result"]
}

/** 校验业务错误中供表现层分支的稳定 data。 */
export function validateProtocolErrorData(value: unknown): void {
  validateRef("#/$defs/protocolErrorData", value, "protocol error data")
}

/** 对 JSON-RPC 信封做方向无关的基础校验。 */
export function assertJsonRpcMessage(value: unknown): asserts value is JsonRpcMessage {
  if (!object(value) || value.jsonrpc !== "2.0") throw new Error("jsonrpc 必须为 2.0")
  const hasMethod = typeof value.method === "string"
  const hasResult = "result" in value
  const hasError = "error" in value
  if (hasMethod) {
    if (value.id !== undefined && typeof value.id !== "string") throw new Error("JSON-RPC id 必须为字符串")
    if (value.params !== undefined && !object(value.params)) throw new Error("JSON-RPC params 必须为对象")
    if (Object.keys(value).some(key => !["jsonrpc", "method", "params", "id"].includes(key))) {
      throw new Error("JSON-RPC request 包含未知字段")
    }
    return
  }
  if ((value.id !== null && typeof value.id !== "string") || Number(hasResult) + Number(hasError) !== 1) {
    throw new Error("JSON-RPC response 无效")
  }
  if (Object.keys(value).some(key => !["jsonrpc", "id", "result", "error"].includes(key))) {
    throw new Error("JSON-RPC response 包含未知字段")
  }
}

export function assertInitializeParams(value: unknown): asserts value is InitializeParams {
  validateOperationParams("initialize", value)
}

export function isClientMethod(value: string): value is OperationName {
  return (CLIENT_METHODS as readonly string[]).includes(value)
}

export function isKnownEventType(value: string): boolean {
  return (EVENT_TYPES as readonly string[]).includes(value)
}

export function isInteractionMethod(value: string): value is InteractionMethod {
  return (INTERACTION_METHODS as readonly string[]).includes(value)
}

function validateRef(ref: string, value: unknown, label: string): void {
  const validator = validatorsByRef[ref as keyof typeof validatorsByRef] as ContractValidator | undefined
  if (!validator) throw new Error(`未知 Schema ref：${ref}`)
  if (!validator(value)) {
    const detail = (validator.errors ?? [])
      .map(error => `${error.instancePath || "/"} ${error.message ?? error.keyword}`)
      .join("; ")
    throw new Error(`${label} 不符合 v3 contract：${detail}`)
  }
}

function entry(group: Record<string, ContractEntry>, name: string): ContractEntry {
  const value = group[name]
  if (!value) throw new Error(`未知 protocol contract：${name}`)
  return value
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
