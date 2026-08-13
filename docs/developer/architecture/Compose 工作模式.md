# Compose 工作模式架构（Work Item 模型）

关联任务：[HC-140](../task/archive/HC-140-重构组合工作模式.md) 及对应 [Spec](../spec/HC-140-重构组合工作模式.md)。

## 定位

Compose 从「一个用户消息触发一次固定五阶段 ComposeRun」重构为「一个 Compose Thread 顺序承载多个持久
ComposeWorkItem」。Work Item 跨 Turn、Run 与 Host 重启持续；Markdown 保存需求与研发产物正文，SQLite 保存执行
事实；UI 上的阶段只是 readiness gate 的投影，不是第二份状态。HC-138 的固定五阶段状态机与旧
`harness_compose_runs/artifacts` 表是历史实现，不作为恢复 fallback。

## 事实边界（单一事实源）

| 事实 | 位置 | 说明 |
| --- | --- | --- |
| Thread 工作模式 | SQLite `harness_thread_modes` | 首个有效 Run 冻结 `build|compose`，之后不可普通切换 |
| Work Item 生命周期 | SQLite `harness_compose_work_items` | active/waiting_user/blocked/completed/abandoned；同 Thread 唯一未终结项（唯一索引 + BEGIN IMMEDIATE）；terminal 走 revision CAS |
| Run 绑定 | SQLite `harness_compose_work_item_run_bindings` | run_id 固定 work_item_id；改绑 conflict |
| Activity 执行 | SQLite `harness_compose_work_item_activities` | kind/status/attempt；重启扫描把遗留 running 收敛 interrupted；WAITING_USER/INTERRUPTED/RETRYABLE_FAILED/FAILED/CANCELLED/COMPLETED 可 restart |
| 副作用 | SQLite `harness_compose_work_item_effects` | intent→receipt/unknown；相同 effect key 幂等，receipt 只来自真实对账 |
| 验证/评审证据 | SQLite `harness_compose_work_item_evidence` | implementation/verification/review/report 总结证据绑定文档 digest + workspace revision |
| 文档正文 | Workspace `docs/compose/<slug>/task|spec|plan|todo|report.md` | front matter（work_item_id/kind/revision/status/updated_at）+ 正文；数据库只存路径/digest/revision/lineage，不复制正文 |
| 人工确认 | SQLite `harness_compose_work_item_confirmations` | confirmation_id 原子 digest group；Task/Spec/Plan 三 gate |

digest 不匹配规则：Markdown 当前 digest 与数据库 confirmed digest 不同 ⇒ 确认立即 stale；Runtime 从不把数据库
摘要覆盖回用户修改后的文件。todo.md 勾选与修复项追加是计划批准后的预期演进：`plan_confirmed` 运行时判定以
plan.md digest 为准（批准时 Plan+Todo 双 digest 联合审计），勾选变化使下游证据 stale 而不重触发 Plan gate。

## 顶层流程

```text
run.start(mode=compose)
  → RunCoordinator 受理
  → ComposeRunAdapter.execute_turn
  → ComposeWorkItemEngine.execute_turn
      1. 解析 Thread mode / active Work Item / 显式命令 / Interaction reply / 确定继续词
      2. TurnIntentResolver（仅自然语言歧义，五分类，无 Tool，误判收敛澄清）
      3. 载入或创建 Work Item；Run binding
      4. 重算文档 digest 与九项 readiness
      5. 门禁顺序：Task gate → Spec gate → Plan/Todo gate（typed Interaction）
      6. 批准实施后同 Turn 自动闭环：Implement → Verify → Review → Report → Guard complete（有界步数）
      7. 收敛 waiting_user | blocked | turn_budget | completed
```

readiness 顺序：`task_confirmed → spec_confirmed → plan_confirmed → todo_executable → implementation_current →
verification_fresh → review_fresh → report_current → complete`。`complete` 还要求无 pending/unknown effect，由
`CompletionGuard`（`compose/guard.py`）以 revision CAS 提交；模型输出不能推进确认或终结 Work Item。

## 关键模块

| 模块 | 职责 |
| --- | --- |
| `compose/work_item_engine.py` | deep module：execute_turn/inspect/abandon；路由、readiness、Activity 流水线、terminal CAS；不拥有 SQLite 连接或 graph |
| `compose/models.py` | 领域枚举与严格模型（Work Item/Activity/Effect/Evidence 等） |
| `compose/readiness.py` | 纯 ReadinessResolver：文档 digest + confirmation groups + 执行证据 → 九谓词 |
| `compose/document_store.py` | `docs/compose/<slug>/` 固定五文件安全读写（Snapshot CAS），防穿越/碰撞 |
| `compose/turn_intent.py` | 五分类 TurnIntentResolver：确定词表优先、小上下文分类、schema 非法收敛 unclear |
| `compose/recovery.py` | RecoveryScanner（running→interrupted + torn effect 枚举）、OutcomeReconciler（RETRYABLE/RECONCILABLE/UNKNOWN 三值对账）、BoundedProviderRetry（429/Retry-After） |
| `compose/guard.py` | CompletionGuard：report.md 生成（绑定全部输入 digest）与 complete CAS |
| `compose/activities/task|spec|plan|implement|verify|review.py` | 门禁与执行 Activity；各自持有 start/restart/finish ledger 语义 |
| `threads/compose_work_item_store.py` | Work Item/Activity/Effect/Evidence SQLite 事实层；共享锁 + BEGIN IMMEDIATE |
| `runtime/managed_agent_executor.py` | Build/Compose/Plugin 统一执行底座；`provider_retry` 有界 429 重试 seam |
| `skills/builtin/` | 原版 Skill bundle（manifest + digest/license 校验，Compose required Skill 只解析 reserved identity） |

## 恢复语义

- Esc/Run cancel：只中断当前执行；Activity 由启动扫描或下一次进入收敛；Work Item 保持未终结。
- 429：executor 按 `provider_retry` 在 stream round 边界有界重试；预算耗尽 Activity retryable_failed，可继续。
- 崩溃：Host 重启后 `RecoveryScanner` 收敛 running→interrupted；torn effect 按 verifier 对账：证明未执行→重放、
  证明已执行→补 receipt、结果未知→blocked + typed decision。文件 effect 用真实重读 Snapshot 对账。
- 模型 token 中间不续写；访谈/草稿状态落在 task.md 的 draft status 与 SQLite Activity 事实中。

## 安全边界

- 所有 Agent execution 经 ManagedAgentExecutor、ResolvedAgentSpec、Policy、WorkspaceBoundary、Sandbox、Tool
  approval 与取消链；Compose 不扩大权限（见 docs/user/安全与沙箱.md）。
- Compose 首版只使用固定内置角色，不自动选择 Plugin Agent。
- 文档路径由 Runtime 规范化并限定工作空间；confirmation/receipt/evidence/terminal 全走 SQLite 事务 + CAS。
- outcome unknown 的副作用绝不自动重放；旧 workspace revision 的验证/评审绝不视为 fresh。

## 协议与交互

- `run.start(mode=compose)` 自动 attach/create Work Item；`compose.inspect` 只读投影；`compose.abandon` CAS 操作；
  `compose.work_item` / `compose.activity` 为类型化 projection 事件。
- `/new-work`、`/abandon` 仅 Compose 可见；`/btw` 双 Mode 可见；补全/菜单/Help/dispatch 共用同一 Command
  Registry availability，dispatch 再复核，错误 Mode 手输命令返回 `COMMAND_MODE_UNAVAILABLE`，不提交模型。
