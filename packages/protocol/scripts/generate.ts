/** 从唯一 v3 JSON Schema 生成双端类型、常量和 Python Schema 副本。 */

import { readFile, writeFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { resolve } from "node:path"

type Schema = Record<string, any>
type ContractEntry = { params?: string; result?: string; payload?: string; capability?: string; handle?: string; controlled?: boolean }
type Metadata = {
  major: number
  minor: number
  max_frame_bytes: number
  max_tool_payload_bytes: number
  operations: Record<string, ContractEntry>
  events: Record<string, ContractEntry>
  interactions: Record<string, ContractEntry>
  capabilities: string[]
  error_codes: Record<string, { jsonrpc_code: number; retryable: boolean }>
}

const protocolRoot = resolve(import.meta.dir, "..")
const repositoryRoot = resolve(protocolRoot, "../..")
const schemaPath = resolve(protocolRoot, "schema/v3.json")
const schemaText = await readFile(schemaPath, "utf8")
const schemaDigest = createHash("sha256").update(schemaText).digest("hex")
const schema = JSON.parse(schemaText) as Schema
const metadata = schema["x-harness"] as Metadata
const targets = [
  [resolve(protocolRoot, "src/generated.ts"), renderTypeScript(schema, metadata, schemaDigest)],
  [resolve(protocolRoot, "fixtures/v3-contract.json"), renderContractFixtures(schema, metadata)],
  [resolve(repositoryRoot, "packages/agent/harness_agent/protocol/generated.py"), renderPython(schema, metadata, schemaDigest)],
  [resolve(repositoryRoot, "packages/agent/harness_agent/protocol/protocol_v3.json"), schemaText],
  [resolve(repositoryRoot, "packages/agent/harness_agent/protocol/protocol_v3.sha256"), `${schemaDigest}\n`],
] as const

if (process.argv.includes("--check")) {
  for (const [path, expected] of targets) {
    const actual = await readFile(path, "utf8").catch(() => "")
    if (actual !== expected) throw new Error(`${path} 已过期，请运行 bun run protocol:generate`)
  }
} else {
  for (const [path, content] of targets) await writeFile(path, content, "utf8")
}

function renderTypeScript(root: Schema, meta: Metadata, digest: string): string {
  const definitions = Object.entries(root.$defs as Record<string, Schema>)
    .map(([name, definition]) => name === "jsonValue"
      ? "export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }"
      : `export type ${pascal(name)} = ${renderType(definition)}`)
    .join("\n")
  const operationAliases = Object.entries(meta.operations).flatMap(([method, entry]) => {
    const base = pascal(method)
    const params = typeFromRef(entry.params!)
    const result = typeFromRef(entry.result!)
    return [
      params === `${base}Params` ? "" : `export type ${base}Params = ${params}`,
      result === `${base}Result` ? "" : `export type ${base}Result = ${result}`,
    ].filter(Boolean)
  }).join("\n")
  const operationMap = Object.entries(meta.operations)
    .map(([method]) => `  ${JSON.stringify(method)}: { params: ${pascal(method)}Params; result: ${pascal(method)}Result }`)
    .join("\n")
  const eventUnion = Object.entries(meta.events)
    .map(([type, entry]) => `  | AgentEventOf<${JSON.stringify(type)}, ${typeFromRef(entry.payload!)}>`)
    .join("\n")
  const interactionMap = Object.entries(meta.interactions)
    .map(([method, entry]) => `  ${JSON.stringify(method)}: { params: ${typeFromRef(entry.params!)}; result: ${typeFromRef(entry.result!)} }`)
    .join("\n")
  const methodEntries = [...Object.keys(meta.operations), "event", ...Object.keys(meta.interactions)]
    .map(method => `  ${constant(method)}: ${JSON.stringify(method)},`)
    .join("\n")

  return `/** 此文件由 packages/protocol/schema/v3.json 生成，请勿手工修改。 */

export const PROTOCOL_MAJOR = ${meta.major} as const
export const PROTOCOL_MINOR = ${meta.minor} as const
export const PROTOCOL_SCHEMA_SHA256 = ${JSON.stringify(digest)} as const
export const MAX_FRAME_BYTES = ${meta.max_frame_bytes} as const
export const MAX_TOOL_PAYLOAD_BYTES = ${meta.max_tool_payload_bytes} as const
export const CLIENT_METHODS = ${JSON.stringify(Object.keys(meta.operations))} as const
export const EVENT_TYPES = ${JSON.stringify(Object.keys(meta.events))} as const
export const INTERACTION_METHODS = ${JSON.stringify(Object.keys(meta.interactions))} as const
export const SERVER_CAPABILITIES = ${JSON.stringify(meta.capabilities)} as const
export const OPERATION_CAPABILITIES = ${JSON.stringify(Object.fromEntries(Object.entries(meta.operations).map(([name, entry]) => [name, entry.capability ?? null])))} as const
export const CONTROLLED_OPERATIONS = ${JSON.stringify(Object.entries(meta.operations).filter(([, entry]) => entry.controlled).map(([name]) => name))} as const
export const INTERACTION_HANDLES = ${JSON.stringify(Object.fromEntries(Object.entries(meta.interactions).map(([name, entry]) => [name, entry.handle])))} as const
export const ERROR_CODES = ${JSON.stringify(Object.fromEntries(Object.entries(meta.error_codes).map(([name, entry]) => [name, { jsonrpcCode: entry.jsonrpc_code, retryable: entry.retryable }])))} as const
export type ErrorCode = keyof typeof ERROR_CODES
export const Capability = ${JSON.stringify(Object.fromEntries(meta.capabilities.map(value => [constant(value), value])))} as const
export const EventType = ${JSON.stringify(Object.fromEntries(Object.keys(meta.events).map(value => [constant(value), value])))} as const

export const Method = {
${methodEntries}
} as const

export const PROTOCOL_VERSION = { major: PROTOCOL_MAJOR, minor: PROTOCOL_MINOR } as const

export type JsonRpcErrorObject = { code: number; message: string; data?: unknown }
export type JsonRpcRequest = { jsonrpc: "2.0"; method: string; params?: JsonObject; id: string }
export type JsonRpcNotification = { jsonrpc: "2.0"; method: string; params?: JsonObject }
export type JsonRpcResponse = { jsonrpc: "2.0"; result?: unknown; error?: JsonRpcErrorObject; id: string | null }
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse

${definitions}

${operationAliases}

export interface OperationMap {
${operationMap}
}
export type OperationName = keyof OperationMap

export type AgentEventOf<T extends string, P> = {
  event_id: string
  type: T
  thread_id: string
  run_id: string
  sequence: number
  timestamp_ms: number
  execution_id?: string
  parent_execution_id?: string | null
  agent_id?: string
  payload: P
}
export type AgentEvent =
${eventUnion}
export type EventEnvelope = AgentEvent

export interface InteractionMap {
${interactionMap}
}
export type InteractionMethod = keyof InteractionMap
export type InteractionRequest = {
  [M in InteractionMethod]: { method: M; id: string; params: InteractionMap[M]["params"] }
}[InteractionMethod]
`
}

function renderContractFixtures(root: Schema, meta: Metadata): string {
  const valid: Array<Record<string, unknown>> = []
  const invalid: Array<Record<string, unknown>> = []
  for (const [name, entry] of Object.entries(meta.operations)) {
    addFixtureGroup(root, valid, invalid, "operation.params", name, entry.params!)
    addFixtureGroup(root, valid, invalid, "operation.result", name, entry.result!)
  }
  for (const [name, entry] of Object.entries(meta.events)) {
    const envelope = sample(root, { $ref: "#/$defs/eventBase" }) as Record<string, unknown>
    envelope.type = name
    envelope.payload = sample(root, { $ref: entry.payload! })
    addValueFixtures(valid, invalid, "event", name, envelope, root.$defs.eventBase)
  }
  const firstEvent = Object.entries(meta.events)[0]
  if (firstEvent) {
    const [_, entry] = firstEvent
    const unknown = sample(root, { $ref: "#/$defs/eventBase" }) as Record<string, unknown>
    unknown.type = "event.unknown"
    unknown.payload = sample(root, { $ref: entry.payload! })
    invalid.push({ kind: "event", name: "event.unknown", case: "unknown_event", value: unknown })
  }
  for (const [name, entry] of Object.entries(meta.interactions)) {
    addFixtureGroup(root, valid, invalid, "interaction.params", name, entry.params!)
    addFixtureGroup(root, valid, invalid, "interaction.result", name, entry.result!)
  }
  addFixtureGroup(root, valid, invalid, "error", "ProtocolErrorData", "#/$defs/protocolErrorData")
  for (const [name, entry] of Object.entries(meta.error_codes)) {
    addValueFixtures(valid, invalid, "error", name, { code: name, retryable: entry.retryable }, {})
  }
  return `${JSON.stringify({ valid, invalid }, null, 2)}\n`
}

function addFixtureGroup(
  root: Schema,
  valid: Array<Record<string, unknown>>,
  invalid: Array<Record<string, unknown>>,
  kind: string,
  name: string,
  ref: string,
): void {
  const definition = resolveRef(root, ref)
  addValueFixtures(valid, invalid, kind, name, sample(root, definition), definition)
}

function addValueFixtures(
  valid: Array<Record<string, unknown>>,
  invalid: Array<Record<string, unknown>>,
  kind: string,
  name: string,
  value: unknown,
  definition: Schema,
): void {
  valid.push({ kind, name, case: "valid", value })
  invalid.push({ kind, name, case: "wrong_type", value: null })
  if (isRecord(value)) {
    if (definition.additionalProperties === false) {
      invalid.push({ kind, name, case: "extra_field", value: { ...value, __extra: true } })
    }
    const required = resolvedRequired(definition)
    if (required.length) {
      const missing = { ...value }
      delete missing[required[0]]
      invalid.push({ kind, name, case: "missing_field", value: missing })
    }
  }
}

function sample(root: Schema, input: Schema): unknown {
  const definition = input.$ref ? resolveRef(root, input.$ref) : input
  if (definition.const !== undefined) return definition.const
  if (definition.enum) return definition.enum[0]
  if (definition.default !== undefined) return definition.default
  if (definition.oneOf) return sample(root, definition.oneOf[0])
  if (definition.anyOf) return sample(root, definition.anyOf[0])
  const type = Array.isArray(definition.type)
    ? definition.type.find((value: string) => value !== "null") ?? "null"
    : definition.type
  if (type === "null") return null
  if (type === "boolean") return false
  if (type === "integer" || type === "number") return Math.max(definition.minimum ?? 0, 1)
  if (type === "string") return "x".repeat(Math.max(definition.minLength ?? 1, 1))
  if (type === "array") {
    return Array.from(
      { length: definition.minItems ?? 0 },
      () => sample(root, definition.items ?? {}),
    )
  }
  if (type === "object") {
    const result: Record<string, unknown> = {}
    for (const field of definition.required ?? []) {
      result[field] = sample(root, definition.properties[field])
    }
    return result
  }
  return null
}

function resolveRef(root: Schema, ref: string): Schema {
  const prefix = "#/$defs/"
  if (!ref.startsWith(prefix)) throw new Error(`不支持的 Schema ref: ${ref}`)
  return root.$defs[ref.slice(prefix.length)]
}

function resolvedRequired(definition: Schema): string[] {
  return Array.isArray(definition.required) ? definition.required : []
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function renderPython(root: Schema, meta: Metadata, digest: string): string {
  const operations = Object.entries(meta.operations)
  const wireTypes = renderPythonWireTypes(root)
  const wireAliases = operations.flatMap(([method, entry]) => {
    const base = pascal(method)
    const params = pythonTypeFromRef(entry.params!)
    const result = pythonTypeFromRef(entry.result!)
    return [
      params === `${base}ParamsWire` ? "" : `${base}ParamsWire = ${params}`,
      result === `${base}ResultWire` ? "" : `${base}ResultWire = ${result}`,
    ].filter(Boolean)
  }).join("\n")
  const aliases = operations.flatMap(([method, entry]) => {
    const base = pascal(method)
    return [
      `${base}Params = schema_model(${JSON.stringify(entry.params)}, name="${base}Params")`,
      `${base}Result = schema_model(${JSON.stringify(entry.result)}, name="${base}Result")`,
    ]
  }).join("\n")
  return `"""由 packages/protocol/schema/v3.json 生成的协议入口，请勿手工修改。"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypeAlias, TypedDict
from harness_agent.protocol.runtime import event_model, schema_model

PROTOCOL_MAJOR = ${meta.major}
PROTOCOL_MINOR = ${meta.minor}
PROTOCOL_SCHEMA_SHA256 = ${JSON.stringify(digest)}
MAX_FRAME_BYTES = ${meta.max_frame_bytes}
MAX_TOOL_PAYLOAD_BYTES = ${meta.max_tool_payload_bytes}
CLIENT_METHODS = ${pythonLiteral(Object.keys(meta.operations))}
EVENT_TYPES = ${pythonLiteral(Object.keys(meta.events))}
INTERACTION_METHODS = ${pythonLiteral(Object.keys(meta.interactions))}
SERVER_CAPABILITIES = ${pythonLiteral(meta.capabilities)}
OPERATION_CAPABILITIES = ${pythonLiteral(Object.fromEntries(operations.map(([name, entry]) => [name, entry.capability ?? null])))}
CONTROLLED_OPERATIONS = ${pythonLiteral(Object.entries(meta.operations).filter(([, entry]) => entry.controlled).map(([name]) => name))}
INTERACTION_HANDLES = ${pythonLiteral(Object.fromEntries(Object.entries(meta.interactions).map(([name, entry]) => [name, entry.handle])))}
ERROR_CODES = ${pythonLiteral(Object.fromEntries(Object.entries(meta.error_codes).map(([name, entry]) => [name, { "jsonrpc_code": entry.jsonrpc_code, "retryable": entry.retryable }])))}
METHOD = ${pythonLiteral(Object.fromEntries([...Object.keys(meta.operations), "event", ...Object.keys(meta.interactions)].map(value => [constant(value), value])))}
CAPABILITY = ${pythonLiteral(Object.fromEntries(meta.capabilities.map(value => [constant(value), value])))}
EVENT_TYPE = ${pythonLiteral(Object.fromEntries(Object.keys(meta.events).map(value => [constant(value), value])))}

${wireTypes}

${wireAliases}

${aliases}

EventEnvelope = event_model()
ApprovalResponse = schema_model("#/$defs/approvalResponse", name="ApprovalResponse")
QuestionResponse = schema_model("#/$defs/questionResponse", name="QuestionResponse")
`
}

function renderPythonWireTypes(root: Schema): string {
  return Object.entries(root.$defs as Record<string, Schema>)
    .map(([name, definition]) => {
      const typeName = `${pascal(name)}Wire`
      if (name === "jsonValue") {
        return `${typeName}: TypeAlias = None | bool | int | float | str | list["${typeName}"] | dict[str, "${typeName}"]`
      }
      if (definition.$ref) return `${typeName}: TypeAlias = ${pythonTypeFromRef(definition.$ref)}`
      if (definition.type !== "object") return `${typeName}: TypeAlias = ${renderPythonType(definition)}`
      const properties = Object.entries(definition.properties ?? {})
      if (properties.length === 0 && definition.additionalProperties && typeof definition.additionalProperties === "object") {
        return `${typeName}: TypeAlias = dict[str, ${renderPythonType(definition.additionalProperties)}]`
      }
      const required = new Set<string>(definition.required ?? [])
      const fields = properties.map(([field, child]) => {
        const annotation = renderPythonType(child as Schema)
        return `    ${field}: ${required.has(field) ? annotation : `NotRequired[${annotation}]`}`
      })
      return `class ${typeName}(TypedDict):\n${fields.length ? fields.join("\n") : "    pass"}`
    })
    .join("\n\n")
}

function renderPythonType(schema: Schema): string {
  if (schema.$ref) return pythonTypeFromRef(schema.$ref)
  if (schema.const !== undefined) return `Literal[${pythonScalar(schema.const)}]`
  if (schema.enum) return `Literal[${schema.enum.map(pythonScalar).join(", ")}]`
  if (schema.oneOf) return schema.oneOf.map((item: Schema) => renderPythonType(item)).join(" | ")
  if (Array.isArray(schema.type)) {
    return schema.type.map((type: string) => renderPythonType({ ...schema, type })).join(" | ")
  }
  switch (schema.type) {
    case "null": return "None"
    case "boolean": return "bool"
    case "number": return "float"
    case "integer": return "int"
    case "string": return "str"
    case "array": return `list[${renderPythonType(schema.items ?? {})}]`
    case "object":
      if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
        return `dict[str, ${renderPythonType(schema.additionalProperties)}]`
      }
      return "dict[str, Any]"
    default: return "Any"
  }
}

function pythonTypeFromRef(ref: string): string {
  const name = ref.split("/").at(-1)
  if (!name) throw new Error(`无效 Schema ref: ${ref}`)
  return `${pascal(name)}Wire`
}

function pythonScalar(value: unknown): string {
  if (value === null) return "None"
  if (value === true) return "True"
  if (value === false) return "False"
  return JSON.stringify(value)
}

function renderType(schema: Schema): string {
  if (schema.$ref) return typeFromRef(schema.$ref)
  if (schema.const !== undefined) return JSON.stringify(schema.const)
  if (schema.enum) return schema.enum.map((value: unknown) => JSON.stringify(value)).join(" | ")
  if (schema.oneOf) return schema.oneOf.map((item: Schema) => `(${renderType(item)})`).join(" | ")
  if (Array.isArray(schema.type)) return schema.type.map((type: string) => renderType({ ...schema, type })).join(" | ")
  switch (schema.type) {
    case "null": return "null"
    case "boolean": return "boolean"
    case "number":
    case "integer": return "number"
    case "string": return "string"
    case "array": return `Array<${renderType(schema.items ?? {})}>`
    case "object": {
      const required = new Set<string>(schema.required ?? [])
      const properties = Object.entries(schema.properties ?? {})
        .map(([name, value]) => `${JSON.stringify(name)}${required.has(name) ? "" : "?"}: ${renderType(value as Schema)}`)
      if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
        if (properties.length === 0) return `Record<string, ${renderType(schema.additionalProperties)}>`
        properties.push(`[key: string]: ${renderType(schema.additionalProperties)} | unknown`)
      }
      return `{ ${properties.join("; ")} }`
    }
    default: return "unknown"
  }
}

function typeFromRef(ref: string): string {
  const name = ref.split("/").at(-1)
  if (!name) throw new Error(`无效 Schema ref: ${ref}`)
  return pascal(name)
}

function pascal(value: string): string {
  return value.split(/[^a-zA-Z0-9]+/).filter(Boolean)
    .map(part => part[0]!.toUpperCase() + part.slice(1))
    .join("")
}

function constant(value: string): string {
  return value.replace(/[^a-zA-Z0-9]+/g, "_").toUpperCase()
}

function pythonLiteral(value: unknown): string {
  return JSON.stringify(value).replaceAll("null", "None").replaceAll("true", "True").replaceAll("false", "False")
}
