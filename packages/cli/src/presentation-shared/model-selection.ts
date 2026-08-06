/** 跨端共享模型展示策略：当前选择优先，回退到握手运行时配置。 */

/** 所需的最小视图形状；与 InteractiveSnapshot 的 selection/catalogs/runtime 字段兼容。 */
type ModelSelectionView = {
  selection: {
    requestedModelProfileId: string | null
    actualModel: { id: string; model: string; provider_label: string } | null
  }
  catalogs: {
    models: { items: readonly { id: string; model: string; provider_label: string }[] }
  }
  runtime: { modelConfigured: boolean; modelName?: string; modelProfileId?: string }
}

/**
 * 生成当前展示模型文案：实际绑定优先，其次用户 /model 显式选择（从 catalog 取
 * profile 的 provider_label · model），最后回退到握手时的运行时配置。
 * TUI 与 Web 必须使用同一函数，避免两端在切换模型后显示不一致。
 */
export function modelSelectionLabel(view: ModelSelectionView): string {
  if (!view.runtime.modelConfigured) return "未配置模型"
  const requested = view.selection.requestedModelProfileId
  const profile = view.selection.actualModel
    ?? (requested ? view.catalogs.models.items.find(item => item.id === requested) : undefined)
  if (profile) return `${profile.provider_label} · ${profile.model}`
  if (view.runtime.modelProfileId) {
    return `${view.runtime.modelProfileId} · ${view.runtime.modelName ?? "已配置模型"}`
  }
  return view.runtime.modelName ?? "已配置模型"
}
