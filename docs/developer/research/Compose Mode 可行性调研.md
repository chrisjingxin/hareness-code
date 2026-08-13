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

## 2026-08-12：MiMo Code Compose 源码复核

> 版本边界：下述“Compose primary Agent + 14 个 Compose Skill”的源码结论基于本地 revision `4ef01a4c9fa8674ba03e4efc6b8424c6e800a936`（2026-06-11）。为避免把旧实现误当成 MiMo 当前推荐方案，文末另补充了 2026-08-12 upstream `de9f47ab04141654cbd8875b7d8499366c25a992` 的三路径设计。

本节针对实际使用反馈重新核对 MiMo Code，并修正本文此前对 “Prompt-only Compose” 过于粗略的描述。调研对象是本地 MiMo Code revision `4ef01a4c9fa8674ba03e4efc6b8424c6e800a936`；证据只使用该 revision 的源码、内置 Skill、测试和 README。Harness 对照基线仍是 [HC-138 Task](../task/HC-138-建立BuildCompose双.md)、[HC-138 Spec](../spec/HC-138-建立BuildCompose双.md) 与已完成的 [HC-139](../task/archive/HC-139-组合模式过程可观察.md)。

### 先给结论

用户的判断方向基本正确，但准确描述不是“MiMo 定义固定阶段，每个阶段只强制一个 Skill”，而是：

```text
长期存续的 Compose primary Agent
  → 每轮从当前 Session 历史、Task ledger、Checkpoint 和工作区文档恢复目标
  → Orchestrator 根据当前问题选择最匹配的 Compose Skill
  → Skill 内的 checklist、terminal step 和下一 Skill 引用形成软流程
  → Spec / Plan / Report Markdown 与 Task ledger 承载可检查进度
  → Runtime 只守工具权限、Session 循环、持久化和恢复，不拥有 Compose 阶段状态机
```

MiMo 的体验优势主要来自“当前工作目标可以跨消息持续，下一步按已有进度路由”，不是来自更完整的阶段状态机。它不会把用户的“继续”解释为一个新的 Compose Run 并重新初始化 Understand；相反，源码中根本没有 Compose 专属的 `current_stage`、transition table 或阶段恢复指针。

### Compose 是 Agent 配置，不是 Workflow Runtime

- `compose` 与 `build`、`plan` 同为 `mode: "primary"` 的原生 Agent。它的 `options` 为空，只在默认权限上允许 `question` 和 `skill`；定义中没有 stage、artifact 或 transition 字段：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/agent/agent.ts:177-191`。
- Compose 系统提示把自己定义为 Skill orchestrator，并规定“匹配 Skill 必须调用”；同时明确允许需求完整、无设计歧义的 Bug 或 well-specified change 跳过 brainstorm，直接进入 debug、TDD 或实现：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/session/prompt/compose.txt:1-13`。
- Skill 选择发生在模型侧：收到消息后判断 Skill 是否适用，适用则调用 `skill` Tool，并按 Skill checklist 执行；Runtime 没有在这里计算阶段：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/session/prompt/compose.txt:54-67`。
- `skill` Tool 的实现只是按名字读取 Skill 内容、经过权限检查、把 `SKILL.md` 和少量相关文件返回给模型；它不保存阶段，也不触发 transition：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/tool/skill.ts:15-72`。

MiMo 将内置 Compose Skills 打包后解压到版本化 data 目录，再向 Prompt 注入一个只含 name、description、location 的 `<compose_skills>` 索引：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/extract.ts:32-49,63-84`。这些 Skill 标记为 hidden，不出现在普通 `available` 列表，但仍能按确切名称从 registry 读取：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/index.ts:250-272`。因此 MiMo 实现的是“Compose 专属的 progressive-disclosure 方法库”，不是“每个 Runtime stage 私有注入一份 method asset”。

### 所谓阶段实际由 Skill 文本链接

MiMo 有可辨认的研发流程，但流程边是自然语言契约：

