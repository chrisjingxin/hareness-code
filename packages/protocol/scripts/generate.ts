/** 从 v2 Schema 的协议元数据生成两端共享常量和模型骨架。 */

import { readFile, writeFile } from "node:fs/promises"
import { resolve } from "node:path"

type Metadata = {
  major: number
  minor: number
  max_frame_bytes: number
  max_tool_payload_bytes: number
  client_methods: string[]
  server_methods: string[]
  server_capabilities: string[]
  event_types: string[]
}

const protocolRoot = resolve(import.meta.dir, "..")
const repositoryRoot = resolve(protocolRoot, "../..")
const schema = JSON.parse(await readFile(resolve(protocolRoot, "schema/v2.json"), "utf8")) as { "x-protocol": Metadata }
const metadata = schema["x-protocol"]
const targets = [
  [resolve(protocolRoot, "src/generated.ts"), renderTypeScript(metadata)],
  [resolve(repositoryRoot, "packages/agent/harness_agent/protocol_generated.py"), renderPython(metadata)],
] as const

if (process.argv.includes("--check")) {
  for (const [path, expected] of targets) {
    const actual = await readFile(path, "utf8").catch(() => "")
    if (actual !== expected) throw new Error(`${path} 已过期，请运行 bun run protocol:generate`)
  }
} else {
  for (const [path, content] of targets) await writeFile(path, content, "utf8")
}

function renderTypeScript(meta: Metadata): string {
  return `/** 此文件由 packages/protocol/scripts/generate.ts 生成，请勿手工修改。 */

export const PROTOCOL_MAJOR = ${meta.major} as const
export const PROTOCOL_MINOR = ${meta.minor} as const
export const MAX_FRAME_BYTES = ${meta.max_frame_bytes} as const
export const MAX_TOOL_PAYLOAD_BYTES = ${meta.max_tool_payload_bytes} as const
export const CLIENT_METHODS = ${JSON.stringify(meta.client_methods)} as const
export const SERVER_METHODS = ${JSON.stringify(meta.server_methods)} as const
export const SERVER_CAPABILITIES = ${JSON.stringify(meta.server_capabilities)} as const
export const EVENT_TYPES = ${JSON.stringify(meta.event_types)} as const

export type JsonObject = Record<string, unknown>
export type JsonRpcErrorObject = { code: number; message: string; data?: unknown }
export type JsonRpcRequest = { jsonrpc: "2.0"; method: string; params?: JsonObject; id: string }
export type JsonRpcNotification = { jsonrpc: "2.0"; method: string; params?: JsonObject }
export type JsonRpcResponse = { jsonrpc: "2.0"; result?: unknown; error?: JsonRpcErrorObject; id: string | null }
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse

export interface InitializeParams { protocol: { major: ${meta.major}; min_minor: number; max_minor: number }; client: { name: string; version: string }; capabilities: string[]; cwd?: string; config_path?: string }
export interface InitializeResult { protocol: { major: ${meta.major}; minor: number }; server: { name: string; version: string }; server_capabilities: string[]; enabled_capabilities: string[]; agent_commands: Array<{ name: string; description: string; aliases: string[] }>; limits: { max_frame_bytes: number; max_tool_payload_bytes: number }; config_summary: JsonObject | null; startup_error: { code: string; message: string } | null }
export interface RunStartParams { message: string; thread_id?: string; run_id?: string }
export interface RunStartResult { thread_id: string; run_id: string; accepted: boolean }
export interface RunCancelParams { thread_id: string; run_id: string }
export interface RunCancelResult { cancelled: boolean; run_id: string }
export interface EventEnvelope { event_id: string; type: string; thread_id: string; run_id: string; sequence: number; timestamp_ms: number; source?: { kind: "root" | "subagent" | "background"; id?: string; parent_tool_call_id?: string }; payload: JsonObject; extensions?: JsonObject }
export interface InteractionRequestEnvelope { request_id: string; type: "approval" | "question"; thread_id: string; run_id: string; sequence: number; timeout_ms: number; payload: JsonObject }
export interface ApprovalResponse { type: "approval"; request_id: string; decision: "approve_once" | "approve_session" | "reject"; feedback?: string }
export interface QuestionResponse { type: "question"; request_id: string; answers: Record<string, string[]> }
export type InteractionResponse = ApprovalResponse | QuestionResponse
`
}

function renderPython(meta: Metadata): string {
  return `\"\"\"由 packages/protocol/scripts/generate.ts 生成的 v2 协议模型，请勿手工修改。\"\"\"

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_MAJOR = ${meta.major}
PROTOCOL_MINOR = ${meta.minor}
MAX_FRAME_BYTES = ${meta.max_frame_bytes}
MAX_TOOL_PAYLOAD_BYTES = ${meta.max_tool_payload_bytes}
CLIENT_METHODS = ${JSON.stringify(meta.client_methods).replaceAll("null", "None")}
SERVER_METHODS = ${JSON.stringify(meta.server_methods).replaceAll("null", "None")}
SERVER_CAPABILITIES = ${JSON.stringify(meta.server_capabilities).replaceAll("null", "None")}
EVENT_TYPES = ${JSON.stringify(meta.event_types).replaceAll("null", "None")}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ProtocolRange(StrictModel):
    major: Literal[${meta.major}]
    min_minor: int = Field(ge=0)
    max_minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "ProtocolRange":
        if self.min_minor > self.max_minor:
            raise ValueError("min_minor must be <= max_minor")
        return self

class ClientInfo(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)

class InitializeParams(StrictModel):
    protocol: ProtocolRange
    client: ClientInfo
    capabilities: list[str]
    cwd: str | None = None
    config_path: str | None = None

class RunStartParams(StrictModel):
    message: str = Field(min_length=1)
    thread_id: str | None = None
    run_id: str | None = None

class RunCancelParams(StrictModel):
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

class EventEnvelope(StrictModel):
    event_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)
    source: dict[str, Any] | None = None
    payload: dict[str, Any]
    extensions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_known_payload(self) -> "EventEnvelope":
        fields = {
            "run.started": {"resumed"}, "content.delta": {"text"}, "thinking.delta": {"text"},
            "tool.started": {"tool_call_id", "name"},
            "tool.delta": {"tool_call_id", "arguments_delta", "output_delta", "truncated", "original_bytes"},
            "tool.completed": {"tool_call_id", "result"},
            "interaction.resolved": {"request_id", "type"},
            "run.completed": {"usage", "duration_ms", "finish_reason"},
            "run.cancelled": {"reason"}, "run.failed": {"error"},
        }
        allowed = fields.get(self.type)
        if allowed is not None and set(self.payload) - allowed:
            raise ValueError(f"unexpected payload fields for {self.type}")
        return self

class InteractionRequestEnvelope(StrictModel):
    request_id: str = Field(min_length=1)
    type: Literal["approval", "question"]
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timeout_ms: int = Field(ge=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self) -> "InteractionRequestEnvelope":
        allowed = ({"interrupt_id", "description", "requests", "decisions"}
                   if self.type == "approval" else {"interrupt_id", "questions"})
        if set(self.payload) - allowed or "interrupt_id" not in self.payload:
            raise ValueError(f"invalid {self.type} payload")
        return self

class ApprovalResponse(StrictModel):
    type: Literal["approval"]
    request_id: str
    decision: Literal["approve_once", "approve_session", "reject"]
    feedback: str = ""

class QuestionResponse(StrictModel):
    type: Literal["question"]
    request_id: str
    answers: dict[str, list[str]]
`
}
