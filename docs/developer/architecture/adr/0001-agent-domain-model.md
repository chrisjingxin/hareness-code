# ADR 0001：Agent 领域对象、生命周期与兼容映射

日期：2026-07-27  
状态：已接受

当前关联任务：[ZC-092](../../tasks/ZC-092.md)

## 背景

Harness 已有命名模型 Profile、角色路由、可共享 `AgentEngineProfile`、Thread 持久化和
DeepAgents 默认 `general-purpose` 子 Agent，但这些能力来自不同阶段的实现：当前 TOML
Profile 同时持有连接与模型参数；v5 的 `ThreadModelBindings` 曾将首次模型选择冻结在 Thread；
AgentEnginePool 则正确要求 AgentEngine 身份不含 Thread/Run 状态。若直接在这些实现上叠加
Thread 内 `/model`、自定义 Agent 或动态 delegation，会产生重复的模型、权限和历史来源。

本决策固定长期领域边界，并给出现有 TOML、SQLite 和运行时对象的兼容映射。当前实现已据此
完成 Thread 根模型选择的最小迁移；后续实现不得偏离本文定义的对象归属。

## 决策

### 核心对象

Agent 系统只有六个核心对象。前四个是静态定义，后两个分别表达 Thread 的可变意图和
Run 的不可变事实：

```text
ProviderDefinition ──1:N──> ModelProfile
                                  │
ExecutionPolicyDefinition ──N:1──┤ Plugin 提供的可派发 Agent
                                  ▼
                           AgentDefinition
                                  │
ThreadExecutionSelection ────────┤ 运行前解析
                                  ▼
                         RunExecutionBinding
```

#### ProviderDefinition

`ProviderDefinition` 描述“连接到哪里”，不描述某个 Agent 的任务或模型选择。

```ts
interface ProviderDefinition {
  id: string
  adapter: "openai-compatible" | "anthropic" | "google" | "custom"
  endpoint?: string
  credentialRef?: string
  staticHeaders?: Record<string, string>
  headerEnvRefs?: Record<string, string>
  transport?: { timeoutSeconds?: number; maxRetries?: number }
}
```

- `endpoint`、凭据引用和 Header 引用只能存在于可信静态配置及其内存解析结果中。
- Run 记录、协议 DTO、日志、AgentEngineProfile record 和 TUI 不得保存或展示 endpoint、
  凭据值、认证 Header 值或环境变量名。

#### ModelProfile

`ModelProfile` 描述“调用哪个模型以及如何调用模型”，通过 `providerId` 引用连接定义。

```ts
interface ModelProfile {
  id: string
  providerId: string
  model: string
  reasoning?: Record<string, unknown>
  parameters?: Record<string, unknown>
  capabilities: string[]
  contextWindowTokens: number
  display: { providerLabel: string }
}
```

模型参数、能力和窗口属于模型，而不是 Agent 或 Thread。每个 Agent 的默认模型、Thread
覆盖和一次 spawn 覆盖都只能引用 Profile ID，不能内联 endpoint、模型名或凭据。

#### ExecutionPolicyDefinition

`ExecutionPolicyDefinition` 描述“被允许怎样执行”，是唯一的安全与运行环境边界。

```ts
interface ExecutionPolicyDefinition {
  id: string
  tools?: { allow?: string[]; deny?: string[] }
  filesystem?: { read?: string[]; write?: string[] }
  shell?: { enabled: boolean; allowedCommands?: string[]; deniedCommands?: string[] }
  network?: { enabled: boolean; allowedHosts?: string[] }
  isolation?: { mode: "local" | "remote" | "worktree" | "container" }
  approval?: { mode: string }
  delegation?: {
    enabled: boolean
    allowedAgents?: string[]
    maxDepth?: number
    maxParallelism?: number
  }
}
```

有效策略始终取交集：

```text
managed / 全局硬边界
        ∩ 父 Agent 的 delegation envelope
        ∩ 目标 Agent 的 ExecutionPolicy
        = 子 Agent 的有效策略
```

因此 AgentDefinition、Thread、Run、Workflow 和 Prompt 都不得独立放宽工具、文件、
Shell、网络、隔离、审批或 delegation 权限。