1. `compose:brainstorm` 的 checklist 负责理解、澄清、方案、Spec 和批准，其 terminal state 明确要求调用 `compose:plan`：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/brainstorm/SKILL.md:25-73,123-160`。
2. `compose:plan` 写完并自检 Plan 后，根据记忆偏好或用户选择进入 `compose:subagent` 或 `compose:execute`：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/plan/SKILL.md:128-161`。
3. `compose:execute` 逐项执行 Plan、运行指定验证，全部完成后调用 `compose:report`，Report 再进入 Merge：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/execute/SKILL.md:17-38`、`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/report/SKILL.md:37-46,173-180`。
4. `compose:subagent` 则为每个任务创建 Task、派 fresh implementer、先做 Spec compliance Review、再做 Code quality Review；所有任务完成后做最终 Review 并进入 Merge：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/subagent/SKILL.md:43-128`。
5. `compose:verify` 是所有完成声明前都应使用的 evidence gate，但它本身仍是模型执行的 checklist，不是 Runtime 读取 exit code 后触发的状态迁移：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/skill/compose/.bundle/verify/SKILL.md:17-38,118-138`。

这也暴露了软流程的边界：`execute` 路径显式要求 `report → merge`，而 `subagent` 流程图在最终 Reviewer 后直接进入 `merge`。Runtime 不会发现或拒绝这种流程差异。MiMo 能灵活跳步，但不保证所有路径满足同一套合法迁移和完成判定。

### 产物会写入工作区，但不是每阶段一个 Markdown

| 产物 | MiMo 规则 | 是否强制 |
| --- | --- | --- |
| Spec | 默认 `docs/compose/specs/YYYY-MM-DD-<topic>-design.md` | 仅多步骤功能或重要架构；单个 Bug、小改动可以只留在 Conversation。证据：`brainstorm/SKILL.md:123-133`。 |
| Plan | 默认 `docs/compose/plans/YYYY-MM-DD-<feature-name>.md`，含精确文件、checkbox、命令和 expected output | 多步骤 Plan 路径强制。证据：`plan/SKILL.md:15-20,46-110`。 |
| 执行进度 | Task Tool 的 Task/子 Task 状态，以及 Plan checkbox | 不是单独的阶段 Markdown。证据：`execute/SKILL.md:19-32`、`subagent/SKILL.md:101-128`。 |
| Final Report | 默认 `docs/compose/reports/<feature-name>.md`，原地覆盖最终状态并回链 Spec/Plan | 标准复杂功能需要；trivial fix 可跳过。证据：`report/SKILL.md:15-35`。 |

所以“直接写入 workspace MD”确实是 MiMo 连续性的关键部分，但不是完整答案。MiMo 同时依赖 Conversation、SQLite Session、Task ledger、Checkpoint 和按任务保存的 progress；README 明确列出 `checkpoint.md`、`tasks/<id>/progress.md` 及 Task tree，并说明 resume 时自动注入：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/README.md:55-75`。

### 为什么输入“继续”不会回到开头

- 每次主循环先读取当前 Session 的未压缩消息切片，并从最后一条 user message 解析本轮 Agent，而不是构造新的 Compose workflow state：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/session/prompt.ts:2120-2154,2393-2400`。
- 只要当前上下文中存在任意 `agent === "compose"` 的 user message，就会把 Compose orchestrator prompt 和 Compose Skill 索引前置到那条消息；这个 reminder 在每轮模型调用前重新处理：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/session/prompt.ts:443-464,2512-2515`。
- Context rebuild 会注入 Task ledger 和 Session checkpoint，并明确告诉模型直接从最近状态继续、不要重述或重新询问目标：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/session/checkpoint.ts:1096-1122,1216-1232`。
- CLI `-c` 只是导航到最近 Session；`/resume` 和 `/continue` 只是打开 Session list 的 alias，不触发任何 Compose stage：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code/packages/opencode/src/cli/cmd/tui/app.tsx:377-397,428-440`。

MiMo 的 resume 单位因此是“有完整历史与账本的 Session/Goal”，不是“某个 Compose stage”。这能自然处理“继续”“按反馈改 Plan”“修复上次失败”等输入；代价是历史、文档和 Task 状态若互相矛盾，模型可能选错 Skill，因为没有 deterministic resolver 负责裁决。

### 与 HC-138 当前实现的关键差异

