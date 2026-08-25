/** 共享 Web presentation 测试 fixture：构造最小合法的 InteractiveSnapshot / WebAdapterSnapshot。 */

import type {
  CommandMenuItem,
} from "../../../src/interactive/commands"
import type {
  AgentSummary,
  InteractiveSnapshot,
  InteractiveConfirmation,
  InteractiveInteraction,
  InteractiveQuestion,
  LoadableCatalog,
  McpServerSummary,
  ModelProfile,
  SkillSummary,
  ThreadSummary,
} from "../../../src/interactive/types"
import type { InteractiveRuntime } from "../../../src/interactive/runtime"
import type { WebAdapterSnapshot } from "../../../src/web/application/adapter"

type PanelState = WebAdapterSnapshot["panelSearch"]

/** 默认 runtime：只携带 capability 子集，配置为已连接且本地执行。 */
export function makeRuntime(overrides: Partial<InteractiveRuntime> = {}): InteractiveRuntime {
  return {
    workspace: "/workspace",
    cliVersion: "0.1.0",
    modelConfigured: true,
    modelName: "test-model",
    executionMode: "local",
    approvalMode: "default",
    capabilities: [
      "threads.read",
      "models.read",
      "models.select",
      "skills.read",
      "mcp.read",
      "agents.read",
    ],
    ...overrides,
  }
}

/** 单一 catalog 状态机；测试需要时可手动覆盖 status / items / message。 */
export function makeCatalog<T>(
  items: readonly T[] = [],
  status: LoadableCatalog<T>["status"] = "ready",
  message = "",
): LoadableCatalog<T> {
  return {
    status,
    items: items as T[],
    ...(status === "error" ? { message } : {}),
  } as LoadableCatalog<T>
}

/** 默认 InteractiveSnapshot；用 overrides 替换局部字段。 */
export function makeInteractive(
  overrides: Partial<InteractiveSnapshot> = {},
): InteractiveSnapshot {
  return {
    currentThreadId: null,
    activity: { kind: "home", label: "就绪" },
    activeRun: null,
    timeline: [],
    runProgress: null,
    interaction: null,
    confirmation: null,
    lastRun: null,
    runtime: makeRuntime(),
    connection: { status: "open" },
    commands: [],
    catalogs: {
      threads: makeCatalog<ThreadSummary>([]),
      models: makeCatalog<ModelProfile>([]),
      skills: makeCatalog<SkillSummary>([]),
      mcp: makeCatalog<McpServerSummary>([]),
      agents: makeCatalog<AgentSummary>([]),
    },
    selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
    childTimelineExecutionId: null,
    ...overrides,
  }
}

/** 默认 WebAdapterSnapshot；interactive 字段可独立覆盖。 */
export function makeSnapshot(overrides: Partial<WebAdapterSnapshot> = {}): WebAdapterSnapshot {
  const panelSearch: PanelState = {
    code: { query: "", submitting: false, error: null },
    models: { query: "", submitting: false, error: null },
    skills: { query: "", submitting: false, error: null },
    mcp: { query: "", submitting: false, error: null },
    agents: { query: "", submitting: false, error: null },
    status: { query: "", submitting: false, error: null },
    help: { query: "", submitting: false, error: null },
  }
  return {
    interactive: makeInteractive(),
    draft: "",
    commandMenuOpen: false,
    commandMenuIndex: 0,
    commandOptions: [],
    contextDock: {
      open: false,
      activePanel: "code",
      widthPx: 560,
      code: { tabs: [], activePath: null, previews: {}, previewErrors: {} },
    },
    workspaceTree: { status: "idle", rows: [], selectedPath: null, limited: false },
    workspaceSidebar: { threadRatio: 0.38, threadRatioCustomized: false, selectedPath: null, widthPx: 280 },
    panelSearch,
    expandedTools: new Set<string>(),
    interactionDraft: null,
    leaving: false,
    threadNewSubmitting: false,
    composerFocusRequest: 0,
    transientNotice: null,
    scrollRequest: null,
    confirmationId: null,
    theme: "light",
    headerMenuOpen: false,
    ...overrides,
  }
}

