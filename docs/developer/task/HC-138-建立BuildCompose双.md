---
id: HC-138
title: 建立 Build / Compose 双工作模式与结构化研发流程
feature_area: Agent 工作模式与结构化研发流程
parent_task: -
decomposed_by: Codex
priority: P1
status: 已过时
owner: Codex
branch: codex/zc-138-build-compose
reviewed_at: 2026-08-12
review_due: -
scope: 在保留现有 Build 行为的同时增加 Compose Mode；由共享 Run 生命周期内的 Compose execution adapter 依次完成 Understand、Plan、Build、Verify、Review，以结构化 artifact、真实 verification evidence、独立 Reviewer 和有界修复循环驱动阶段，并在 TUI/Web 提供一致的 Mode 切换、计划确认、进度和终态展示。
acceptance: 空闲时 Tab 可切 Build/Compose、Shift+Tab 仍只切审批模式；Build 无行为回归；Compose 只在真实决策和整体 Plan 处等待用户，其余阶段自动推进；所有阶段由代码状态机和严格 artifact 驱动，Verify 无 fresh exit-code evidence 不得通过，Requirement/Code Review 来自独立 execution；失败/取消/超限唯一收敛且不提权；Protocol、Python、Interactive Core、TUI/Web、fake-model E2E、build/typecheck/test/project:check 与用户/架构文档闭环。
user_docs: docs/user/交互使用.md、docs/user/安全与沙箱.md
developer_docs: docs/developer/spec/HC-138-建立BuildCompose双.md、docs/developer/research/Compose Mode 可行性调研.md、docs/developer/plan/HC-138-建立BuildCompose双.md、docs/developer/todo/HC-138-建立BuildCompose双.md、docs/developer/architecture/架构总览.md、docs/developer/architecture/adr/0001-agent-domain-model.md
test_evidence: 见正文「实施证据」小节
references: HC-140（替代任务）、docs/developer/spec/HC-138-建立BuildCompose双.md、docs/developer/research/Compose Mode 可行性调研.md、docs/developer/plan/HC-138-建立BuildCompose双.md、docs/developer/todo/HC-138-建立BuildCompose双.md、docs/developer/architecture/adr/0001-agent-domain-model.md
completed_at: 待用户确认后由 task:complete 填写
---

> **已过时：** 本任务记录固定 `Understand → Plan → Build → Verify → Review` ComposeRun 的首次实现。后续调研确认其以 `run_id` 作为流程身份，导致每个新 Turn 都重新 Understand，且 Compose Stage 与 Plugin Agent 仍存在分叉执行入口。目标设计与后续实施由 [HC-140 重构组合工作模式](HC-140-重构组合工作模式.md) 及其 [Spec](../spec/HC-140-重构组合工作模式.md) 替代；本文与原 Spec/Plan/Todo 保留为历史，不再继续实施或验收。

## 背景

Harness 当前的 Build 路径把一次用户消息交给主 Agent，自主完成模型/工具循环。Run、AgentExecution、Skill snapshot、Context、Policy、Transcript 与双端 Interactive Core 已经具备稳定 seam，但没有一个由代码掌控阶段、evidence 和独立 Review 的完整研发流程。

用户提供的 Compose 方案提出 `Understand → Plan → Build → Verify → Review`，并参考 Matt Pocock Skills、Addy Osmani Agent Skills 和 Superpowers。调研确认方法方向可用，但这些 Skill 自身是模型指令，不能承担可靠状态机、权限、持久化或真实 verification；各项目的 ticket/访谈/2～5 分钟步骤规则也与本仓库“一项完整功能 = 一个 Task”冲突。

## 当前存在的问题

- `run.start` 没有工作 mode，`RunCoordinator._execute()` 把通用生命周期与现有 Build Agent stream 写在同一路径，直接加 stage 分支会继续放大单体。
- 当前 Skill catalog 没有 canonical workflow invocation；强行新增会把 Compose 私有方法暴露给 Build 模型，并在 canonical/Plugin Skill adapter 间形成重复解释。
- 没有 ComposeRun、stage artifact、verification evidence 或 review finding 的结构化事实；模型只能用 conversation 自述进度。
- Runtime 没有经过现有 Policy/Approval/sandbox/并发锁的 verification command port；直接 subprocess 会绕过安全边界。
- TUI 虽显示 `tab modes` 提示，但空闲 composer 当前没有 Mode intent；`Shift+Tab` 已用于 Approval Mode。Web 也没有共享工作模式状态。

