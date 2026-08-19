/**
 * UI 契约帧校验：尺寸、JSON、精确字段与类型白名单。
 *
 * 校验只做结构防御（形状/类型/长度），业务语义（Thread 是否存在、能力是否具备、
 * requestId 是否过期、工作区路径是否越界）由 Coordinator / InteractiveCore /
 * WorkspaceExplorer 决定，不在此重复。
 */

import type { IntentOutcome, InteractiveIntent } from "../../interactive/types"
import { APPROVAL_MODE_CYCLE } from "../../interactive/runtime"
import type { WorkspaceIntent, WorkspaceOutcome } from "../../workspace/types"
import { MAX_REQUEST_ID_LENGTH, MAX_UI_FRAME_BYTES, MAX_UI_TOKEN_LENGTH, type WebUiClientMessage, type WebUiPatch, type WebUiServerMessage, type WebUiState } from "./messages"
import type { PresentationState, ReturnReason } from "../state"

const textEncoder = new TextEncoder()

/** 解析一条 Browser → 网关帧；畸形、超限、未知类型一律返回 undefined（fail-closed）。 */
export function parseClientFrame(value: unknown): WebUiClientMessage | undefined {
  const parsed = parseFrame(value)
  if (parsed === undefined) return undefined
  if (!isRecord(parsed)) return undefined
  const type = parsed.type
  if (type === "interactive.intent") {
    if (!exactFields(parsed, ["type", "requestId", "revision", "intent"])) return undefined
    const requestId = parsed.requestId
    const revision = parsed.revision
    if (!isRequestId(requestId)) return undefined
    if (!isNonNegativeInt(revision)) return undefined
    const intent = parsed.intent
    if (!isInteractiveIntent(intent)) return undefined
    return { type: "interactive.intent", requestId, revision, intent }
  }
  if (type === "workspace.intent") {
    if (!exactFields(parsed, ["type", "requestId", "revision", "intent"])) return undefined
    const requestId = parsed.requestId
    const revision = parsed.revision
    if (!isRequestId(requestId)) return undefined
    if (!isNonNegativeInt(revision)) return undefined
    const intent = parsed.intent
    if (!isWorkspaceIntent(intent)) return undefined
    return { type: "workspace.intent", requestId, revision, intent }
  }
  if (type === "handoff.ready" || type === "handoff.return" || type === "handoff.exit") {
    return exactFields(parsed, ["type"]) ? { type } : undefined
  }
  return undefined
}

/** 解析一条网关 → Browser 帧；畸形或超限返回 undefined（Browser 防御性丢弃）。 */
export function parseServerFrame(value: unknown): WebUiServerMessage | undefined {
  const parsed = parseFrame(value)
  if (parsed === undefined) return undefined
  if (!isRecord(parsed)) return undefined
  const type = parsed.type
  if (type === "handoff.token") {
    if (!exactFields(parsed, ["type", "token"])) return undefined
    if (!isNonEmptyString(parsed.token, MAX_UI_TOKEN_LENGTH)) return undefined
    return { type: "handoff.token", token: parsed.token }
  }
  if (type === "state.replace") {
    if (!exactFields(parsed, ["type", "revision", "state"])) return undefined
    if (!isNonNegativeInt(parsed.revision)) return undefined
    const state = isWebUiState(parsed.state)
    if (!state) return undefined
    return { type: "state.replace", revision: parsed.revision, state }
  }
  if (type === "state.patch") {
    if (!exactFields(parsed, ["type", "revision", "patch"])) return undefined
    if (!isNonNegativeInt(parsed.revision)) return undefined
    const patch = isWebUiPatch(parsed.patch)
    if (!patch) return undefined
    return { type: "state.patch", revision: parsed.revision, patch }
  }
  if (type === "intent.outcome") {
    if (!exactFields(parsed, ["type", "requestId", "domain", "outcome"])) return undefined
    if (!isRequestId(parsed.requestId)) return undefined
    const domain = parsed.domain
    if (domain === "interactive") {
      if (!isIntentOutcome(parsed.outcome)) return undefined
      return { type: "intent.outcome", requestId: parsed.requestId, domain, outcome: parsed.outcome }
    }
    if (domain === "workspace") {
      if (!isWorkspaceOutcome(parsed.outcome)) return undefined
      return { type: "intent.outcome", requestId: parsed.requestId, domain, outcome: parsed.outcome }
    }
    return undefined
  }
  if (type === "handoff.state") {
    if (!exactFields(parsed, ["type", "state"])) return undefined
    const state = isPresentationState(parsed.state)
    if (!state) return undefined
    return { type: "handoff.state", state }
  }
  return undefined
}

