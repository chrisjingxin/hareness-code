# Compose Mode 可行性调研

日期：2026-08-11  
结论：有条件可行；应保留“代码控制流程、Skill 提供方法、Agent 提供独立视角、Artifact 传递状态”的方向，但必须重画 Run 生命周期、持久化事实和 Skill 边界后再实施。

## 要解决的问题

用户希望 Harness Code 增加与 Build 平级的 Compose Mode：Build 继续让用户直接与主 Agent 协作，Compose 则在一次提交后自动完成需求理解、计划、实现、验证和独立 Review，只有真正的产品决策与整体方案确认需要用户介入。

本调研验证三个问题：

1. 参考方案的工作流方法是否值得采用。
2. Harness 当前架构是否有足够的 Run、Agent、Skill、Context 与 UI 基础。
3. 怎样落地才不会形成第二套 Agent Runtime 或破坏已有生命周期。

## 证据范围

### 本地 Skill 源码

| 来源 | 本地 revision | 重点读取 | 可吸收结论 | 不直接吸收 |
| --- | --- | --- | --- | --- |
| Matt Pocock `skills` | `84fdeffd12f2ee307994d1eb6feb48173b6e0502` | `grilling`、`to-tickets`、`tdd`、`diagnosing-bugs`、`code-review` | Facts 由 Agent 查、Decisions 交给用户；测试 public seam；fresh-context 双轴 Review；tracer bullet | relentless interview；每个 slice 发布成独立 ticket；将 refactor 固定塞入另一阶段 |
| Addy Osmani `agent-skills` | `7676817c12a1317454ae3898a0c5c1eacf5dd3d5` | `interview-me`、`planning-and-task-breakdown`、`incremental-implementation`、`test-driven-development`、`debugging-and-error-recovery`、`code-review-and-quality`、`context-engineering` | 简单需求跳过访谈；repo-aware test command；小步验证；结构化 Debug；选择性 ContextPack | 95% confidence 仪式；按 3～5 个文件机械拆 Task；为计划建立仓库外的平行事实来源 |
| obra `superpowers` | `44c9b2d6e889982ac18c27d05a19fefe335194e1` | `brainstorming`、`writing-plans`、`test-driven-development`、`systematic-debugging`、`verification-before-completion`、`requesting-code-review` | RED 必须被观察；evidence before claims；Reviewer 只拿精确上下文；失败先查根因 | 所有变更强制完整 brainstorming；2～5 分钟一步；每个 Task 都 commit/review 的硬门禁 |

三个仓库均为 MIT License。若后续直接复制实质性文本，必须保留版权和许可；推荐只吸收方法，重新编写短小的 Harness 内置工作流指令。

### Harness 当前代码事实

- `RunCoordinator` 已集中拥有 Run 受理、同 Thread 互斥、owner、取消、Interaction、事件 sequence、Transcript、唯一终态和资源释放；Compose 不应复制这些能力。
- `AgentDelegator`、Managed/Inline `AgentExecution` 和 `TeamCoordinator` 已存在。独立 Reviewer 可以复用 Managed execution；Compose 整体不是固定 Team DAG，不能直接套 `TeamCoordinator`。
- `ThreadPersistence` 已区分 append-only Transcript 与 LangGraph 模型投影，且 Run 开始时冻结 `RunContextSnapshot` 和 Skill snapshot。Compose artifact 不能伪装成聊天消息，也不能复用只服务上下文压缩的 `ContextArtifact`。
- 当前 Skill 主路径支持用户显式 requested Skill 和模型从 Skill index 按需读取；Plugin adapter 另有 `model_invocable`。仓库没有 canonical `workflow` invocation。为 Compose 扩大公共 Skill catalog 会污染 Build 的长期 index，也会制造第三套元数据解释。
- TUI/Web 共用 `InteractiveController` 和 snapshot，适合只增加一次 Mode 选择与一份 Compose 投影。`Tab` 当前只在 Picker/Command menu 中选择项目；空闲 composer 没有 Mode 切换动作，`Shift+Tab` 已专用于审批模式，两者可以保持正交。
- `run.start` 目前只表达普通 Build Run，没有 mode、stage、artifact 或 review 事件。`RunCoordinator._execute()` 同时包含通用生命周期和 Build Agent stream 细节，Compose 出现后应抽出真实的执行 adapter seam。
- 当前没有“由 Runtime 直接运行验证命令并捕获 evidence”的安全入口。Compose 不能绕过 Tool Policy、Approval、Workspace boundary、Host 读写锁或 sandbox 直接调用 `subprocess`。