| 维度 | Harness HC-138 | MiMo Code |
| --- | --- | --- |
| 用户提交的语义 | 每次 Compose submit 是新 Run；adapter 对每个 Run 调用 `ComposeStateMachine.initial()`，初始固定 Understand：`packages/agent/harness_agent/host/run_execution.py:382-406`、`packages/agent/harness_agent/compose/state_machine.py:457-490` | 新消息追加到同一 Session，Orchestrator 结合历史选择下一 Skill。 |
| 流程形状 | 固定 `Understand → Plan → Build → Verify → Review`，只有 Verify/Review fix loop：`HC-138 Spec:23-62` | Brainstorm 可跳过；Plan 后可 inline/subagent；Debug/TDD/Review/Report/Merge 按情境组合。 |
| 推进 owner | `ComposeStateMachine` 只接受 typed event；模型无权切 stage | 模型遵循 Skill checklist 并调用下一 Skill；Runtime 不验证 transition。 |
| 连续性 | Artifact 在当前 Run 内传递；Spec 明确不支持跨进程 durable resume，且新 Run 不读取旧 Compose state：`HC-138 Spec:191-197` | Session 历史、Task、Checkpoint、Spec/Plan/Report 共同恢复目标，没有阶段指针。 |
| 完成可信度 | VerificationPort 读取 fresh exit code；双 Reviewer、retry budget、blocked 是代码 invariant | Verify/Review 纪律写在 Prompt/Skill；灵活但可能被模型漏用或误用。 |
| 工作区事实 | Compose artifact 保存在独立 SQLite store，不进入仓库研发文档 | 复杂功能的 Spec/Plan/Report 直接进入工作区，Task ledger 保存执行进度。 |

当前 Harness 的“继续又从 Understand 开始”不是偶发 Prompt 问题，而是实现语义的直接结果：`ComposeRunAdapter.execute()` 无条件为新的 `run_id` 创建 initial state，`initial()` 又无条件把 Understand 设为 running。HC-138 的状态机在单次长 Run 内非常可靠，但没有定义“同一用户 Goal 跨多个 Run 的持续身份”和“新消息应修订/恢复哪个已有产物”。

### 设计判断

MiMo 的方式在**交互连续性、按任务复杂度跳步、让 Spec/Plan 成为用户可编辑事实**三个方面明显优于当前 HC-138；当前 Harness 在**权限不提权、fresh verification、Reviewer 隔离、非法迁移与 retry 上限**方面更可靠。二者不是简单二选一：直接照搬 MiMo 会丢掉 Harness 已经建立的可信完成判定，继续维持 HC-138 的“每条消息开启固定五阶段”则会持续产生与用户目标不一致的体验。

不考虑历史包袱时，更合理的方向是：

```text
Thread 内持久化 Compose Goal / Work Item
  → Artifact resolver 判断已有 Spec、Plan、Task、Diff、Evidence 和待决策
  → Router 选择下一种能力（brainstorm / plan / execute / debug / verify / review）
  → 对应 Skill 规定这一能力内部怎么做
  → Runtime guard 只硬控不可妥协的安全与证据 invariant
  → 结果写回 canonical workspace 文档 + 小型机器状态
```

建议保留的硬约束只有：Run owner/取消/Interaction/权限、写入副作用边界、fresh verification evidence、Reviewer provenance、终态幂等、结构化产物校验和有界 retry。`Understand → Plan → Build → Verify → Review` 不应继续作为每个提交都必须从头走的全局 FSM，而应降级为 Router 的默认推荐路径；明确 Bug 可直接 `debug → TDD/fix → verify → review`，已有批准 Plan 可直接 resume execute，用户反馈应修订当前 Artifact 而不是创建新的 Understanding。

工作区 Markdown 适合承载人可读、可编辑、可 Review 的 Spec/Plan/Report；小型机器状态只保存稳定 work item ID、artifact references、pending decision、task/evidence status 和 revision，不能成为第二份需求正文。恢复时由 resolver 读取两者并 fail closed 处理矛盾，而不是仅信 conversation，也不是仅信一个 `current_stage` 枚举。

### 已确认风险