function parseFrame(value: unknown): unknown {
  if (typeof value !== "string") return undefined
  if (textEncoder.encode(value).byteLength > MAX_UI_FRAME_BYTES) return undefined
  try {
    return JSON.parse(value)
  } catch {
    return undefined
  }
}

function isRequestId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_REQUEST_ID_LENGTH
}

function isNonNegativeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
}

function isNonEmptyString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean"
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  const keys = Object.keys(value)
  return keys.length === fields.length && fields.every(field => field in value)
}

/** InteractiveIntent 类型白名单；payload 语义由 Controller 校验，这里只做形状防御。 */
const INTENT_TYPES = new Set([
  "input.submit",
  "command.execute",
  "run.cancel",
  "catalog.refresh",
  "thread.open",
  "model.select",
  "skill.arm",
  "skill.clear",
  "skill.set-enabled",
  "mcp.add",
  "mcp.remove",
  "interaction.respond",
  "confirmation.resolve",
  "approval-mode.cycle",
  "approval-mode.set",
])

function isInteractiveIntent(value: unknown): value is InteractiveIntent {
  if (!isRecord(value)) return false
  const type = value.type
  if (typeof type !== "string" || !INTENT_TYPES.has(type)) return false
  switch (type) {
    case "input.submit":
      return exactFields(value, ["type", "value"]) && isString(value.value, 64 * 1024)
    case "command.execute": {
      // argument 与类型契约（InteractiveIntent.command.execute.argument?）一致为可选；
      // 键必须是 type/commandId/argument 子集且不得带未知键（长度 2 或 3）。
      const keys = Object.keys(value)
      const knownKeys = keys.every(key => key === "type" || key === "commandId" || key === "argument")
      return knownKeys
        && (keys.length === 2 || keys.length === 3)
        && isNonEmptyString(value.commandId, 128)
        && (value.argument === undefined || isString(value.argument, 64 * 1024))
    }
    case "run.cancel":
      return exactFields(value, ["type"])
    case "catalog.refresh":
      return exactFields(value, ["type", "catalog"])
        && (value.catalog === "threads" || value.catalog === "models" || value.catalog === "skills" || value.catalog === "mcp")
    case "thread.open":
      return exactFields(value, ["type", "threadId"]) && isNonEmptyString(value.threadId, 256)
    case "model.select":
      return exactFields(value, ["type", "profileId"]) && isNonEmptyString(value.profileId, 256)
    case "skill.arm":
      return exactFields(value, ["type", "skillId"]) && isNonEmptyString(value.skillId, 256)
    case "skill.clear":
      return exactFields(value, ["type"])
    case "skill.set-enabled":
      return exactFields(value, ["type", "skillId", "enabled"])
        && isNonEmptyString(value.skillId, 256)
        && isBoolean(value.enabled)
    case "mcp.add":
      return exactFields(value, ["type", "input"]) && isMcpAddInput(value.input)
    case "mcp.remove":
      return exactFields(value, ["type", "name"]) && isNonEmptyString(value.name, 256)
    case "interaction.respond":
      return exactFields(value, ["type", "requestId", "response"])
        && isRequestId(value.requestId)
        && isInteractionResponse(value.response)
    case "confirmation.resolve":
      return exactFields(value, ["type", "confirmationId", "confirmed"])
        && isNonEmptyString(value.confirmationId, 128)
        && isBoolean(value.confirmed)
    case "approval-mode.cycle":
      return exactFields(value, ["type"])
    case "approval-mode.set":
      return exactFields(value, ["type", "mode"]) && APPROVAL_MODE_CYCLE.some(mode => mode === value.mode)
    default:
      return false
  }
}

function isString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length <= maxLength
}

/** MCP 添加输入只校验最小结构；完整参数校验由 Controller 的 mcp.add 执行。 */
function isMcpAddInput(value: unknown): boolean {
  if (!isRecord(value)) return false
  if (!isNonEmptyString(value.name, 256)) return false
  return true
}

function isInteractionResponse(value: unknown): boolean {
  if (!isRecord(value)) return false
  if (value.kind === "approval") {
    if (!exactFields(value, ["kind", "decision", "feedback"]) && !exactFields(value, ["kind", "decision"])) return false
    if (typeof value.decision !== "string" || value.decision.length > 64) return false
    if (value.feedback !== undefined && !isString(value.feedback, 64 * 1024)) return false
    return true
  }
  if (value.kind === "question") {
    if (!exactFields(value, ["kind", "answers"])) return false
    if (!isRecord(value.answers)) return false
    for (const answer of Object.values(value.answers)) {
      if (!Array.isArray(answer) || answer.some(item => typeof item !== "string" || item.length > 64 * 1024)) return false
    }
    return true
  }
  if (value.kind === "directory_trust") {
    return exactFields(value, ["kind", "decision"])
      && typeof value.decision === "string"
      && value.decision.length <= 64
  }
  return false
}

