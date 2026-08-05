/**
 * UI 契约帧校验：尺寸、JSON、精确字段与类型白名单。
 *
 * 校验只做结构防御（形状/类型/长度），业务语义（Thread 是否存在、能力是否具备、
 * requestId 是否过期）由 Coordinator / InteractiveCore 决定，不在此重复。
 */

import type { IntentOutcome, InteractiveIntent } from "../../interactive/types"
import { MAX_REQUEST_ID_LENGTH, MAX_UI_FRAME_BYTES, type WebPresentationIntent, type WebUiClientMessage, type WebUiPatch, type WebUiServerMessage, type WebUiState } from "./messages"
import type { PresentationState, ReturnReason } from "../state"

const textEncoder = new TextEncoder()

/** 解析一条 Browser → 网关帧；畸形、超限、未知类型一律返回 undefined（fail-closed）。 */
export function parseClientFrame(value: unknown): WebUiClientMessage | undefined {
  const parsed = parseFrame(value)
  if (parsed === undefined) return undefined
  if (!isRecord(parsed)) return undefined
  const type = parsed.type
  if (type === "intent") {
    if (!exactFields(parsed, ["type", "requestId", "revision", "intent"])) return undefined
    const requestId = parsed.requestId
    const revision = parsed.revision
    if (!isRequestId(requestId)) return undefined
    if (!isNonNegativeInt(revision)) return undefined
    const intent = parsed.intent
    if (!isInteractiveIntent(intent)) return undefined
    return { type: "intent", requestId, revision, intent }
  }
  if (type === "presentation-intent") {
    if (!exactFields(parsed, ["type", "intent"])) return undefined
    const intent = isPresentationIntent(parsed.intent)
    if (!intent) return undefined
    return { type: "presentation-intent", intent }
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
    if (!exactFields(parsed, ["type", "requestId", "outcome"])) return undefined
    if (!isRequestId(parsed.requestId)) return undefined
    if (!isIntentOutcome(parsed.outcome)) return undefined
    return { type: "intent.outcome", requestId: parsed.requestId, outcome: parsed.outcome }
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
])

function isInteractiveIntent(value: unknown): value is InteractiveIntent {
  if (!isRecord(value)) return false
  const type = value.type
  if (typeof type !== "string" || !INTENT_TYPES.has(type)) return false
  switch (type) {
    case "input.submit":
      return exactFields(value, ["type", "value"]) && isString(value.value, 64 * 1024)
    case "command.execute":
      return exactFields(value, ["type", "commandId", "argument"])
        && isNonEmptyString(value.commandId, 128)
        && (value.argument === undefined || isString(value.argument, 64 * 1024))
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
  return false
}

function isPresentationIntent(value: unknown): WebPresentationIntent | undefined {
  if (!isRecord(value)) return undefined
  if (value.type === "theme.set") {
    if (!exactFields(value, ["type", "theme"])) return undefined
    if (value.theme !== "light" && value.theme !== "dark") return undefined
    return { type: "theme.set", theme: value.theme }
  }
  if (value.type === "panel.open") {
    if (!exactFields(value, ["type", "panel"])) return undefined
    if (typeof value.panel !== "string" || value.panel.length > 64) return undefined
    return { type: "panel.open", panel: value.panel }
  }
  if (value.type === "panel.close") {
    return exactFields(value, ["type"]) ? { type: "panel.close" } : undefined
  }
  return undefined
}

/** state.replace 的完整状态：五个分片全部必须存在（缺失视为畸形帧）。 */
function isWebUiState(value: unknown): WebUiState | undefined {
  if (!isRecord(value)) return undefined
  if (!exactFields(value, ["conversation", "interaction", "navigation", "command", "runtime"])) return undefined
  if (!isRecord(value.conversation) || !isRecord(value.interaction) || !isRecord(value.navigation) || !isRecord(value.command) || !isRecord(value.runtime)) return undefined
  return value as unknown as WebUiState
}

/** state.patch 的分片形状校验；只要求各分片存在时是 record。 */
function isWebUiPatch(value: unknown): WebUiPatch | undefined {
  if (!isRecord(value)) return undefined
  const keys = Object.keys(value)
  if (keys.length === 0) return undefined
  if (!keys.every(key => key === "conversation" || key === "interaction" || key === "navigation" || key === "command" || key === "runtime")) return undefined
  for (const key of keys) {
    if (!isRecord(value[key])) return undefined
  }
  return value as unknown as WebUiPatch
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