#### AgentDefinition

`AgentDefinition` 是一个由已验证 Plugin 提供、可被内置主 Agent 动态派发的完整静态声明，负责“谁来做、做什么、怎么做”。主 Agent `main` 是 Python 内置实现，不是 AgentDefinition，不读取、也不允许被 JSON/Plugin 配置覆盖。

```ts
interface AgentDefinition {
  id: string
  description?: string
  purpose: string
  instructions: string
  inputContract?: string
  outputContract?: string
  successCriteria?: string[]
  model: { strategy: "inherit" } | { strategy: "profile"; profile: string }
  policy: string
  skills?: string[]
  mcpServers?: string[]
  limits?: { maxTurns?: number }
}
```

任务职责、工作方法和表达要求都是同一 Agent 指令的章节；它们不拆分为 Role 或 Persona。
输入/输出 contract 与 instruction fragment 是被 Agent 引用的资产，不是可独立选择的核心
对象。AgentDefinition 不内联 Provider、模型参数、权限明细、父子关系或 Thread 状态。生产路径
只加载已安装、已校验并已启用的 Plugin catalog，仍不扫描普通用户或项目 Agent 目录。

#### ThreadExecutionSelection

`ThreadExecutionSelection` 是当前 Thread 对未来 Run 的可变选择，不修改任何静态定义。

```ts
interface ThreadExecutionSelection {
  rootModelProfileId?: string
  agentModelOverrides?: Record<string, string> // 预留，MVP 不开放
}
```

- `/model` 的 MVP 只写 `rootModelProfileId`，仅影响根 Agent 的下一次 Run。
- Picker 确认可先更新本地 UI；在 `run.start` 被服务端成功受理后，选择作为请求快照持久化。
- `/model` 确认还会独立调用受控 Config Writer 更新用户级
  `models.default_profile`，供未来新 Thread 使用；该写入失败不得回滚 ThreadSelection，
  也不能改写 AgentDefinition、历史 Run 或 legacy 角色绑定。
- 当前 Run、已启动的子 Agent 和历史 Run 均不被热切换。
- 未显式选择的新 Thread 从当前 TOML 默认获得初始选择；配置默认不是 Thread
  的历史事实，也不会覆盖已恢复 Thread 的选择。

#### RunExecutionBinding

`RunExecutionBinding` 记录一个 Run 已经发生的执行事实，供恢复、审计和 TUI 历史使用。

```ts
interface RunExecutionBinding {
  projectFingerprint: string
  threadId: string
  runId: string
  requestedSelection: ThreadExecutionSelection
  rootExecutionId: string
  executions: AgentExecutionBinding[]
  createdAtMs: number
  finalizedAtMs?: number
}

interface AgentExecutionBinding {
  executionId: string
  parentExecutionId?: string
  agentId: string
  model: {
    profileId: string
    profileFingerprint: string
    display: { providerLabel: string; model: string }
    source: "spawn-override" | "thread-agent" | "thread-root" | "plugin-default" | "config-default" | "legacy"
  }
  executionPolicy: { id: string; fingerprint: string }
  depth: number
  status: "pending" | "running" | "completed" | "failed" | "cancelled"
  startedAtMs: number
  finishedAtMs?: number
}
```

`AgentExecutionBinding` 是 `RunExecutionBinding.executions` 的嵌套值对象，不是第七个
可配置对象。Run 在执行期间只能追加 execution 或更新其终态；一个 execution 启动后，
其 Agent、模型、Policy 与父子归属不可更改。Run 终态后整个 Binding sealed，不得作为
下一次 Run 的配置源。

### 解析与优先级

静态引用必须在 catalog 建立时验证。运行时只解析合法引用，并记录最终来源。

```text
根 Agent 模型
run.start 中的 ThreadExecutionSelection.rootModelProfileId
    > execution_binding 中的最近 Run、legacy 与 TOML 默认回退

动态子 Agent 模型
合法 spawn model override
    > ThreadExecutionSelection.agentModelOverrides（未来能力）
    > AgentDefinition.model 的 profile/inherit 策略
```

