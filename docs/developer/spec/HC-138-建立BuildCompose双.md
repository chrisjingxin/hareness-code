# HC-138 Build / Compose 双工作模式设计

关联任务：[HC-138](../task/HC-138-建立BuildCompose双.md)  
调研依据：[Compose Mode 可行性调研](../research/Compose Mode 可行性调研.md)  
实施计划：[HC-138 Plan](../plan/HC-138-建立BuildCompose双.md)  
执行清单：[HC-138 Todo](../todo/HC-138-建立BuildCompose双.md)

## 通俗问题说明

Harness 当前只有一种工作方式：用户提交一条消息，主 Agent 在同一个 Run 中自行规划并执行。用户可以用 Skill 改变方法，也可以委派 Agent/Team，但没有一个由 Harness 明确掌控的完整研发流程。结果是弱模型可能过早写代码、忘记验证、自己 Review 自己，或者在失败后继续猜测。

Compose Mode 让用户只确认真正需要人决定的事项和整体方案，其余流程由代码推进：先理解需求，再形成计划，按小任务实现，用真实命令验证，最后交给独立上下文 Review。Build 保持现有直接协作体验。

## 已确认现状

- 一次用户请求的 canonical 执行身份仍是 Run；Run 的 owner、取消、Interaction、事件 sequence、Transcript 和唯一终态由 `RunCoordinator` 统一拥有。
- `AgentExecutionRegistry`、`AgentDelegator` 和 AgentEnginePool 已能为子角色提供独立 execution、模型/Policy 事实、取消传播和 fresh context。
- TUI/Web 只通过共享 `InteractiveController` 执行业务 intent，两个 renderer 不直接调用 Agent RPC。
- Approval Mode 是工具权限策略，不是产品工作模式；现有 `Shift+Tab` 循环不能被 Compose 占用。
- Skill catalog 在顶层 Run 边界冻结。普通 Skill 仍服务用户显式调用和模型 progressive disclosure；Compose 方法资产不能污染 Build 的 Skill index。
- Host 关闭会取消 active Run。当前文件写操作不具备可安全重放的 workflow 幂等语义，因此 V1 不承诺进程重启后续跑同一 Compose Run。

## 目标流程与关键 invariant

```text
空闲 Thread 选择 Build
  → submit
  → 现有 BuildRunAdapter

空闲 Thread 选择 Compose
  → submit
  → Understand
      ├─ 可发现事实：Agent 自己读取仓库
      └─ 产品决策：interaction.question
  → Plan
  → 用户确认整体方案
      ├─ revise → Plan
      ├─ cancel → Run cancelled
      └─ approve → Build
  → Build(task 1..N)
      ├─ behavior/bug/refactor → TDD 方法资产
      └─ failure → Debug 方法资产，限次重试
  → Verify
      ├─ fail → 生成 fix task → Build（限次）
      └─ pass → Review
  → Review(requirement + code fresh executions)
      ├─ finding → 生成 fix task → Build → Verify（限次）
      └─ pass → Run completed
```

关键 invariant：

1. Build 与 Compose 共用一个 Run lifecycle；不复制 owner、取消、Interaction、事件信封、Transcript 或终态。
2. Mode 在 Run 受理时冻结。运行中按 Tab 不切换当前 Run，也不改变已启动 execution。
3. Compose、AgentDefinition、Skill 和 Prompt 都不能放宽 EffectiveExecutionPolicy；所有文件、Shell、MCP、网络和 delegation 继续取既有策略交集。
4. 阶段推进只由 `ComposeStateMachine` 根据 typed artifact、真实 evidence 和固定 retry budget 决定；模型不能自行宣称跳到下一阶段。
5. 每个阶段只拿当前 ContextPack。阶段之间传 artifact pointer 与有界摘要，不传完整过程历史。
6. Verify 的通过条件是所有 required command 产生当前轮 exit code 0；缺命令、超时、输出不可解析或 adapter 失败均不是 pass。
7. Requirement Reviewer 与 Code Reviewer 是两个独立 Managed AgentExecution；作者 execution 不得兼任最终 Reviewer。
8. Plan approval 与 Tool approval 是不同语义。前者使用 workflow question（批准/修改/取消，修改必须携带 feedback），后者继续由 Approval Policy 在工具边界触发。验证命令的审批独立于 approval_mode 且不会因 yolo/auto 自动放行：这是有意收紧，绝不扩大任何权限。
9. Artifact 是 Compose 的唯一阶段事实；Transcript 只保存用户消息和最终可见 assistant 结果，LangGraph messages 仍只是 execution 投影缓存。
10. 任一 retry budget 耗尽、Review finding 无法修复或 verification 环境缺失都进入 `blocked`，返回原因与已有 evidence，不伪造 complete。

## 领域模型

