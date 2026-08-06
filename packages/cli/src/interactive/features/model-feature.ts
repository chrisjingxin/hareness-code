/** Model Feature：管理模型 Profile 选择、默认模型同步与热切换绑定确认。 */

import { Capability, type ConfigChange, type ModelProfile } from "@za38/protocol"
import type { IntentOutcome, InteractiveConfirmation } from "../ports"
import { appendNotice } from "../state"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 默认模型同步失败的稳定原因映射；不把原始错误透出给界面。 */
function safeModelDefaultSyncError(error: unknown): string {
  const reason = error instanceof Error ? error.message : "配置服务暂时不可用"
  const messages: Record<string, string> = {
    CONFIG_WRITE_CAPABILITY_REQUIRED: "当前客户端未协商 config.write",
    CONFIG_FIELD_NOT_ALLOWED: "默认模型字段不可写",
    CONFIG_FIELD_NOT_WRITABLE: "默认模型字段当前不可写",
    CONFIG_USER_FILE_MISSING: "用户配置文件不存在",
    MANAGED_POLICY_LOCKED: "默认模型字段受受管策略锁定",
    SOURCE_OVERRIDE_ACTIVE: "默认模型由更高优先级来源覆盖",
    UNTRUSTED_PROJECT_CONFIGURATION: "项目配置不允许写入用户默认值",
    EXPLICIT_CONFIGURATION_ACTIVE: "当前显式配置来源不可写",
    CONFIG_REVISION_CONFLICT: "配置已被其他操作修改，请重试",
    CONFIG_WRITE_FAILED: "用户配置写入失败",
    MODEL_PROFILE_NOT_FOUND: "所选模型 Profile 不存在",
    MODEL_PROFILE_UNAVAILABLE: "所选模型不可用",
    MODEL_PROFILE_CAPABILITY_MISSING: "所选模型缺少 Single Agent 所需能力",
  }
  return messages[reason] ?? "配置服务暂时不可用"
}

export class ModelFeature {
  requestedModelProfileId: string | null = null
  actualModelProfile: ModelProfile | undefined
  /** 用户是否在本会话显式选择过模型；为 true 时 catalog 刷新不得用持久化选择覆盖。 */
  explicitlySelected = false

  /** 先改变当前 Thread 的下一次模型，再独立同步未来新 Thread 默认值。 */
  async selectModel(
    profileId: string,
    ctx: FeatureContext,
    options: {
      models: readonly ModelProfile[]
      onModelsRefreshed: () => Promise<void>
    },
  ): Promise<IntentOutcome> {
    const model = options.models.find(item => item.id === profileId)
    if (!model || !model.available) {
      ctx.commit(current => appendNotice(
        current,
        model
          ? `${model.provider_label} · ${model.model} 不可用：${model.unavailable_reason ?? "配置不可用"}`
          : "所选模型 Profile 不存在。",
      ))
      return { status: "rejected", code: "not-found", message: "Model profile not found or unavailable" }
    }

    this.requestedModelProfileId = model.id
    // 显式选择取代上一次运行的实际绑定：展示跟随意图，直到下一次 Run 产生新事实。
    this.actualModelProfile = undefined
    this.explicitlySelected = true
    ctx.publish()
    const label = `${model.provider_label} · ${model.model}`
    try {
      await this.syncDefaultModel(model.id, ctx, options.onModelsRefreshed)
      ctx.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；后续新 Thread 默认模型已同步。`))
    } catch (error) {
      ctx.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；未来新 Thread 默认未更新：${safeModelDefaultSyncError(error)}`))
    }
    return { status: "accepted" }
  }

  /** 同步未来新 Thread 的默认模型；失败只影响默认值，不回收当前选择。 */
  private async syncDefaultModel(
    profileId: string,
    ctx: FeatureContext,
    onModelsRefreshed: () => Promise<void>,
  ): Promise<void> {
    if (!ctx.baseRuntime.capabilities?.includes(Capability.CONFIG_WRITE)) {
      throw new Error("CONFIG_WRITE_CAPABILITY_REQUIRED")
    }
    const details = await ctx.gateway.configDetails()
    const field = details.fields.find(value => value && typeof value === "object" && (value as Record<string, unknown>).path === "models.default_profile")
    if (!field) throw new Error("CONFIG_FIELD_NOT_ALLOWED")
    const record = field as Record<string, unknown>
    if (record.editable !== true) {
      throw new Error(typeof record.unavailable_reason === "string" ? record.unavailable_reason : "CONFIG_FIELD_NOT_WRITABLE")
    }
    if (record.value !== profileId) {
      const changes: ConfigChange[] = [{ path: "models.default_profile", value: profileId }]
      const preview = await ctx.gateway.previewConfig(changes)
      await ctx.gateway.commitConfig(preview.revision, changes)
    }
    await onModelsRefreshed()
  }

  /** 检查当前 Thread 是否使用 legacy immutable binding；返回 confirmation 或 null。 */
  async modelBindingConfirmation(ctx: FeatureContext): Promise<InteractiveConfirmation | null> {
    const threadId = ctx.getState().currentThreadId
    if (!threadId) return null
    if (ctx.baseRuntime.capabilities?.includes(Capability.MODELS_SELECT) === true) return null

    try {
      const result = await ctx.gateway.listModels(threadId)
      const binding = result.thread_binding
      const roles = binding && typeof binding === "object" ? (binding as Record<string, unknown>).roles : undefined
      const executor = roles && typeof roles === "object" ? (roles as Record<string, unknown>).executor : undefined
      const executorRecord = executor && typeof executor === "object" ? executor as Record<string, unknown> : undefined
      return {
        confirmationId: "model-binding",
        title: "当前 Thread 的模型不可变",
        message: executorRecord
          ? `当前 Thread 已绑定 ${stringValue(executorRecord.provider_label, "unknown")} · ${stringValue(executorRecord.model, "unknown")}（${stringValue(executorRecord.id, "unknown")}）。请新建 Thread 后使用 /model 选择模型。`
          : "当前 Thread 使用 legacy immutable binding，不能热切换模型。请新建 Thread 后使用 /model 选择模型。",
        confirmLabel: "新建 Thread",
        cancelLabel: "保留当前 Thread",
      }
    } catch {
      return {
        confirmationId: "model-binding",
        title: "模型绑定不可读取",
        message: "无法读取当前 Thread 的模型绑定。请新建 Thread 后再选择模型。",
        confirmLabel: "新建 Thread",
        cancelLabel: "保留当前 Thread",
      }
    }
  }
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback
}