根 Agent 的 Thread override 不应用于子 Agent；否则一次 `/model` 会意外改变 Explorer、
Reviewer 或 Tester 的成本与能力。Policy 不存在“低优先级覆盖”：所有 Policy 始终取交集。

无效 ID、模型不可用、能力不足、Policy 提权、循环引用、未可信项目来源和无法恢复的 legacy
记录都必须 fail closed。v5 可读的 Thread 模型快照可以以 `legacy` 来源解析；只有旧 AgentEngine
指纹而无法恢复 Profile ID 时，UI 必须标记“历史模型未知”，不得将当前默认模型伪装为历史事实。

### 生命周期与 AgentEngine 边界

| 对象 | 生命周期 | 可变性 | AgentEngineProfile 处理 |
| --- | --- | --- | --- |
| Provider / Model | TOML 配置 snapshot | snapshot 内不可变 | 当前 root 模型的有效内容以脱敏指纹参与 |
| Plugin Policy / Plugin Agent | 已验证 Plugin catalog snapshot | snapshot 内不可变 | 仅在该 Plugin Agent 被派发时以脱敏指纹参与 |
| ThreadExecutionSelection | Thread 交互期间 | 仅影响未来 Run | 不直接参与；解析出的实际模型参与 |
| RunExecutionBinding | 从 run.start 受理到历史保留 | execution 启动后模型/策略不可变；终态 sealed | 不参与 |
| ResolvedAgentSpec | 单次角色解析到 AgentEngine 构建/调用 | 不可变快照 | 由其静态指纹构成 Profile，不持久化 |

`ResolvedAgentSpec` 是将一个内置 Agent 或未来受信 `AgentDefinition`、Model、EffectiveExecutionPolicy、
`EffectiveCapabilityView`、Prompt 和执行后端描述解析后的内部 DTO；它不是配置对象、数据库表或 JSON-RPC DTO。
能力视图由当前实际 Tool、Skill、MCP 快照与 Policy 求交产生，并同时控制模型 schema、Skill/MCP
子集和工具执行守卫；因此父窄子宽、Prompt 提权和伪造 tool call 都不能扩大能力。
当前生产实现解析内置 `main` 和受信 Plugin Agent，但不把 Thread、Run、Team 组合或任务正文放入
spec。AgentEngineProfile 只能由该 spec 的静态视图生成，AgentEngine builder 再按 Profile key 取回
同一个 spec，不能重新解释配置。有效 Agent/Model/Policy/Prompt/工具/Skill/MCP/Sandbox/middleware
指纹可以参与；`thread_id`、消息、run ID、审批状态、取消令牌、执行树和凭据绝不参与。

### 不作为当前核心对象的概念

- `Role`、`Persona`：分别已收敛为 AgentDefinition 的职责与指令内容。未来若需要独立
  生命周期、可切换 UI 或跨 Agent 的强复用，必须以新 ADR 从 AgentDefinition 中抽取。
- `InstructionFragment`、JSON Schema：文件资产，由 AgentDefinition 引用，不独立参与执行
  选择。
- `AgentWorkflow`：ZC-138 起以 Compose 工作模式落地首个代码驱动 workflow：它是 Run 内部
  的执行策略（`mode: compose`），不改变 Thread、Model 或 Run 的前置语义，也不注册为公共
  Workflow SDK 或第三种 Skill invocation；阶段 Agent 复用 AgentDelegator 与既有 Policy。
- `AgentTeam`：已作为独立 TeamCoordinator 的固定 DAG 模式存在，只引用 AgentDefinition，
  不改变 Thread、Model、Run 或 AgentEngine 的基础语义。
- `Mailbox`：没有真实需求，当前明确不实现。

## 现有实现的兼容映射