```text
InteractionMode = build | compose

ComposeRun
├── run_ref
├── revision
├── stage: understand | plan | build | verify | review
├── status: running | waiting_user | blocked | completed | failed | cancelled
├── stage_attempts
├── understanding_artifact_id
├── plan_artifact_id
├── build_tasks[]
├── verification_evidence_id
└── review_report_id

ComposeArtifact
├── artifact_id / kind / version
├── run_id / source_execution_id
├── created_at_ms
├── bounded structured payload
└── content_digest
```

Artifact payload：

- `UnderstandingArtifact`：goal、constraints、acceptance、out_of_scope、open_decisions、change_kind。
- `PlanArtifact`：solution、ordered tasks、每项 acceptance、依赖、relevant pointers、verification commands。
- `TaskResultArtifact`：task ID、changed paths、focused test evidence、remaining issue。
- `VerificationEvidence`：command、工作目录身份、started/finished、exit code、bounded stdout/stderr digest 与摘要。
- `ReviewReport`：requirement/code 两轴 verdict、带 severity 的 finding、对应 acceptance 或 diff location。

所有 payload 使用严格 schema、字段白名单、条数/字节上限；源码、完整 Tool output、凭据和绝对用户目录不进入 artifact。Plan task 只是 ComposeRun 内部执行项，不映射为 `docs/developer/task/ZC-*`。

## Module、interface 与 seam

### Run execution adapter seam

`RunCoordinator` 保留小而稳定的外部 interface：`start`、`cancel`、owner disconnect、idle reservation。把现有 Agent stream 细节收敛为 `BuildRunAdapter`，新增 `ComposeRunAdapter`；两者满足同一个内部 interface：

```text
prepare(immutable Run input) → prepared execution
execute(prepared execution, lifecycle port) → RunCompletion
cancel() 由共享 cancellation token 传播
```

`lifecycle port` 只暴露发 typed signal、请求 Interaction、追加可见 Transcript 和读取取消状态，不让 adapter 自己分配 sequence 或写终态。RunCoordinator 仍是唯一终态 owner。

### Compose workflow module

`ComposeRunAdapter` 是 deep module；其外部 interface 不暴露五个 stage handler。implementation 内部包含：

- `ComposeStateMachine`：纯 transition 函数和 retry budget。
- `ContextPackBuilder`：按 stage/task 从 frozen Run snapshot、artifact 与相关源码 pointer 生成有界输入。
- `StageAgentPort`：由 Harness 内置 stage Agent adapter 实现，底层复用 `AgentDelegator` / AgentEnginePool。
- `VerificationPort`：由 canonical execution backend adapter 实现，继续经过 Policy、Approval、workspace boundary、sandbox 和 Host 并发锁。
- `ComposeArtifactStore`：由 ThreadPersistence SQLite adapter 实现；只保存结构化 artifact 和 Compose projection，不碰 Transcript/ContextArtifact 表。
- 六份私有方法资产：understand、plan、build、tdd、debug、code-review。它们是 Compose implementation 资源，不注册为用户/模型可见 Skill；Verify 阶段由代码直接驱动（fresh evidence 就是方法），不依赖方法资产。

只有出现第二个真实 Workflow 并证明要共享 invocation 行为时，才允许把私有方法资产抽成公共 `workflow` Skill seam。

### Stage Agent

V1 内置四类 Managed execution spec：understander/planner、builder、requirement-reviewer、code-reviewer。understand 与 plan 可以共用相同模型 Profile，但必须使用不同 execution namespace；两个 Reviewer 强制只读能力视图。Builder 按当前 task 获得最小 ContextPack 和既有有效 Policy，不继承之前 Agent 的 conversation。

Plugin specialist 自动选择、用户 `@agent` 指定 reviewer 和 reviewer Team 不进入 V1；现有 Plugin Agent/Team interface 保持不变。

## Protocol 与可观察状态

`run.start` 增加必填 `mode: "build" | "compose"`，所有 CLI/测试调用方一次迁移，不保留旧内部 fallback。`run.started` 回传实际 mode。

新增 `compose.state` event，payload 是有界完整 projection，而不是要求 UI 自行拼 stage patch：

```text
revision
stage / status
stages[{id,status,attempts}]
tasks[{id,title,status}]
evidence[{label,status}]
blocked_reason?
```

projection 不含 artifact 正文、Prompt、绝对路径或内部 Agent 配置。每次合法 transition 后 revision 单调递增并发一帧；Interactive Core 只接受当前 active Run、sequence 合法且 revision 更大的 projection。普通 `threads.open` 不恢复进行中的 Compose，因为 V1 Host 关闭仍取消 active Run；历史只通过最终 assistant 结果和正常 Transcript 展示。

错误仍通过唯一 `run.failed` / `run.cancelled` 终态收敛。`blocked` 是 `compose.state` 中可观察的 Workflow 状态：Runtime 先发布最后一份 blocked projection，再以 `run.failed` 和稳定错误码结束 Run，不新增 `run.blocked` terminal event。Compose-specific 稳定错误至少区分：artifact invalid、stage execution failed、verification failed、retry exhausted、review blocked、workflow state invalid；原始异常、命令完整输出和 provider payload 不越过 wire。