## 为什么现在要修改

弱模型依赖明确的流程、短 ContextPack 和可验证完成条件。继续只用 Prompt/Skill 约束，会让“何时进入下一阶段、失败后是否继续、验证是否真的通过、Reviewer 是否独立”仍由模型决定。与此同时，Harness 已完成 RunCoordinator、AgentDelegator、TeamCoordinator、Skill snapshot、Transcript/Context 分层和共享 Interactive Core，具备建立 Compose deep module 的必要基础；现在设计可以复用这些 seam，而不必重造底层。

## 目标设计

```text
input.submit + selected mode
  → run.start(build|compose)
  → RunCoordinator（唯一 owner / cancel / interaction / sequence / terminal）
       ├─ BuildRunAdapter（迁移现有行为）
       └─ ComposeRunAdapter
            → code-owned five-stage state machine
            → private method assets
            → Managed stage AgentExecution
            → canonical VerificationPort
            → structured ComposeArtifactStore
  → compose.state projection
  → shared Interactive Core
  → TUI / Web
```

完整领域模型、transition、interface、错误规则、UI 与测试 seam 见 [HC-138 Design](../spec/HC-138-建立BuildCompose双.md)。本任务是一个完整产品功能；Protocol、Agent、CLI、测试和文档是同一纵向结果的实现步骤，不创建子 Task。

详细依赖顺序、工作包边界和验证命令见 [HC-138 Plan](../plan/HC-138-建立BuildCompose双.md)，多执行 Thread 接力使用 [HC-138 Todo](../todo/HC-138-建立BuildCompose双.md)。Task 保留范围与整体验收，Design 保留设计事实，Plan/Todo 不得自行改变两者。

## 范围

- Build/Compose 工作模式选择、Run 受理事实和双端展示。
- Run execution adapter seam 与 Compose 五阶段 deep module。
- Compose 私有方法资产、structured artifact、ContextPack、Stage Agent 与 Reviewer。
- 经过既有安全边界的 deterministic verification 和有界 fix loop。
- fake model/backend 的跨包自动化测试、用户/架构文档与任务证据。

## 非范围

- Host 重启后续跑同一个 Compose Run；V1 沿用 owner 断开取消语义。
- Plugin Mode、公共 Workflow SDK、公共 workflow Skill invocation。
- Plugin specialist 自动选择、Agent mailbox、并行写任务或自由 Team 自协调。
- 自动 commit/push/PR、发布或远端 CI；既有用户显式授权路径不因 Compose 改变。
- 为实现步骤再创建 `ZC-*` 子任务或第二份 Design。

## 实施计划

### 1. 建立 Mode 与共享 Run execution seam

改什么：先以 Protocol + Python/TypeScript contract tests 固定 `build|compose`、`compose.state` projection 和 Run fingerprint；随后把现有 Build stream 从 `RunCoordinator` 收敛为 `BuildRunAdapter`，新增同 interface 的 Compose adapter 空壳，保持 coordinator 唯一拥有 owner、取消、Interaction、sequence、Transcript 和终态。

为什么：这是后续所有阶段的真实 seam。先迁移 Build 并证明行为不变，才能避免在 2000+ 行 coordinator 中堆 Compose 条件分支，也避免另建一套 Run 生命周期。

如何验证：Protocol 生成/校验通过；现有 Run 幂等、busy、owner、Interaction、取消、Transcript、唯一终态回归全绿；新增 adapter contract 证明 build 与 compose 都只能通过 lifecycle port 发事件和完成。

### 2. 交付 Understand → Plan → 用户确认的首个纵向 Compose 路径

改什么：实现 Compose state machine、artifact schema/store、ContextPackBuilder 和 understand/plan 私有方法资产；通过 StageAgentPort 启动 fresh Managed execution；复用 typed question Interaction 完成真实决策与“批准/修改/取消”方案门禁；每次 transition 发布完整有界 projection。

为什么：这一条先形成无写入副作用、可端到端演示的 tracer bullet，验证 artifact、stage、fresh context 和用户门禁是否能在现有 Run 内工作。

如何验证：fake Stage Agent 覆盖简单需求跳过访谈、事实自行查找、产品决策提问、artifact malformed retry、Plan revise/cancel/approve、sequence/revision 和无权限提升；TUI/Web 暂可用最小 projection fixture 验证状态形状。

### 3. 完成 Build → Verify 的安全执行与失败回路