| 当前实现 | canonical 映射 | 迁移要求 |
| --- | --- | --- |
| `Za38Config`、`ModelCatalog`、`ModelSettings`、`ModelProfile` | TOML 输入 adapter；每个当前 Profile 暂可投影为一个 ProviderDefinition + 一个 ModelProfile | 不改 TOML v1、endpoint、`api_key`、`headers_env` 或来源优先级 |
| `[models.roles]`、`MODEL_ROLES` | 当前 AgentEngine 编译兼容层；`executor` 是当前 Single Agent root 的物理模型 | 不成为新的 RoleDefinition 领域对象；`/model` 只改变 root selection |
| `execution_binding.py` | Thread 根模型选择与 RunExecutionBinding 构造 | 一个纯函数集中请求、最近 Run、v5 legacy 和配置默认优先级；AgentEngine、持久化与 Protocol 消费同一解析结果 |
| `harness_thread_model_bindings` | v5 legacy 选择来源 | 新 Run 已写入 RunExecutionBinding；不可把当前配置回填为历史 |
| `harness_thread_runtime_profiles` | v4/v5 legacy Thread→AgentEngine 绑定 | 不再用于阻止模型切换；`harness_runtime_profiles` 仍可保存去重后的 AgentEngineProfile record |
| `ActiveRun`、`RunContext` | 单次调用控制状态 | 继续保存取消、审批、PromptEpoch 路由等易失状态；不取代持久 RunExecutionBinding |
| TUI `threadModelSelection` / `actualModelProfile` | ThreadExecutionSelection 的当前临时投影 | 已区分未来选择与本 Run 实际绑定，并以 `run.started` 校准 |
| `create_harness_agent()` | Python 内置主 Agent | 继续作为固定 root；不得改为读取 AgentDefinition |
| DeepAgents `task` / `general-purpose` | `AgentDelegator` 的兼容工具与受控 Inline target | 模型只提交 target/task；模式、Policy、execution 身份和资源由 Host 决定 |
| `AgentCatalog` / Plugin `agents/*.md` | 受信 AgentDefinition 与 ExecutionPolicy snapshot | 只通过 Managed target 进入 Pool；不得覆盖 `main` |
| `TeamCoordinator` | 固定 TeamDefinition 的 Run-scoped DAG owner | 只调用 AgentDelegator；不持有图、消息、Prompt 或 checkpoint |

当前 TOML Profile 之所以可以同时投影 Provider 和 Model，是因为 v1 仅支持 OpenAI-compatible
连接，且每个 Profile 自带 `base_url`、凭据和模型名。该投影可能临时为每个 Profile 生成一个
Provider ID；它不是要求立即拆分文件的信号。未来若引入独立 Provider 配置，必须保持同一
canonical model 和凭据脱敏边界。

## 后果

- ThreadExecutionSelection、每 Run 模型解析、RunExecutionBinding 和 AgentEngineProfile 选择必须由同一领域 module 协调，不得在 Server、TUI 和存储 adapter 中各自推断。
- 内置主 Agent 继续由 Python 固定构建，不放入 Agent catalog。
- Plugin Agent catalog、Agent 定向模型和固定 Team DAG 已接入；Compose 工作模式（ZC-138）
  已作为首个代码驱动 Workflow 落地；Mailbox、Team 产品协议/UI 和第二 Provider 仍是后续
  产品方向。
- 在第二 Provider adapter 出现前不建立 Canonical Message port；出现真实的第二 adapter 时，必须先阻止 Provider 私有历史跨 adapter 泄漏。
- `/model` 更新未来新 Thread 的配置默认时，不能修改既有 ThreadExecutionSelection 或历史 RunBinding。

## 备选方案

### 将 Role 与 Persona 独立建模

拒绝。两者当前都只贡献 Agent 指令文本，没有独立生命周期、权限或运行时行为；提前拆分会
造成重复引用和不必要的优先级规则。

### 将模型选择永久写入 ThreadModelBinding

拒绝。它会使当前 Thread 无法切换模型，也会把“下一次想使用什么”与“某次实际使用什么”
混为一谈。

### 先实现固定 Planner → Executor → Reviewer 工作流

拒绝作为当前主路径。Harness 选择由根 Agent 按任务动态派发 Subagent；固定 Workflow 可在
未来作为独立可选模式实现，不能反向定义基础对象。

### 将 Provider、模型、权限、Prompt 全部内联到 AgentProfile

拒绝。连接、模型、Policy 在多个 Agent 间有独立复用、敏感数据边界和缓存身份；内联会使
配置覆盖、权限审计和 AgentEngineProfile 指纹重复且难以验证。
