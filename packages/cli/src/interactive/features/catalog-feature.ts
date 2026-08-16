import type { McpServerStatus, ModelProfile, ThreadSummary } from "@za38/protocol"
import type { LoadableCatalog, SkillSummary } from "../ports"
import type { SkillMenuItem } from "../commands"
import type { FeatureContext } from "./types"

function skillMenuItem(skill: Record<string, unknown>): SkillMenuItem | undefined {
  const id = typeof skill.id === "string" ? skill.id : undefined
  if (!id) return undefined
  return {
    id,
    name: typeof skill.name === "string" ? skill.name : id,
    description: typeof skill.description === "string" ? skill.description : "",
    source: typeof skill.source === "string" ? skill.source : "user",
    enabled: typeof skill.enabled === "boolean" ? skill.enabled : true,
    userInvocable: typeof skill.user_invocable === "boolean" ? skill.user_invocable : true,
    argumentHint: typeof skill.argument_hint === "string" ? skill.argument_hint : undefined,
  }
}

/** 按最近更新时间降序排列 Thread；TUI 与 Web 共用同一排序，避免两端顺序不一致。 */
export function sortThreadsByRecency(threads: readonly ThreadSummary[]): ThreadSummary[] {
  return threads.slice().sort((a, b) => b.updated_at_ms - a.updated_at_ms)
}

export type CatalogKey = "threads" | "models" | "skills" | "mcp"

export type InternalCatalog<T> = LoadableCatalog<T> & { epoch: number }

export type CatalogState = {
  threads: InternalCatalog<ThreadSummary>
  models: InternalCatalog<ModelProfile>
  skills: InternalCatalog<SkillMenuItem>
  mcp: InternalCatalog<McpServerStatus>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export class CatalogFeature {
  readonly state: CatalogState = {
    threads: { status: "idle", items: [], epoch: 0 },
    models: { status: "idle", items: [], epoch: 0 },
    skills: { status: "idle", items: [], epoch: 0 },
    mcp: { status: "idle", items: [], epoch: 0 },
  }

  private closed = false

  close(): void {
    this.closed = true
  }

  /** 重置某项或者全量 Catalog 为 idle 状态。Skills 与 MCP 属于全局配置，跨 Thread 保持不变。 */
  reset(options: { models?: readonly ModelProfile[] } = {}, ctx: FeatureContext): void {
    this.state.threads = { status: "idle", items: [], epoch: this.state.threads.epoch }
    if (options.models) {
      this.state.models = { status: "ready", items: options.models, epoch: this.state.models.epoch }
    } else {
      this.state.models = { status: "idle", items: [], epoch: this.state.models.epoch }
    }
    ctx.publish()
  }

  /** 刷新指定 Catalog；每个 Catalog 有独立 epoch，刷新 A 不取消 B。 */
  async refreshCatalog(key: CatalogKey, ctx: FeatureContext, onModelSelection?: (profileId: string) => void): Promise<void> {
    if (key === "threads") await this.refreshThreadCatalog(ctx)
    else if (key === "models") await this.refreshModelCatalog(ctx, onModelSelection)
    else if (key === "skills") await this.refreshSkillCatalog(ctx)
    else await this.refreshMcpCatalog(ctx)
  }

  /** 读取 Thread catalog；异步结果只允许写回对应打开轮次。 */
  async refreshThreadCatalog(ctx: FeatureContext): Promise<void> {
    const epoch = ++this.state.threads.epoch
    this.state.threads = { status: "loading", items: this.state.threads.items, epoch }
    ctx.publish()
    try {
      const result = await ctx.gateway.listThreads()
      if (this.closed || epoch !== this.state.threads.epoch) return
      this.state.threads = { status: "ready", items: sortThreadsByRecency(result.threads), epoch }
      ctx.publish()
    } catch (error) {
      if (this.closed || epoch !== this.state.threads.epoch) return
      this.state.threads = { status: "error", items: this.state.threads.items, message: errorMessage(error), epoch }
      ctx.publish()
    }
  }

  /** 读取 Model catalog；与当前 thread 的绑定和最近运行绑定一起收敛。 */
  async refreshModelCatalog(ctx: FeatureContext, onModelSelection?: (profileId: string) => void): Promise<void> {
    const epoch = ++this.state.models.epoch
    this.state.models = { status: "loading", items: this.state.models.items, epoch }
    ctx.publish()
    try {
      const currentThreadId = ctx.getState().currentThreadId
      const result = await ctx.gateway.listModels(currentThreadId ?? undefined)
      if (this.closed || epoch !== this.state.models.epoch) return
      this.state.models = { status: "ready", items: result.profiles, epoch }
      if (result.thread_selection?.primary_profile && !ctx.getState().activeRun) {
        onModelSelection?.(result.thread_selection.primary_profile)
      }
      ctx.publish()
    } catch (error) {
      if (this.closed || epoch !== this.state.models.epoch) return
      this.state.models = { status: "error", items: this.state.models.items, message: errorMessage(error), epoch }
      ctx.publish()
    }
  }

  /** 读取 Skill catalog；始终拉取权威全集（含 disabled）。 */
  async refreshSkillCatalog(ctx: FeatureContext): Promise<void> {
    const epoch = ++this.state.skills.epoch
    this.state.skills = { status: "loading", items: this.state.skills.items, epoch }
    ctx.publish()
    try {
      const result = await ctx.gateway.listSkills(true)
      if (this.closed || epoch !== this.state.skills.epoch) return
      const next = Array.isArray(result.skills)
        ? result.skills.map((item: any) => skillMenuItem(item)).filter((item): item is SkillMenuItem => item !== undefined)
        : []
      this.state.skills = { status: "ready", items: next, epoch }
      ctx.publish()
    } catch (error) {
      if (this.closed || epoch !== this.state.skills.epoch) return
      this.state.skills = { status: "error", items: this.state.skills.items, message: errorMessage(error), epoch }
      ctx.publish()
    }
  }

  /** 读取 MCP catalog。 */
  async refreshMcpCatalog(ctx: FeatureContext): Promise<void> {
    const epoch = ++this.state.mcp.epoch
    this.state.mcp = { status: "loading", items: this.state.mcp.items, epoch }
    ctx.publish()
    try {
      const result = await ctx.gateway.mcpStatus()
      if (this.closed || epoch !== this.state.mcp.epoch) return
      this.state.mcp = { status: "ready", items: result.servers, epoch }
      ctx.publish()
    } catch (error) {
      if (this.closed || epoch !== this.state.mcp.epoch) return
      this.state.mcp = { status: "error", items: this.state.mcp.items, message: errorMessage(error), epoch }
      ctx.publish()
    }
  }
}