### 外部一手资料

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview) 将 LangGraph 定位为长时、有状态 Agent/Workflow 的低层 orchestration runtime，核心能力包括 persistence、streaming 与 HITL。
- [LangGraph custom workflow](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow) 明确适合混合确定性分支、循环和 Agent node；这支持“代码拥有流程、Agent 只完成阶段工作”的方向。
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 要求可恢复 HITL 使用 checkpointer、稳定 thread identity，并保证 interrupt 前副作用幂等。
- [LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api) 强调 durable workflow 的确定性、JSON 可序列化结果与副作用幂等；这意味着文件修改不能仅靠重放 workflow node 恢复。
- [Deep Agents subagents](https://docs.langchain.com/oss/python/deepagents/subagents) 把 subagent 的主要价值定义为 context quarantine 与专门指令，并明确简单任务不应付出 subagent 成本。
- [Deep Agents skills](https://docs.langchain.com/oss/python/deepagents/skills) 使用 progressive disclosure；Skill 是方法与知识，不是可靠的流程状态机。
- [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) 说明独立 teammate 适合研究、Review 和真正可并行工作，但有额外 token/协调成本，并公开标记 session resume、任务协调和 shutdown 的限制。
- 本地 Codex `ModeKind` / `CollaborationMode` 实现把 Plan Mode 作为每 Turn 的开发者指令与工具可用性策略，而不是五阶段持久化 Workflow。它证明 UI Mode 与 Approval Mode 可以正交，但不能作为 Compose workflow runtime 的替代。

## 对原方案的判断

### 可以保留

```text
Mode 选择用户与 Harness 的协作方式
  → Runtime 决定阶段与重试
  → Skill/方法资产约束阶段做法
  → AgentExecution 提供独立上下文和视角
  → Artifact 传递结果
  → Evidence 决定能否完成
```

- `Understand → Plan → Build → Verify → Review` 五阶段足够清晰，不再增加顶层 Debug/TDD 阶段。
- Verify 必须由 Runtime 执行真实命令并读取 exit code，不能依赖模型自报。
- Requirement Review 与 Code Review 使用两个 fresh Managed execution，结果分别保留，不能由同一作者上下文自审替代。
- TDD 只用于行为变更、Bug 和可观察逻辑；Debug 只在失败后进入。是否加载由结构化 task kind 与失败事实决定，不由模型自由选择下一阶段。
- Review/Verify 修复循环必须有固定上限，超过后进入 blocked 并返回完整 evidence。

### 必须修改

1. **Compose 仍是一种 Run 执行方式，不是第二套顶层 Runtime。** `run.start` 携带 `build|compose`，`RunCoordinator` 继续拥有通用生命周期；内部以 Build/Compose 两个 execution adapter 形成真实 seam。
2. **Compose artifact 单独建模。** Understanding、Plan、TaskResult、VerificationEvidence、ReviewReport 使用有版本、大小上限和来源 execution 的结构化值；它们不进入普通 Skill index，不冒充 Transcript message，也不复用 compression artifact。
3. **不新增通用 `workflow` Skill invocation。** V1 的七份方法资产属于 Compose implementation 私有资源，由 Compose adapter 按 stage 注入。等出现第二个真实 Workflow 后，再评估是否把它抽成公共 Skill invocation seam。
4. **Plan 中的 task 不是仓库 Task。** 它只是一个 ComposeRun 内的执行项。仓库仍保持“一项完整功能 = 一个 Task = 一个同 ID Design”；不能把 Matt/Addy 的 ticket 规则映射成一批 `ZC-*`。
5. **Verification 必须走安全 port。** Runtime 只能通过复用现有 execution backend、Policy、Approval 和并发锁的 adapter 执行计划中的有界命令。
6. **Compose Mode 与审批模式正交。** `Tab` 切 Build/Compose；`Shift+Tab` 继续切 plan/default/auto-edit/auto/yolo。Compose 不隐式启用 yolo，也不放宽任何 Agent/Workflow 权限。
7. **首版不承诺 Host 重启后续跑。** 当前 Run owner 断开会取消 Run，Agent 写副作用也不满足安全重放条件。V1 持久化已完成 artifact 与终态用于审计，但进程退出后仍按现有取消语义收敛；durable resume 需要单独确认 suspended 状态、event sequence 延续和写任务 outcome-unknown 恢复规则，不能夹带实现。

## 推荐 module 与 seam

```text
CLI Mode selection
  → run.start(mode, message, thread/run/model/approval/skill snapshot)
  → RunCoordinator
       ├─ BuildRunAdapter   → 现有主 Agent stream
       └─ ComposeRunAdapter
            → ComposeStateMachine
            → StageAgentPort ──adapter──> AgentDelegator / AgentEnginePool
            → VerificationPort ─adapter──> canonical execution backend + Policy
            → ComposeArtifactStore ─adapter──> ThreadPersistence SQLite
  → canonical event envelope
  → Interactive Core Compose projection
  → TUI / Web native renderer
```

`RunCoordinator` 的外部 interface 仍是 start/cancel/owner/idle；调用方不学习五阶段细节。Compose adapter 的 interface 只接收 immutable Run input、发出 typed workflow signal，并返回一个 Run completion。删除 Compose adapter 后，阶段、artifact、verification 和 reviewer 复杂度会重新散落到 Host/CLI，因此这个 module 有实际 depth。

## 可行性与主要风险

| 结论 | 判断 |
| --- | --- |
| 技术可行性 | 高。Run、AgentExecution、Interaction、Skill snapshot、ThreadPersistence 和共享 Interactive Core 已提供主要基础。 |
| 改造规模 | 大。至少纵向修改 Protocol、Python Host/runtime/persistence、Interactive Core、TUI/Web、测试与文档。 |
| 最大架构风险 | 把 Compose 分支直接塞进 2000+ 行 `RunCoordinator`，或绕过它另建生命周期，都会产生重复 owner/取消/终态。 |
| 最大安全风险 | Verification 或 Stage Agent 直接执行命令，绕过现有 Policy/Approval/sandbox；Compose 绝不能成为权限升级。 |
| 最大一致性风险 | 把 artifact 同时写入 Transcript、LangGraph state 和独立表，形成多个事实源。 |
| 最大产品风险 | 对简单任务也强制五次模型调用和两次 Review，成本与延迟不可接受；Understand 必须能生成 `simple=true` 的短 artifact，但仍由 Runtime 跳过不需要的访谈，不跳过最终 Verify。 |

## 最终建议

按 [HC-138 Design](../spec/HC-138-建立BuildCompose双.md) 作为一个完整功能实施，不拆成 Protocol/Agent/CLI 子 Task。Task 内按五个纵向步骤推进，每一步同时覆盖相关实现、focused tests 和可观察结果。V1 先交付单进程生命周期内可靠的 Compose；durable resume、Plugin specialist 自动选择、Plugin Mode、并行写任务和通用 Workflow SDK 明确留在非范围。