/** WorkspaceIntent 白名单：5 个 intent，精确字段校验；路径语义由 Explorer 决定。 */
const WORKSPACE_INTENT_TYPES = new Set([
  "workspace.load",
  "workspace.refresh",
  "workspace.toggle-directory",
  "workspace.preview-file",
  "workspace.refresh-preview",
])

function isWorkspaceIntent(value: unknown): value is WorkspaceIntent {
  if (!isRecord(value)) return false
  const type = value.type
  if (typeof type !== "string" || !WORKSPACE_INTENT_TYPES.has(type)) return false
  switch (type) {
    case "workspace.load":
    case "workspace.refresh":
      return exactFields(value, ["type"])
    case "workspace.toggle-directory":
    case "workspace.preview-file":
    case "workspace.refresh-preview":
      return exactFields(value, ["type", "path"]) && isNonEmptyString(value.path, 4096)
    default:
      return false
  }
}

/** state.replace 的完整状态：八个分片全部必须存在（缺失视为畸形帧）。 */
function isWebUiState(value: unknown): WebUiState | undefined {
  if (!isRecord(value)) return undefined
  const slices = ["conversation", "interaction", "navigation", "command", "runtime", "workItem", "workspaceTree", "workspacePreview"]
  if (!exactFields(value, slices)) return undefined
  if (!isRecord(value.conversation) || !isRecord(value.interaction) || !isRecord(value.navigation) || !isRecord(value.command) || !isRecord(value.runtime) || !isRecord(value.workItem)) return undefined
  if (!isWorkspaceTreeView(value.workspaceTree)) return undefined
  if (!isWorkspacePreviewView(value.workspacePreview)) return undefined
  return value as unknown as WebUiState
}

/** state.patch 的分片形状校验；只要求各分片存在时是 record。 */
function isWebUiPatch(value: unknown): WebUiPatch | undefined {
  if (!isRecord(value)) return undefined
  const keys = Object.keys(value)
  if (keys.length === 0) return undefined
  const allowed = new Set(["conversation", "interaction", "navigation", "command", "runtime", "workItem", "workspaceTree", "workspacePreview"])
  if (!keys.every(key => allowed.has(key))) return undefined
  for (const key of keys) {
    if (key === "workspaceTree") {
      if (!isWorkspaceTreeView(value[key])) return undefined
    } else if (key === "workspacePreview") {
      if (!isWorkspacePreviewView(value[key])) return undefined
    } else if (!isRecord(value[key])) {
      return undefined
    }
  }
  return value as unknown as WebUiPatch
}

/** 文件树视图：status 联合 + 固定字段；message 仅 error 时存在。 */
function isWorkspaceTreeView(value: unknown): boolean {
  if (!isRecord(value)) return false
  const keys = Object.keys(value)
  const allowed = new Set(["status", "rows", "selectedPath", "limited", "message"])
  if (!keys.every(key => allowed.has(key))) return false
  if (value.status !== "idle" && value.status !== "loading" && value.status !== "ready" && value.status !== "error") return false
  if (!Array.isArray(value.rows)) return false
  for (const row of value.rows) {
    if (!isWorkspaceTreeRow(row)) return false
  }
  if (value.selectedPath !== null && typeof value.selectedPath !== "string") return false
  if (typeof value.limited !== "boolean") return false
  if (value.message !== undefined && typeof value.message !== "string") return false
  return true
}

/** 单行树节点：kind 枚举 + 固定字段。 */
function isWorkspaceTreeRow(value: unknown): boolean {
  if (!isRecord(value)) return false
  if (!exactFields(value, ["path", "name", "kind", "depth", "expanded", "loading", "hasChildren"])) return false
  if (typeof value.path !== "string" || typeof value.name !== "string") return false
  if (value.kind !== "directory" && value.kind !== "file" && value.kind !== "symlink") return false
  const depth = value.depth
  if (typeof depth !== "number" || !Number.isSafeInteger(depth) || depth < 0) return false
  if (typeof value.expanded !== "boolean" || typeof value.loading !== "boolean" || typeof value.hasChildren !== "boolean") return false
  return true
}

