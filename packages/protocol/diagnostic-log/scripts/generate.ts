/** 从唯一 Diagnostic Log v1 Schema 生成双端契约、validator 与 fixture。 */

import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import Ajv2020 from "ajv/dist/2020"
import standaloneCode from "ajv/dist/standalone"

type Schema = Record<string, any>
type EventMetadata = { levels: string[]; fields: string }
type Metadata = { version: number; max_record_bytes: number; events: Record<string, EventMetadata> }

const diagnosticRoot = resolve(import.meta.dir, "..")
const protocolRoot = resolve(diagnosticRoot, "..")
const repositoryRoot = resolve(protocolRoot, "../..")
const schemaPath = resolve(diagnosticRoot, "schema/v1.json")
const schemaText = await readFile(schemaPath, "utf8")
const schema = JSON.parse(schemaText) as Schema
const metadata = schema["x-harness-diagnostic"] as Metadata
const digest = createHash("sha256").update(schemaText).digest("hex")
const targets = [
  [resolve(diagnosticRoot, "src/generated.ts"), renderTypes(schema, metadata, digest)],
  [resolve(diagnosticRoot, "src/validators.generated.ts"), renderValidators(schema, metadata)],
  [resolve(diagnosticRoot, "fixtures/v1-contract.json"), renderFixtures(schema, metadata)],
  [resolve(repositoryRoot, "packages/agent/harness_agent/diagnostic_log/generated.py"), renderPython(metadata, digest)],
  [resolve(repositoryRoot, "packages/agent/harness_agent/diagnostic_log/diagnostic_log_v1.json"), schemaText],
  [resolve(repositoryRoot, "packages/agent/harness_agent/diagnostic_log/diagnostic_log_v1.sha256"), `${digest}\n`],
] as const

if (process.argv.includes("--check")) {
  for (const [path, expected] of targets) {
    const actual = await readFile(path, "utf8").catch(() => "")
    if (actual !== expected) throw new Error(`${path} 已过期，请运行 bun run protocol:generate`)
  }
} else {
  for (const [path, content] of targets) await writeFile(path, content, "utf8")
}

function renderTypes(root: Schema, meta: Metadata, schemaDigest: string): string {
  const fields = Object.entries(meta.events)
    .map(([event, entry]) => `  ${JSON.stringify(event)}: ${renderType(root, resolveRef(root, entry.fields))}`)
    .join("\n")
  const levels = Object.entries(meta.events)
    .map(([event, entry]) => `  ${JSON.stringify(event)}: ${entry.levels.map(JSON.stringify).join(" | ")}`)
    .join("\n")
  return `/** 由 diagnostic-log/schema/v1.json 生成，请勿手工修改。 */

export const DIAGNOSTIC_LOG_SCHEMA_VERSION = ${meta.version} as const
export const DIAGNOSTIC_LOG_SCHEMA_SHA256 = ${JSON.stringify(schemaDigest)} as const
export const MAX_DIAGNOSTIC_RECORD_BYTES = ${meta.max_record_bytes} as const
export const DIAGNOSTIC_EVENT_LEVELS = ${JSON.stringify(Object.fromEntries(Object.entries(meta.events).map(([event, entry]) => [event, entry.levels])))} as const
export const DIAGNOSTIC_EVENTS = ${JSON.stringify(Object.keys(meta.events))} as const
export type DiagnosticEvent = keyof DiagnosticFieldsMap
export type DiagnosticLevel = "debug" | "info" | "warn" | "error"
export type DiagnosticComponent = "cli" | "agent"
export interface DiagnosticFieldsMap {
${fields}
}
export interface DiagnosticLevelMap {
${levels}
}
export type DiagnosticContext = Partial<Pick<DiagnosticRecord, "thread_id" | "run_id" | "execution_id" | "parent_execution_id" | "agent_id" | "tool_call_id" | "event_id" | "event_sequence" | "activity_id" | "rpc_request_id">>
export type DiagnosticRecord<E extends DiagnosticEvent = DiagnosticEvent> = {
  schema_version: 1
  timestamp_ms: number
  level: DiagnosticLevelMap[E]
  event: E
  component: DiagnosticComponent
  process: { pid: number; started_at_ms: number; record_sequence: number }
  project_fingerprint: string
  thread_id?: string
  run_id?: string
  execution_id?: string
  parent_execution_id?: string
  agent_id?: string
  tool_call_id?: string
  event_id?: string
  event_sequence?: number
  activity_id?: string
  rpc_request_id?: string
  fields: DiagnosticFieldsMap[E]
}
`
}

function renderValidators(root: Schema, meta: Metadata): string {
  const ajv = new Ajv2020({ allErrors: true, strict: true, code: { source: true, esm: true } })
  ajv.addKeyword("x-harness-diagnostic")
  const rootId = root.$id as string
  ajv.addSchema(root, rootId)
  const validators: Record<string, string> = { record: `${rootId}#/$defs/record` }
  for (const [event, entry] of Object.entries(meta.events)) validators[`fields:${event}`] = `${rootId}${entry.fields}`
  const exportsByKey = Object.fromEntries(Object.entries(validators).map(([key, schemaId], index) => {
    const exportName = `validateDiagnostic${index}`
    ajv.addSchema({ $ref: schemaId }, `urn:harness:diagnostic-validator:${index}`)
    return [key, { exportName, schemaId: `urn:harness:diagnostic-validator:${index}` }]
  }))
  const moduleCode = standaloneCode(ajv, Object.fromEntries(Object.values(exportsByKey).map(value => [value.exportName, value.schemaId])))
    .replace(/const (\w+) = require\("ajv\/dist\/runtime\/ucs2length"\)\.default;/, "const $1 = function ucs2length(value){let length=0;for(let index=0;index<value.length;index++,length++){const first=value.charCodeAt(index);if(first>=55296&&first<=56319&&index+1<value.length&&(value.charCodeAt(index+1)&64512)===56320)index++;}return length;};")
  const entries = Object.entries(exportsByKey).map(([key, value]) => `  ${JSON.stringify(key)}: ${value.exportName},`).join("\n")
  return `/** 由 Diagnostic Log v1 Schema 构建期生成，请勿手工修改。 */
// @ts-nocheck -- Ajv standalone 输出由跨语言契约测试覆盖。

${moduleCode}

export const diagnosticValidators = {
${entries}
}
`
}