## UI 行为

- 空闲且无 Picker/Menu/Dialog 时，`Tab` 在 Build/Compose 间切换；有浮层时 Tab 保持现有“选择候选”语义。
- Work Mode 是当前 Thread 下一次 Run 的选择；同一 Thread 可在相邻 Run 之间切换并保留当前选择，Run 受理后冻结。V1 不持久化跨应用重启的 Mode 偏好。
- `Shift+Tab` 继续循环 Approval Mode；界面同时显示 Work Mode 与 Approval Mode，避免把 Compose 误解为自动授权。
- active Run、pending Interaction、context compact 或 cancellation 收敛期间禁止切 Mode，并给出稳定 disabled 原因。
- Compose 运行时两端只显示五阶段、当前 Build task、Verify command 状态和 Review verdict；不显示 ContextPack、artifact ID、Agent spec 或 transition 名。
- Plan approval 展示 solution、tasks、acceptance 和 verification 摘要，用户可选择“批准 / 修改 / 取消”；“修改”必须携带 feedback 并回到 Plan。
- Run 完成后 Timeline 保留一条结果摘要：改动文件数、verification evidence 汇总、Review verdict 和未解决风险。

## 阶段完成与失败规则

- Understand：结构化 artifact 校验通过且 `open_decisions` 为空。可发现事实不得转问用户；真正产品决策通过 question 回写后重建 artifact。
- Plan：task DAG 无环、每项 acceptance 非空、verification commands 有界且方案无 placeholder；用户批准后才进入 Build。
- Build：所有 task completed 且 focused evidence 已记录。行为/Bug/refactor task 强制先记录 RED evidence；文档、静态配置和纯样式可 direct。
- Verify：required commands 全部 fresh pass。默认 retry/fix round 为 2；失败会创建来源明确的 fix task，不能修改原 acceptance。
- Review：两轴 Reviewer 都 pass 且无 Critical/Required finding。默认 review-fix round 为 2；Optional/Nit 可进入最终报告但不阻止完成。
- 任一 stage agent 输出 schema invalid 只允许一次结构化重试；仍无效则 failed。Verify/Review 修复预算耗尽则 blocked，用户可在普通 Build 中接管后续工作。

## 测试方案

测试 seam 与生产调用方一致：

1. `ComposeStateMachine` 纯测试覆盖全部合法/非法 transition、revision、retry budget、cancel 与 blocked。
2. Compose adapter 使用 fake StageAgent/Verification/ArtifactStore，通过统一 RunCoordinator interface 验证 owner、取消、唯一终态、Interaction resume、malformed artifact 与 event ordering。
3. Python Host/Protocol 覆盖 build/compose mode 受理、幂等 fingerprint、Policy 不提权、只读 Reviewer、Verification adapter 和 Transcript/artifact 隔离。
4. TypeScript Core 覆盖 Mode intent、busy gate、compose.state sequence/revision、terminal cleanup 与 TUI/Web snapshot parity。
5. TUI/Web 覆盖 Tab/Shift+Tab 正交、菜单优先级、Plan approval、五阶段/任务/evidence 展示、窄屏和键盘可达性。
6. 端到端使用 fake models 和 fake execution backend 走通：happy path、Understand 提问、Plan revise、Build RED/GREEN、Verify fail→fix、Review finding→fix、retry exhausted、cancel。测试禁止真实模型凭据。

## 可观察验收

- 用户可在空闲输入区用 Tab 切换 Build/Compose，Shift+Tab 仍只改变审批模式。
- Build 的行为和当前版本一致；Compose 提交后无需反复输入“继续”，只在真实决策和整体计划处等待。
- Compose 只按五阶段前进，UI 与 Host 状态一致；错误、取消和超限只有一个终态。
- Verify 展示的每个通过项都有本轮真实 exit code evidence；没有 evidence 时不能显示 complete。
- Requirement/Code Review 来自独立 execution；Required finding 修复后必须重新 Verify 和 Review。
- Compose 不扩大文件、Shell、网络、MCP、sandbox 或 delegation 权限，也不把内部 artifact/Prompt/凭据暴露给 UI。
- Protocol 生成物、Python/TypeScript focused tests、项目级 build/typecheck/test/project:check 通过，用户/架构文档同步。

## 非范围

- Host/CLI 退出、崩溃或升级后的同一 Compose Run durable resume。
- Plugin 自定义 Mode、通用 Workflow SDK、公共 workflow Skill invocation。
- Plugin specialist 自动选择、Agent mailbox、Reviewer 互发消息、并行写 task。
- 自动 commit/push/PR、版本发布、远端 CI 等外部副作用；除非用户请求且既有 Approval/Tool 明确授权。
- 让 Compose 隐式启用 yolo、跳过用户计划确认或覆盖仓库 Task/Design 流程。