/** 预览状态机：status 判别 + 各分支固定字段。 */
function isWorkspacePreviewView(value: unknown): boolean {
  if (!isRecord(value)) return false
  const status = value.status
  if (status === "idle") return exactFields(value, ["status"])
  if (status === "loading") {
    return exactFields(value, ["status", "path"]) && isString(value.path, 4096)
  }
  if (status === "ready") {
    if (!exactFields(value, ["status", "file"])) return false
    return isWorkspaceFilePreview(value.file)
  }
  if (status === "unsupported") {
    if (!exactFields(value, ["status", "path", "reason", "sizeBytes"])) return false
    if (!isString(value.path, 4096) || !isString(value.reason, 2048)) return false
    return Number.isSafeInteger(value.sizeBytes) && (value.sizeBytes as number) >= 0
  }
  if (status === "error") {
    if (!exactFields(value, ["status", "path", "code", "message"])) return false
    if (!isString(value.path, 4096) || !isString(value.message, 4096)) return false
    // 与 isWorkspaceOutcome 共用稳定错误码白名单。
    return typeof value.code === "string" && WORKSPACE_ERROR_CODES.has(value.code)
  }
  return false
}

/** 预览文件载荷：固定字段与类型。 */
function isWorkspaceFilePreview(value: unknown): boolean {
  if (!isRecord(value)) return false
  const fields = ["path", "name", "content", "language", "sizeBytes", "lineCount", "modifiedAtMs", "truncated", "version"]
  if (!exactFields(value, fields)) return false
  if (!isString(value.path, 4096) || !isString(value.name, 1024) || !isString(value.content, 512 * 1024)) return false
  if (value.language !== null && typeof value.language !== "string") return false
  if (!Number.isSafeInteger(value.sizeBytes) || (value.sizeBytes as number) < 0) return false
  if (!Number.isSafeInteger(value.lineCount) || (value.lineCount as number) < 0) return false
  if (typeof value.modifiedAtMs !== "number" || !isString(value.version, 256)) return false
  return typeof value.truncated === "boolean"
}

function isIntentOutcome(value: unknown): value is IntentOutcome {
  if (!isRecord(value)) return false
  if (value.status === "accepted") {
    if (!exactFields(value, ["status", "effects"]) && !exactFields(value, ["status"])) return false
    if (value.effects !== undefined && !Array.isArray(value.effects)) return false
    return true
  }
  if (value.status === "rejected") {
    if (!exactFields(value, ["status", "code", "message"])) return false
    if (typeof value.code !== "string" || value.code.length > 64) return false
    if (!isString(value.message, 4096)) return false
    return true
  }
  return false
}

/** WorkspaceOutcome 校验：错误码必须是稳定白名单。 */
const WORKSPACE_ERROR_CODES = new Set([
  "invalid-path",
  "outside-workspace",
  "not-found",
  "permission-denied",
  "not-directory",
  "not-file",
  "unsupported-file",
  "unsupported-encoding",
  "workspace-too-large",
  "workspace-changed",
  "io-error",
  "invalid-argument",
])

function isWorkspaceOutcome(value: unknown): value is WorkspaceOutcome {
  if (!isRecord(value)) return false
  if (value.status === "accepted") return exactFields(value, ["status"])
  if (value.status === "rejected") {
    if (!exactFields(value, ["status", "code", "message"])) return false
    if (typeof value.code !== "string" || !WORKSPACE_ERROR_CODES.has(value.code)) return false
    if (!isString(value.message, 4096)) return false
    return true
  }
  return false
}

function isPresentationState(value: unknown): PresentationState | undefined {
  if (!isRecord(value)) return undefined
  if (value.phase === "tui-active") {
    return exactFields(value, ["phase"]) ? { phase: "tui-active" } : undefined
  }
  if (value.phase === "opening-web" || value.phase === "web-active") {
    if (!exactFields(value, ["phase", "handoffId"])) return undefined
    if (typeof value.handoffId !== "string" || value.handoffId.length === 0 || value.handoffId.length > 256) return undefined
    return { phase: value.phase, handoffId: value.handoffId }
  }
  if (value.phase === "returning-tui") {
    if (!exactFields(value, ["phase", "handoffId", "reason"])) return undefined
    if (typeof value.handoffId !== "string" || value.handoffId.length === 0 || value.handoffId.length > 256) return undefined
    const reason = isReturnReason(value.reason)
    if (!reason) return undefined
    return { phase: "returning-tui", handoffId: value.handoffId, reason }
  }
  return undefined
}

/** ReturnReason 白名单；未知原因视为畸形帧。 */
function isReturnReason(value: unknown): ReturnReason | undefined {
  if (typeof value !== "string") return undefined
  return RETURN_REASONS.has(value as ReturnReason) ? value as ReturnReason : undefined
}

const RETURN_REASONS = new Set<ReturnReason>([
  "browser-close",
  "ready-timeout",
  "invalid-message",
  "returned",
  "exit-requested",
  "opener-failed",
  "cli-exit",
])