/** 单个 Thread fixture：thread_id 稳定，可选字段有默认值。 */
export function makeThread(overrides: Partial<ThreadSummary> = {}): ThreadSummary {
  return {
    thread_id: "thread-1",
    created_at_ms: Date.now() - 60_000,
    updated_at_ms: Date.now() - 5_000,
    first_message: "first user prompt",
    latest_message: "latest assistant reply",
    message_count: 3,
    ...overrides,
  }
}

/** 单个 Model Profile fixture。 */
export function makeModel(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "profile-a",
    model: "gpt-x",
    provider_label: "Provider A",
    context_window_tokens: 8000,
    capabilities: [],
    is_default: true,
    available: true,
    source: "config",
    ...overrides,
  }
}

/** 单个 Skill catalog item。 */
export function makeSkill(overrides: Partial<SkillSummary> = {}): SkillSummary {
  return {
    id: "skill-a",
    name: "Skill A",
    description: "demo skill",
    source: "builtin",
    enabled: true,
    userInvocable: true,
    ...overrides,
  }
}

/** 单个可派发 Agent 摘要。 */
export function makeAgent(overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    id: "explore",
    description: "只读探索子代理",
    purpose: "explore",
    model_profile_id: "inherit",
    execution_policy_id: "inherit",
    requested_skills: [],
    requested_mcp_servers: [],
    max_turns: null,
    source: "builtin",
    fingerprint: "explore-fingerprint",
    kind: "builtin",
    tools: ["ls", "read_file", "glob", "grep", "lsp"],
    ...overrides,
  }
}

/** 单个 MCP server。 */
export function makeMcp(overrides: Partial<McpServerSummary> = {}): McpServerSummary {
  return {
    name: "server-a",
    transport: "stdio",
    status: "connected",
    tool_names: ["tool_a"],
    ...overrides,
  }
}

/** 构造 approval interaction。 */
export function makeApproval(
  requestId: string,
  decisions: InteractiveInteraction extends { type: "approval"; decisions: infer D } ? D : never = ["approve_once", "reject"] as never,
  description = "需要批准",
): Extract<InteractiveInteraction, { type: "approval" }> {
  const fallbackDecisions = ["approve_once", "reject"] as unknown as typeof decisions
  return {
    type: "approval",
    requestId,
    description,
    requests: [{ tool: "write_file" }],
    presentation: null,
    decisions: (decisions ?? fallbackDecisions) as Extract<InteractiveInteraction, { type: "approval" }>["decisions"],
    deadlineAtMs: Date.now() + 60_000,
  }
}

/** 构造 question interaction。 */
export function makeQuestion(
  requestId: string,
  questions: readonly InteractiveQuestion[] = [],
): Extract<InteractiveInteraction, { type: "question" }> {
  const built: InteractiveQuestion[] = questions.length > 0
    ? [...questions]
    : [
        {
          id: "q1",
          question: "请选择颜色",
          header: "颜色",
          body: "选择一个主色",
          options: [
            { label: "红", value: "red", description: "热情" },
            { label: "蓝", value: "blue", description: "冷静" },
          ],
          multiSelect: false,
          allowOther: false,
        },
      ]
  return {
    type: "question",
    requestId,
    questions: built,
    deadlineAtMs: Date.now() + 60_000,
  }
}

/** 构造 confirmation 快照。 */
export function makeConfirmation(
  overrides: Partial<InteractiveConfirmation> = {},
): InteractiveConfirmation {
  return {
    confirmationId: "conf-1",
    title: "确认操作",
    message: "是否继续？",
    confirmLabel: "继续",
    cancelLabel: "取消",
    ...overrides,
  }
}

/** 简单 Command menu item：内置 help 命令。 */
export function makeCommandMenu(items: readonly CommandMenuItem[] = []): readonly CommandMenuItem[] {
  return items
}