- MiMo 的 transition 是 Prompt contract，没有 Runtime 保证；弱模型、上下文截断或 Skill 漏调时仍会偏航。
- MiMo 的不同执行 Skill 对 Report 是否必经并不一致，证明 Skill graph 需要静态/运行期 contract 检查，不能直接等同于可靠状态机。
- 仅靠 workspace Markdown 无法安全判断命令是否 fresh pass、Reviewer 是否独立、写副作用是否 outcome-unknown；这些仍必须由 Runtime 记录 provenance/evidence。
- 仅靠数据库阶段枚举也无法表达“用户刚刚修改 Spec”“已有 Plan 可直接执行”“当前是旧 Bug 的新复现”等语义；需要 artifact-aware resolver。
- Harness 仓库已有 canonical `task → spec → plan → todo → implement → review` 文档规范。若重新设计 Compose，必须写入现有 `docs/developer/` 同编号链路，不能照搬 MiMo 的 `docs/compose/` 平行目录。

### 2026-08-12 upstream：MiMo 已从单一 Compose Agent 演进为三条路径

最新官方源码 `de9f47ab04141654cbd8875b7d8499366c25a992` 已不再把 legacy Compose Agent 作为唯一答案：

| 路径 | 实现 | 适用场景 | 关键边界 |
| --- | --- | --- | --- |
| Build + `/compose-next` | 一份 187 行 self-contained Skill contract，按 `Orient → Grill → Spec → Workspace → Implement → Verify → Review → Finalize → Finish` 执行；内部不再在 14 个 Skill 间 hand-off | 前沿模型；需要用户中途修订、判断和自然对话的功能开发 | 明确要求用户显式调用；机械改动可跳 Grill/Spec；只维护一份 `docs/compose/spec/<feature>.md`，不再平行生成 Plan/Report。证据：`packages/opencode/src/skill/builtin/.bundle/compose-next/SKILL.md:1-187`。 |
| legacy Compose Agent | `compose` primary Agent + orchestrator prompt + 14 个 `compose:*` Skill | 较弱模型需要逐步 curriculum 时 | 最新 README 已标为 legacy；Session 首条消息后 Compose 与 Build/Plan 互相隔离，以固定 Skill/Tool 集提升工具调用可靠性。证据：`packages/opencode/src/agent/agent.ts`、`packages/opencode/src/cli/cmd/tui/context/local.tsx:35-136`。 |
| deterministic Compose Workflow | 749 行 JavaScript Workflow，代码固定 Brainstorm、Design、Implement、Verify、Review、Report、Merge，结构化 schema、有界重试、依赖拓扑、worktree 并行 | 需求明确、任务可独立拆分、无人交互的 fire-and-forget | 非交互；Agent 负责写 Spec/Plan/Report，Workflow 只校验存在、抽取结构化任务和推进阶段。证据：`packages/opencode/src/workflow/builtin/compose.js:1-749`。 |