改什么：把 Plan task 逐项交给 fresh builder execution；Runtime 按 task kind 确定 direct/TDD，并在失败后注入 Debug 方法资产；新增 VerificationPort adapter，通过 canonical execution backend、Policy、Approval、workspace boundary、sandbox 和 Host 读写锁运行有界命令，生成 fresh evidence；实现 verify-fix budget。

为什么：Compose 的核心可信度来自“Runtime 真实执行并读取证据”，而不是更多 Prompt。这个步骤必须把安全授权与 evidence 一起纵向打通。

如何验证：RED→GREEN evidence、文档/direct task、命令失败/超时/取消、Policy deny、审批、远端 sandbox、输出截断、fix round pass/exhausted 和 artifact/Transcript 隔离全部通过；任何缺 evidence 场景都不能进入 Review。

### 4. 完成独立 Review 与唯一完成判定

改什么：增加只读 Requirement Reviewer 与 Code Reviewer Managed spec，分别消费 goal/acceptance/plan/diff/evidence/project rules 的有界 ContextPack；合并为 typed ReviewReport；Required finding 生成来源明确的 fix task，重新经过 Build → Verify → Review，并应用 review-fix budget。

为什么：通过测试不等于实现了正确需求，也不等于代码结构正确；两个独立上下文分别守住 Spec 与 Code，避免单一 reviewer 互相稀释。

如何验证：两轴 pass、missing requirement、scope creep、architecture/security finding、Optional/Nit 不阻断、Required 回路修复、budget exhausted blocked、Reviewer 只读能力和父级取消传播通过；Run 只产生一个 completed/failed/blocked/cancelled 终态。

### 5. 完成共享交互体验、文档和项目级验收

改什么：在 Interactive Core 增加 Mode intent、busy gate 和 Compose projection；TUI/Web 实现空闲 Tab 切 Mode、Shift+Tab 继续切 Approval、计划确认、五阶段/task/evidence/review 展示与终态摘要；更新用户安全/交互文档、架构总览和 ADR 0001 的 Workflow 现状；运行 fake-model E2E 与项目级检查。

为什么：Mode 是共享业务状态，不能由两个 renderer 各自解释；用户必须清楚 Compose 改变流程但不改变权限。

如何验证：Core/TUI/Web parity、快捷键菜单优先级、active Run 禁切、窄终端/Web 响应式、键盘/ARIA、happy/failure/cancel E2E 通过；`bun run build`、`bun run typecheck`、`bun run test`、`bun run project:check` 通过并记录证据。

## 执行 Todo

- [x] 修改 canonical Protocol schema 与生成物：`run.start` 必填 mode、`run.started` 回传实际 mode、增加严格 `compose.state` payload；运行 `bun run protocol:generate && bun run protocol:check`，预期 TS/Python 类型、validator 和 fixture 一致。
- [x] 在 Python Run module 建立 `BuildRunAdapter` / `ComposeRunAdapter` 的真实双 adapter seam，把现有 Build 行为迁入前者；运行 RunCoordinator/Host focused tests，预期现有受理、幂等、owner、取消、Interaction、Transcript 和终态无回归。
- [x] 实现 Compose state/artifact/store/ContextPack 与私有方法资产，打通 Understand → Plan → question/approval；运行 compose focused tests，预期所有 transition、revision、artifact schema、revise/cancel/approve 可确定复现。
- [x] 实现 builder task 驱动和 Runtime-owned TDD/Debug 选择；运行 fake builder tests，预期行为/Bug task 有 RED→GREEN evidence，direct task 不被机械强制 TDD，失败只按预算重试。
- [x] 实现安全 VerificationPort 与 evidence schema，复用 canonical backend/Policy/Approval/sandbox/并发锁；运行 policy、tool、remote/local 和 compose verify tests，预期拒绝/超时/取消零旁路，无 fresh pass evidence 时状态机不能进入 Review。
- [x] 实现 Requirement/Code 两个只读 Managed Reviewer 和 review-fix loop；运行 delegation/execution/compose review tests，预期 fresh context、只读能力、两轴 finding、重新 Verify/Review 和 blocked 终态正确。
- [x] 在 Interactive Core 增加 mode intent、compose projection、busy/terminal cleanup；运行 interactive focused tests，预期 sequence + revision 双重拒绝迟到帧，TUI/Web snapshot 完全一致。
- [x] 在 TUI/Web 实现 Tab/Shift+Tab 正交、计划确认与五阶段/task/evidence/review 展示；运行组件、快捷键、Web/TUI parity、可访问性和响应式 tests，预期菜单打开时 Tab 只选候选，active operation 时不能切 Mode。
- [x] 增加 fake model/backend 的跨包 E2E：happy path、真实决策、Plan revise、Verify fail→fix、Review finding→fix、retry exhausted、cancel；预期不使用真实凭据且每条路径只有一个终态。
- [x] 更新 `docs/user/交互使用.md`、`docs/user/安全与沙箱.md`、架构总览、ADR 0001 和本任务证据，明确 Compose 不提权、V1 不支持 Host restart resume、版本影响；运行 `bun run build && bun run typecheck && bun run test && bun run project:check` 并记录完整结果。