function renderFixtures(root: Schema, meta: Metadata): string {
  const fixtures: Array<{ valid: boolean; value: unknown }> = []
  for (const [event, entry] of Object.entries(meta.events)) {
    const fieldsSchema = resolveRef(root, entry.fields)
    const record = baseRecord(event, entry.levels[0]!, sample(root, fieldsSchema))
    fixtures.push({ valid: true, value: record })
    fixtures.push({ valid: false, value: { ...record, level: disallowedLevel(entry.levels) } })
    fixtures.push({ valid: false, value: { ...record, fields: { ...(record.fields as object), __extra: true } } })
    const required = fieldsSchema.required as string[] | undefined
    if (required?.length) {
      const missing = { ...(record.fields as Record<string, unknown>) }
      delete missing[required[0]!]
      fixtures.push({ valid: false, value: { ...record, fields: missing } })
    }
  }
  fixtures.push({ valid: false, value: { ...baseRecord("event.unknown", "info", {}), extra: true } })
  return `${JSON.stringify(fixtures, null, 2)}\n`
}

function renderPython(meta: Metadata, schemaDigest: string): string {
  return `\"\"\"由 Diagnostic Log v1 Schema 生成，请勿手工修改。\"\"\"

DIAGNOSTIC_LOG_SCHEMA_VERSION = ${meta.version}
DIAGNOSTIC_LOG_SCHEMA_SHA256 = ${JSON.stringify(schemaDigest)}
MAX_DIAGNOSTIC_RECORD_BYTES = ${meta.max_record_bytes}
DIAGNOSTIC_EVENT_LEVELS = ${pythonLiteral(Object.fromEntries(Object.entries(meta.events).map(([event, entry]) => [event, entry.levels])))}
DIAGNOSTIC_EVENTS = tuple(DIAGNOSTIC_EVENT_LEVELS)
`
}

function baseRecord(event: string, level: string, fields: unknown): Record<string, unknown> {
  return {
    schema_version: 1,
    timestamp_ms: 1,
    level,
    event,
    component: "cli",
    process: { pid: 1, started_at_ms: 1, record_sequence: 1 },
    project_fingerprint: "a".repeat(64),
    fields,
  }
}

function disallowedLevel(levels: string[]): string {
  return ["debug", "info", "warn", "error"].find(level => !levels.includes(level)) ?? "invalid"
}

function sample(root: Schema, input: Schema): unknown {
  const schema = input.$ref ? resolveRef(root, input.$ref) : input
  if (schema.const !== undefined) return schema.const
  if (schema.enum) return schema.enum[0]
  if (Array.isArray(schema.type)) return sample(root, { ...schema, type: schema.type.find((type: string) => type !== "null") ?? "null" })
  if (schema.type === "null") return null
  if (schema.type === "boolean") return false
  if (schema.type === "integer" || schema.type === "number") return Math.max(schema.minimum ?? 0, 1)
  if (schema.type === "string") return schema.pattern === "^[0-9a-f]{64}$" ? "a".repeat(64) : "x"
  if (schema.type === "array") return []
  if (schema.type === "object") {
    return Object.fromEntries((schema.required ?? []).map((name: string) => [name, sample(root, schema.properties[name])]))
  }
  return null
}

function resolveRef(root: Schema, ref: string): Schema {
  const prefix = "#/$defs/"
  if (!ref.startsWith(prefix)) throw new Error(`不支持的 Schema ref: ${ref}`)
  return root.$defs[ref.slice(prefix.length)]
}

function renderType(root: Schema, input: Schema): string {
  if (input.$ref) return renderType(root, resolveRef(root, input.$ref))
  if (input.const !== undefined) return JSON.stringify(input.const)
  if (input.enum) return input.enum.map(JSON.stringify).join(" | ")
  if (Array.isArray(input.type)) return input.type.map((type: string) => renderType(root, { ...input, type })).join(" | ")
  if (input.type === "null") return "null"
  if (input.type === "boolean") return "boolean"
  if (input.type === "integer" || input.type === "number") return "number"
  if (input.type === "string") return "string"
  if (input.type === "array") return `Array<${renderType(root, input.items ?? {})}>`
  if (input.type === "object") {
    const required = new Set<string>(input.required ?? [])
    return `{ ${Object.entries(input.properties ?? {}).map(([name, child]) => `${JSON.stringify(name)}${required.has(name) ? "" : "?"}: ${renderType(root, child as Schema)}`).join("; ")} }`
  }
  return "unknown"
}

function pythonLiteral(value: unknown): string {
  return JSON.stringify(value).replaceAll("null", "None").replaceAll("true", "True").replaceAll("false", "False")
}