最新 MiMo 的实际产品判断因此是：**交互式强模型不需要一个重型专用 Mode，更适合 Build Agent 加一份紧凑 workflow contract；确定性状态机仍有价值，但应作为独立、非交互 Workflow，而不是把所有用户消息都塞进同一个五阶段 Run。** 官方 README 也明确说明：需要中途改方向或注入判断时用 `/compose-next`（或 legacy agent），需求清楚且可并行拆分时才使用 deterministic Workflow。[MiMo Code 官方仓库](https://github.com/XiaomiMiMo/MiMo-Code)

这比只对照 2026-06-11 本地源码更有启发：Harness 当前 HC-138 实际把 MiMo 的“确定性 Workflow”能力和“交互式 Compose Mode”产品入口合成了一条路径，所以既承担了 Workflow 的严格性，又承受了对话不连续的代价。

### 最终推荐：保留一个 Compose Work Item，不保留固定五阶段 Mode

不考虑历史包袱时，推荐目标不是“把现有状态机改松一点”，而是重画身份与事实源：

```text
Thread
└─ Compose Work Item（跨 Turn 的稳定目标身份）
   ├─ canonical Task / Spec / Plan / Todo Markdown
   ├─ Activity ledger（短期 Agent execution、Skill、输入/输出摘要）
   ├─ Verification evidence（绑定 workspace change-set digest）
   └─ Reviewer provenance（fresh、read-only、独立 execution）

每个用户 Turn
  → 附着当前 active Work Item
  → 从 Markdown + ledger 重建 readiness
  → Router 选择下一 Activity 和 required Skill
  → Guard Kernel 校验前置条件、权限、evidence 与 completion
  → 执行到 waiting_user / blocked / turn budget / completed
```

`run_id` 只标识一次执行，`work_item_id` 才标识同一个功能。用户输入“继续”会附着现有 Work Item，从第一个缺失或 stale 的 Gate 继续；显式 Bug 可直接 `debug/fix → verify → review`；用户修改上游需求时允许回到 Task/Spec，并自动使下游 Plan、Todo、Verification、Review stale。

建议的 readiness Gate 与本仓库 canonical 流程一致：

```text
task_confirmed
  → spec_valid
  → plan_valid
  → todo_executable
  → implementation_current
  → verification_fresh
  → review_fresh
  → complete
```

Stage 只作为 `current_activity` 的 UI 标签，不再是持久化真相。Runtime 只硬控不可让模型自行决定的 invariant：Policy/Approval、写副作用 intent/receipt、artifact lineage、fresh verification、Reviewer provenance、retry/turn budget、terminal CAS。Skill 负责“该 Activity 应该怎样做”，Markdown 负责“人类已经确认了什么”，模型负责在 Guard 允许的 action 集合内选择和执行。

外部 module interface 应保持很小：

```text
execute_turn(thread_id, run_id, active|new|explicit work_item, message)
  → turn result + work item projection

inspect(thread_id, work_item_id?)
  → documents + readiness gates + current activity + pending decision
```

动态 Workflow/Plugin 不是首版必要条件。只有出现第二个真实 workflow 后，才在 module 内部增加 `WorkflowProgram.next(snapshot, stimulus) → decision` 和 `ActivityAdapter.execute(...)` 两个 seam；外部 Host/CLI 永远不学习 stage handler。这样可以先获得 continuity 和 locality，不因“未来也许可扩展”提前引入通用 Workflow SDK。

### 对 HC-138 的处理建议

- **应删除/替换：** `run.start(mode=compose)` 触发整条五阶段 FSM 的产品语义、`ComposeStateMachine.initial()` 作为每次用户输入入口、以 `run_id` 为主键的流程身份、UI 固定五格阶段条、SQLite artifact 作为需求/计划的主要事实源。
- **应保留并下沉到 Guard/Activity：** `RunCoordinator` 的 owner/cancel/Interaction/sequence/唯一 Run terminal；共享 execution stream；Policy/Approval/sandbox/workspace；VerificationPort 的真实 exit-code evidence；fresh Reviewer 和只读能力；activity timeline 与有界审计。
- **应改为 canonical 文档：** 需求、规格、计划与清单直接更新仓库现有同名 `docs/developer/task|spec|plan|todo/HC-XXX-….md`；SQLite 只保存 `work_item_id`、document digest/reference、pending decision、activity/effect receipt、evidence/reviewer provenance、revision 和 completion fingerprint。
- **应新增显式用户操作：** 当前 Work Item 标识；`继续` 默认附着；新目标必须显式 `new compose work item`（可做命令或 UI action）；Thread 同时只允许一个 active Work Item，避免模型猜测“这是新需求还是补充”。
- **完成必须 fail closed：** Task/Spec/Plan/Todo revision 与 workspace change-set digest 必须和 fresh Verification、Review 一致；任一变化立即失效旧证据；workflow 只能 propose complete，Guard Kernel 才能原子提交完成。

这不是在现有 HC-138 上增加 resume 字段即可完成的改造。若只把上一 `run_id` 的 stage 读回来，会保留错误的单向状态语义：用户修改需求仍难以合法回退，Markdown 与 SQLite 仍会争夺事实源，Stage 仍会被误当成目标身份。正确的重构单位是 `ComposeRun → ComposeWorkItem`，状态模型从 `current_stage` 改为 `artifact readiness + evidence freshness`。
