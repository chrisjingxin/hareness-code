/** Selector 输出类型：可序列化视图与 FeatureAvailability。 */

/** 从 snapshot 推导的展示可用性；全布尔、可 JSON 序列化，presentation 不再直接判断协议 Capability。 */
export type FeatureAvailability = {
  readonly canSubmit: boolean
  readonly canCancelRun: boolean
  readonly canOpenThread: boolean
  readonly canToggleSkill: boolean
  readonly canManageMcp: boolean
  readonly canChangeModel: boolean
  readonly canOpenModelsPanel: boolean
  readonly canOpenSkillsPanel: boolean
  readonly canOpenMcpPanel: boolean
  /** 纯 capability 门：是否具备对应协议能力；不折叠 busy 状态，面板在 run 期间保持可见仅禁用。 */
  readonly hasSkillManage: boolean
  readonly hasMcpManage: boolean
}

/** 在 snapshot 层做 capability 判断的合法白名单；其余能力判断必须下沉到 Core。 */
export const CAPABILITY_GATE = {
  toggleSkill: "skills.manage",
  manageMcp: "mcp.manage",
  changeModel: "models.select",
  openModelsPanel: "models.read",
  openSkillsPanel: "skills.read",
  openMcpPanel: "mcp.read",
} as const
