/** 由 diagnostic-log/schema/v1.json 生成，请勿手工修改。 */

export const DIAGNOSTIC_LOG_SCHEMA_VERSION = 1 as const
export const DIAGNOSTIC_LOG_SCHEMA_SHA256 = "60347221506f0e7d507970a3c3c54fad0b5c3aecb364128fefd1209e96d9e566" as const
export const MAX_DIAGNOSTIC_RECORD_BYTES = 8192 as const
export const DIAGNOSTIC_EVENT_LEVELS = {"logging.started":["info"],"logging.reconfigured":["info"],"logging.dropped":["warn"],"logging.contract_violation":["warn"],"logging.writer_failed":["error"],"logging.stopped":["info","warn"],"process.started":["info"],"process.stopped":["info","warn","error"],"sidecar.stderr_observed":["warn"],"ipc.initialize.completed":["info"],"ipc.request.completed":["debug","info"],"ipc.request.failed":["warn","error"],"ipc.transport.closed":["info","warn","error"],"run.started":["info"],"run.completed":["info"],"run.cancelled":["warn"],"run.failed":["error"],"runtime.acquire.completed":["debug","info"],"runtime.acquire.failed":["error"],"runtime.released":["debug","info","warn"],"runtime.pool_snapshot":["debug"],"context.build.completed":["info"],"context.build.failed":["error"],"context.compaction.completed":["info"],"context.compaction.failed":["error"],"catalog.bound":["info"],"skill.read":["info"],"hook.started":["debug","info"],"hook.completed":["info"],"hook.failed":["warn","error"],"execution.started":["info"],"execution.completed":["info"],"execution.failed":["warn","error"],"model.started":["debug","info"],"model.completed":["info"],"model.retry_scheduled":["debug","warn"],"model.failed":["warn","error"],"interaction.started":["info"],"interaction.completed":["info","warn"],"tool.started":["debug","info"],"tool.completed":["info"],"tool.failed":["warn","error"],"mcp.connection.completed":["info"],"mcp.connection.failed":["warn","error"],"mcp.connection.closed":["info","warn"],"persistence.operation.completed":["debug","info"],"persistence.operation.failed":["error"],"presentation.handoff.opening":["info"],"presentation.renderer.accepted":["info"],"presentation.handoff.active":["info"],"presentation.handoff.returning":["info"],"presentation.gateway.connected":["info"]} as const
export const DIAGNOSTIC_EVENTS = ["logging.started","logging.reconfigured","logging.dropped","logging.contract_violation","logging.writer_failed","logging.stopped","process.started","process.stopped","sidecar.stderr_observed","ipc.initialize.completed","ipc.request.completed","ipc.request.failed","ipc.transport.closed","run.started","run.completed","run.cancelled","run.failed","runtime.acquire.completed","runtime.acquire.failed","runtime.released","runtime.pool_snapshot","context.build.completed","context.build.failed","context.compaction.completed","context.compaction.failed","catalog.bound","skill.read","hook.started","hook.completed","hook.failed","execution.started","execution.completed","execution.failed","model.started","model.completed","model.retry_scheduled","model.failed","interaction.started","interaction.completed","tool.started","tool.completed","tool.failed","mcp.connection.completed","mcp.connection.failed","mcp.connection.closed","persistence.operation.completed","persistence.operation.failed","presentation.handoff.opening","presentation.renderer.accepted","presentation.handoff.active","presentation.handoff.returning","presentation.gateway.connected"] as const
export type DiagnosticEvent = keyof DiagnosticFieldsMap
export type DiagnosticLevel = "debug" | "info" | "warn" | "error"
export type DiagnosticComponent = "cli" | "agent"
export type DiagnosticQueryResult = { "query_schema_version": 1; "project_fingerprint": string; "thread_id"?: string; "run_id"?: string; "filters": { "minimum_level": "debug" | "info" | "warn" | "error"; "event": string | null; "component": "cli" | "agent" | null }; "summary"?: { "run_count": number; "outcome"?: string; "runs": Array<{ "run_id": string; "mode"?: "build" | "compose"; "outcome"?: string; "duration_ms"?: number }> } | { "outcome"?: string; "duration_ms"?: number; "active_ms"?: number; "interaction_wait_ms"?: number; "retry_wait_ms"?: number; "first_visible_activity_ms"?: number | null }; "threads"?: Array<{ "thread_id": string; "last_activity_ms": number; "run_count": number; "latest_mode"?: "build" | "compose"; "latest_outcome"?: string; "latest_duration_ms"?: number }>; "events": Array<{ "schema_version": 1; "timestamp_ms": number; "level": "debug" | "info" | "warn" | "error"; "event": string; "component": "cli" | "agent"; "process": { "pid": number; "started_at_ms": number; "record_sequence": number }; "project_fingerprint": string; "thread_id"?: string; "run_id"?: string; "execution_id"?: string; "parent_execution_id"?: string; "agent_id"?: string; "tool_call_id"?: string; "event_id"?: string; "event_sequence"?: number; "activity_id"?: string; "rpc_request_id"?: string; "fields": {  } }>; "matched_count": number; "returned_count": number; "truncated": boolean; "next_cursor": string | null; "diagnostics": { "malformed_count": number; "invalid_count": number; "unsupported_schema_count": number; "partial_line_count": number; "orphan_active_count": number; "orphan_active_bytes": number } }
export interface DiagnosticFieldsMap {
  "logging.started": { "effective_level": "debug" | "info" | "warn" | "error"; "max_queue_records": number; "max_queue_bytes": number; "reserved_queue_records": number; "reserved_queue_bytes": number; "max_file_bytes": number }
  "logging.reconfigured": { "previous_level": "debug" | "info" | "warn" | "error"; "effective_level": "debug" | "info" | "warn" | "error"; "source": "default" | "environment" | "config" | "initialize" }
  "logging.dropped": { "debug_count": number; "info_count": number; "warn_count": number; "error_count": number; "invalid_count": number; "oversize_count": number; "reason": "queue_full" | "record_too_large" | "contract_violation" | "close_timeout" | "writer_disabled" }
  "logging.contract_violation": { "invalid_level_count": number; "invalid_event_count": number; "invalid_field_count": number }
  "logging.writer_failed": { "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "logging.stopped": { "flush_outcome": "completed" | "timeout" | "disabled"; "queued_count": number; "written_count": number; "dropped_count": number; "duration_ms": number }
  "process.started": { "command_kind": string; "runtime_version": string; "platform": string; "arch": string }
  "process.stopped": { "outcome": "completed" | "cancelled" | "failed"; "exit_code": number | null; "duration_ms": number }
  "sidecar.stderr_observed": { "bytes": number; "lines": number; "truncated": boolean }
  "ipc.initialize.completed": { "side": "client" | "server"; "duration_ms": number; "protocol_minor": number }
  "ipc.request.completed": { "side": "client" | "server"; "method": string; "duration_ms": number }
  "ipc.request.failed": { "side": "client" | "server"; "method": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "ipc.transport.closed": { "side": "client" | "server"; "outcome": "completed" | "cancelled" | "failed"; "pending_requests": number }
  "run.started": { "mode": "build" | "compose"; "resumed": boolean; "approval_mode": string; "model_profile_id": string }
  "run.completed": { "outcome": "completed"; "duration_ms": number; "active_ms": number; "interaction_wait_ms": number; "retry_wait_ms": number; "first_visible_activity_ms": number | null; "usage": { "input_tokens": number | null; "output_tokens": number | null; "cached_input_tokens": number | null } }
  "run.cancelled": { "cancellation_source": string; "duration_ms": number; "active_ms": number; "interaction_wait_ms": number; "retry_wait_ms": number }
  "run.failed": { "duration_ms": number; "active_ms": number; "interaction_wait_ms": number; "retry_wait_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "runtime.acquire.completed": { "source": "new" | "reused"; "queue_ms": number; "build_ms": number; "duration_ms": number }
  "runtime.acquire.failed": { "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "runtime.released": { "outcome": "released" | "discarded" | "failed"; "duration_ms": number }
  "runtime.pool_snapshot": { "active_count": number; "idle_count": number; "waiter_count": number; "eviction_count": number }
  "context.build.completed": { "duration_ms": number; "message_count": number; "estimated_tokens": number; "cache_status": "hit" | "miss" | "disabled" }
  "context.build.failed": { "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "context.compaction.completed": { "trigger": "auto" | "manual" | "overflow"; "action": string; "duration_ms": number; "before_estimated_tokens": number; "after_estimated_tokens": number; "artifact_count": number }
  "context.compaction.failed": { "trigger": "auto" | "manual" | "overflow"; "action": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "catalog.bound": { "skill_count": number; "mcp_count": number; "plugin_count": number; "skill_ids"?: Array<string>; "mcp_ids"?: Array<string>; "plugin_ids"?: Array<string> }
  "skill.read": { "skill_id": string; "kind": "body" | "resource"; "model_round"?: number }
  "hook.started": { "plugin_id": string; "hook_event": "PreToolUse" | "PostToolUse" | "PostToolUseFailure"; "tool_name": string; "model_round"?: number }
  "hook.completed": { "plugin_id": string; "hook_event": "PreToolUse" | "PostToolUse" | "PostToolUseFailure"; "tool_name": string; "outcome": "allow" | "deny" | "ok"; "duration_ms": number; "model_round"?: number }
  "hook.failed": { "plugin_id": string; "hook_event": "PreToolUse" | "PostToolUse" | "PostToolUseFailure"; "tool_name": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string; "model_round"?: number }
  "execution.started": { "kind": "root" | "child" | "compose_stage"; "agent_id": string }
  "execution.completed": { "kind": "root" | "child" | "compose_stage"; "agent_id": string; "outcome": string; "duration_ms": number }
  "execution.failed": { "kind": "root" | "child" | "compose_stage"; "agent_id": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "model.started": { "model_round": number; "provider_attempt": number; "profile_id": string }
  "model.completed": { "model_round": number; "provider_attempt": number; "duration_ms": number; "provider_first_chunk_ms": number | null; "usage": { "input_tokens": number | null; "output_tokens": number | null; "cached_input_tokens": number | null }; "finish_reason": string }
  "model.retry_scheduled": { "model_round": number; "provider_attempt": number; "retry_wait_ms": number; "reason_code": string }
  "model.failed": { "model_round": number; "provider_attempt": number; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "interaction.started": { "kind": string; "source": string; "model_round"?: number; "tool_kind"?: string }
  "interaction.completed": { "kind": string; "outcome": string; "wait_ms": number }
  "tool.started": { "tool_name": string; "tool_kind": string; "server_name"?: string; "model_round": number }
  "tool.completed": { "tool_name": string; "tool_kind": string; "server_name"?: string; "outcome": string; "duration_ms": number; "result_bytes": number; "truncated": boolean }
  "tool.failed": { "tool_name": string; "tool_kind": string; "server_name"?: string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "mcp.connection.completed": { "server_name": string; "server_fingerprint": string; "transport": string; "duration_ms": number; "tool_count": number }
  "mcp.connection.failed": { "server_name": string; "server_fingerprint": string; "transport": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "mcp.connection.closed": { "server_name": string; "server_fingerprint": string; "outcome": string; "duration_ms": number }
  "persistence.operation.completed": { "operation": string; "duration_ms": number; "row_count": number | null }
  "persistence.operation.failed": { "operation": string; "duration_ms": number; "failure_stage": string; "error_code"?: string; "error_type": string; "retryable"?: boolean; "summary_code": string }
  "presentation.handoff.opening": {  }
  "presentation.renderer.accepted": {  }
  "presentation.handoff.active": {  }
  "presentation.handoff.returning": { "reason": string }
  "presentation.gateway.connected": {  }
}
export interface DiagnosticLevelMap {
  "logging.started": "info"
  "logging.reconfigured": "info"
  "logging.dropped": "warn"
  "logging.contract_violation": "warn"
  "logging.writer_failed": "error"
  "logging.stopped": "info" | "warn"
  "process.started": "info"
  "process.stopped": "info" | "warn" | "error"
  "sidecar.stderr_observed": "warn"
  "ipc.initialize.completed": "info"
  "ipc.request.completed": "debug" | "info"
  "ipc.request.failed": "warn" | "error"
  "ipc.transport.closed": "info" | "warn" | "error"
  "run.started": "info"
  "run.completed": "info"
  "run.cancelled": "warn"
  "run.failed": "error"
  "runtime.acquire.completed": "debug" | "info"
  "runtime.acquire.failed": "error"
  "runtime.released": "debug" | "info" | "warn"
  "runtime.pool_snapshot": "debug"
  "context.build.completed": "info"
  "context.build.failed": "error"
  "context.compaction.completed": "info"
  "context.compaction.failed": "error"
  "catalog.bound": "info"
  "skill.read": "info"
  "hook.started": "debug" | "info"
  "hook.completed": "info"
  "hook.failed": "warn" | "error"
  "execution.started": "info"
  "execution.completed": "info"
  "execution.failed": "warn" | "error"
  "model.started": "debug" | "info"
  "model.completed": "info"
  "model.retry_scheduled": "debug" | "warn"
  "model.failed": "warn" | "error"
  "interaction.started": "info"
  "interaction.completed": "info" | "warn"
  "tool.started": "debug" | "info"
  "tool.completed": "info"
  "tool.failed": "warn" | "error"
  "mcp.connection.completed": "info"
  "mcp.connection.failed": "warn" | "error"
  "mcp.connection.closed": "info" | "warn"
  "persistence.operation.completed": "debug" | "info"
  "persistence.operation.failed": "error"
  "presentation.handoff.opening": "info"
  "presentation.renderer.accepted": "info"
  "presentation.handoff.active": "info"
  "presentation.handoff.returning": "info"
  "presentation.gateway.connected": "info"
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