## 验收清单

- [x] Build/Compose Mode 与 Approval Mode 正交，双端一致且运行中不可切换。
- [x] Build 生产行为无回归，Compose 不复制 Run lifecycle 或 Agent Runtime。
- [x] 五阶段与修复循环只由 typed artifact、evidence 和固定 budget 推进。
- [x] 用户只在真实决策与整体 Plan 处被阻塞，不需要反复输入“继续”。
- [x] Verify 无 fresh exit-code evidence 不通过，Review 来自两个独立只读 execution。
- [x] Policy、Approval、workspace、sandbox、MCP、network 和 delegation 无任何提权旁路。
- [x] Transcript、Compose artifact 与 LangGraph projection 各自只有一个事实职责，内部数据不越过 wire。
- [x] Protocol、Python/TS focused、双端、fake E2E、build/typecheck/test/project:check 与文档证据闭环。

## 定期复核记录

- 2026-08-11（Codex）：根据用户提供方案、三个本地 Skill 仓库、Harness 当前代码与外部一手资料完成首次调研和设计。结论为单一完整功能，不拆子 Task；下一次复核 2026-08-25。


## 实施证据

**工作包与提交（按 HC-138 Plan 的 10 个工作包）**

- WP1 `7af4ad0`：protocol v3 minor 3→4；`run.start`/`run.started` 必填 `build|compose`；新增严格 `compose.state` projection；Mode 纳入 Run fingerprint（同 thread/run 不同 mode → `RUN_ID_CONFLICT`）；全部 TS/Python 调用方与 fixture 一次迁移，无缺省 fallback。
- WP2 `8f92216`：`host/run_execution.py` 新增 `RunLifecyclePort`/`RunExecutionAdapter`/`BuildRunAdapter`；`RunCoordinator` 保持唯一拥有 sequence/终态/owner/取消/Interaction/资源释放；adapter 发终态被拒（`ADAPTER_TERMINAL_VIOLATION`）；执行路径 `RunError` 以稳定错误码收敛。
- WP3 `7b61713`：`compose/models.py`（严格 artifact + DAG/placeholder/字节/digest 校验）、`state_machine.py`（纯 transition、revision、attempt、schema-invalid 单次重试、verify/review budget、唯一终态）、`threads/compose_artifact_store.py`；Thread schema v11→v12 走既有 migration/backup/校验路径。
- Checkpoint A `44e9cef`（code-reviewer 复核 APPROVE + 跟进修复）：fix-round 必须携带来源明确 fix task；TASK_COMPLETE 强制依赖顺序；VERIFY_PASS 要求非空全 PASSED evidence；artifact_id 防碰撞；save_run 终态不可被覆盖；schema_retry_used 持久化。
- WP4 `e05f307`：`context_pack.py`（有界 ContextPack）、`stage_agents.py`（StageAgentPort + Managed 实现）、`workflow.py`（Understand→Plan→question/批准门禁）；私有方法资产 `understand.md`/`plan.md`；compose 不反向依赖 host（架构测试通过）。
- WP5 `cff13fc`：`TASK_STARTED` 事件 + `TASK_ATTEMPT_BUDGET=2`；`TaskResultArtifact.red_evidence`（behavior/bug/refactor 强制 RED，docs/config/style direct）；`build.md`/`tdd.md`/`debug.md`；依赖拓扑执行，attempt 耗尽 → blocked。
- WP6 `74f1256`：`verification.py` ManagedVerificationPort（canonical backend + Policy deny + 审批 + Host 写锁 + 无回退）；fresh evidence（command/时间/exit code/digest）；fail→fix task→Build→Verify 最多两轮。
- WP7 `30ee54a`：`restrict_spec_to_read_only`（Reviewer 只读能力视图）、双轴独立 Reviewer、`code-review.md`、Required finding fix 回路、Optional/Nit 不阻断；首个完整 Compose completed 路径。
- Checkpoint B `566b17e`（security-auditor 复核：无提权旁路 + 2 major 修复）：Reviewer 工具集与可见内置工具求交集；验证命令全程持锁；backend timeout 权威；输出脱敏；stage 图关闭 ask_user；idempotency key 混入任务摘要。
- WP8 `90406a8`：Interactive Core `workMode` + `composeState`（revision 递增才接受）、`work-mode.cycle` intent（busy 拒绝）、submit 携带冻结 mode、Web 推送链同构。
- WP9 `4c862c3`：TUI Tab/Shift+Tab 正交、Web Tab→work-mode-cycle、双端 Work/Approval 标签、Compose 五阶段/任务/evidence/blocked 展示。
- WP10（本工作包）：E2E 测试、用户/架构文档、项目级检查。
- 最终复核跟进（code-reviewer APPROVE_WITH_FOLLOW_UP）：每次 transition 的 projection 持久化到 `harness_compose_runs`（终态唯一）；stage 基础设施失败收敛 `COMPOSE_STAGE_EXECUTION_FAILED`（原始异常不越过 wire，AgentDelegationError 带稳定 code）；终态写入有界 assistant 摘要到 Transcript；Plan 门禁改用 workflow question（设计 invariant 8）；verify 审批 request_id 含命令哈希；`lastRun.composeSummary` 保留最终投影。
- 真实 Host 回归修复：Compose stage 复用主 Agent delegation policy 时内置 stage id 不在 `allowed_agents`，真实运行时第一个 stage 即 `DELEGATION_TARGET_FORBIDDEN`（fake 测试未覆盖真实 `AgentDelegator` 校验路径）。修复为 stage-scoped `DelegationPolicy`（`allowed_agents=(当前 stage,)`、`max_depth=1`、`max_parallelism=1`），不扩权且禁止 stage 再委派；新增 `test_stage_port.py` 走真实 `ManagedStageAgentPort → AgentDelegator` 链路回归。
- 诊断修复（用户报 `Expecting value: line 1 column 1 (char 0)` 且页面无显示）：根因为 stage 模型输出空/非 JSON 正文时 `parse_structured_output` 把 `json.loads` 裸解析器文本作为 `COMPOSE_ARTIFACT_INVALID` 的 message 越过 wire（TUI/Web 只显示 message）。修复为可读错误（「输出为空」/「不是有效 JSON（长度 N 字符）」），原文不越过 wire；新增 `test_stage_output_diagnostics.py` 用真实 port+workflow+coordinator 链路回归空输出与带解释文本两种形态。
- 失败后画面空白修复（用户报「error 前 TUI 没有任何内容显示」）：三层证据链确认 wire 端进度帧完整（run.started→compose.state rev0/1/2→run.failed）、reducer 端状态正确，缺陷在终态清理——`markRunFailed`/`RUN_CANCELLED` 立即清空 `composeState` 且 `lastRun` 无摘要，失败瞬间画面只剩错误文本（Compose 本身不输出流式文本）。修复：三个终态统一保留完整投影快照到 `lastRun.composeSummary`，TUI/Web 的进度面板在失败/取消/完成后渲染冻结的终态阶段（含哪个阶段 failed、attempts）；双端各有回归测试。

**测试与项目级检查（全部通过）**

- `cd packages/agent && .venv/bin/python -m pytest -q` → `1811 passed, 2 skipped`（含 compose 61 个测试：state machine 17、store 11、understand/plan 7、build 6、verification 10、review 7、e2e 4）。
- `cd packages/cli && bun test <canonical 脚本目录>` → `562 tests`，唯一失败为 `tests/web/bundle.test.ts`（EISDIR 的 bun 模块缓存环境问题，已在干净 master 复现，与本次改动无关）；`bun run typecheck` 通过。
- `bun run protocol:check` 通过；`bun run project:check` 见下方执行记录。
- 未使用任何真实模型凭据；全部 Compose 测试走 fake StageAgent/Verification/Interaction。

**版本影响**：协议 minor 3→4（两端同仓同发，无外部兼容承诺）；无独立版本号变更，未执行 `version:set`。

**后续步骤**：由用户/强模型对照 Task、Design、Plan 与完整 diff 复核后执行 `bun run task:complete` 并运行 `bun run tasks:sync`。
