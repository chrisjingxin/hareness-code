---
id: HC-105-legacy
title: 建立分级上下文压力策略与确定性微压缩
priority: P0
status: 已完成
owner: Codex (Luna Max)
branch: master
scope: 将压缩触发从 ContextWindowMiddleware 中提取为纯 ContextPressurePolicy，并按总体水位、可回收工具压力和顶层空闲条件先执行可恢复微压缩、重新计量后再决定是否升级。
acceptance: 工具压力可提前触发微压缩；达到完整压缩水位时始终先微压缩并使用新估算复判；空闲条件只在顶层 Run 首次模型调用生效；规范记录不变且工具原子组与最近结果得到保留。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
test_evidence: focused Python 64 passed；CLI TUI state 10 passed；typecheck/project:check/diff check 通过；全量 Python 617 passed/1 skipped，唯一沙箱端口失败在沙箱外定点通过；TS 111 passed/1 skipped，2 个既有 TUI 时序失败已隔离记录；Luna Max 增量评审无 P0/P1
references: docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md、1062df6
completed_at: 2026-08-03
---

## 背景

当前 `ContextWindowMiddleware._prepare()` 已按 50% / 60% / 80% / 90% 分层处理：

```text
<50%      不处理
50%～60%  只报告
60%～80%  工具结果脱水
80%～90%  摘要并保留两轮
>=90%     强制摘要并保留一轮
```

这个基础应当保留，但当前达到 80% 后会直接进入摘要，不会先尝试低损耗工具回收并重新计量。阈值判断、消息修改、摘要调用、持久化和事件也集中在同一中间件中。

本任务对应 `REQ-CTX-005`。参考 Claude Code 非官方还原快照的“微压缩先于完整压缩、工具压力和空闲条件分别触发、保留最近结果”行为，但不采用 Anthropic `cache_edits`、API context management 或其内部固定数值。

## 当前存在的问题

### 1. “何时处理”与“如何处理”耦合

无法单独测试同一压力输入应选择 report、micro、full 还是 overflow，也无法保证微压缩后使用新 Token 值继续判断。

### 2. 只有总体占用比例

大量旧工具输出可能在总体占用尚低时已造成明显浪费；纯文本历史即使达到高水位，也可能没有任何适合微压缩的内容。

### 3. 高水位跳过低损耗回收

达到摘要水位后当前直接调用模型。若旧工具结果本可释放足够空间，会产生不必要的摘要成本和信息损失。

### 4. 空闲恢复与普通模型续跑没有区分

顶层 Run 长时间未活动时可以考虑清理已经冷却的旧工具结果，但工具续跑、模型重试和 Interaction 恢复绝不能被误判成新的空闲会话。

## 为什么现在要修改

- [HC-104](HC-104-legacy-分离模型工作投影并持久化压缩检.md) 已使压缩只作用于模型投影，并支持 `mode=micro` 检查点。
- 完整压缩闭环必须建立在确定的“微压缩后仍不足”事实上，而不是读取旧估算。
- 把策略做成纯模块，可以在不调用模型和不操作 SQLite 的情况下覆盖大量边界测试。

## 目标设计

新增 `context_pressure.py`：

```python
ContextPressureSnapshot(
    projected_input_tokens,
    input_cap_tokens,
    occupancy_ratio,
    reclaimable_tool_tokens,
    reclaimable_tool_count,
    idle_duration_ms,
)

ContextPressureDecision(
    action,       # none / report / micro / full / overflow
    reason,       # occupancy / tool_pressure / idle / manual / overflow
    keep_recent,
)
```

执行闭环：

```text
ContextProjector 输出
→ PressurePolicy 测量
→ 命中工具压力、微压缩水位或空闲条件？
   ├─ 否：继续；若已到 full 且无候选，micro 为 no-op
   └─ 是：归档并替换旧的可恢复工具结果
→ 对新投影重新测量
→ 低于 full：提交 mode=micro 检查点
→ 仍高于 full：把内存中的微压缩投影交给完整压缩
```

策略维度：

1. **总体水位**：固定满足 `report < micro < full < hard limit`。
2. **工具压力**：根据可回收工具结果的数量和估算 Token 独立判断，可以提前触发。
3. **空闲条件**：可配置的辅助触发器，仅顶层 Run 的首次模型调用可用。

阈值决策要求：

- 初始总体水位以现有 50% / 60% / 80% / 90% 行为作为兼容基线；若调整，必须在任务实施结果中记录测试和预算依据。
- 工具压力阈值、空闲时长与 `keep_recent` 集中在一个 typed policy 配置中，不散落常量，也不照抄 Claude 的 180k 或 60 分钟值。
- 实施前用工具密集、文本密集和混合历史 fixture 校准默认值；最终值和默认开关写入开发文档与测试。
- 所有 Token 比例基于本次模型实际 input cap，预留输出和估算误差空间。

微压缩规则：

- 只选择已经完成、较旧、能够从 Artifact 恢复的工具结果。
- 候选按 Harness 工具语义和可恢复性分类，不照抄第三方工具名列表。
- 完整保留 tool call / tool result 原子组，保留最近 N 个候选且至少一个。
- 原文先进入不可变 Artifact，再以包含工具名、摘要、哈希和 `/.harness/history/<artifact-id>.md` 的占位内容替换。
- 已经是有效 Artifact 占位的结果再次执行时为 no-op，不重复归档。
- Transcript 永远不被微压缩修改。

## 实施步骤

1. 从 `ContextWindowMiddleware` 提取纯预算测量和决策函数，建立 `ContextPressurePolicy` 与 typed snapshot/decision。
2. 集中当前 input cap、总体水位、工具压力、空闲条件和近期保留配置，建立关系校验并拒绝无序或越界值。
3. 为每次模型调用计算总体与可回收压力；从 Run 生命周期明确传入“是否顶层 Run 首次模型调用”，不要从时间戳猜测调用类型。
4. 建立工具结果可恢复性分类和候选选择器，保持完整 user turn 与 tool call/result 边界。
5. 复用 Context Artifact 执行确定性微压缩，生成新投影、Artifact 草稿、pressure before/after 和触发原因。
6. 强制执行“达到 full 仍先 micro → 重新计量 → 再决定 full”；没有候选时 micro 无副作用。
7. 微压缩单独足够时通过 HC-104 原子提交 `mode=micro` 检查点；仍需 full 时不提交中间检查点，只把内存投影交给后续完整压缩。
8. 让空闲条件只在新的顶层 Run 首次模型调用评估；工具续跑、重试、Interaction 恢复、缺失或异常时间戳不得触发。
9. 使用现有 `context.updated` action 字符串区分 report、pressure micro、idle micro、skip 和 failure；若不新增字段则保持 JSON-RPC v3 schema。
10. 更新 CLI 状态映射和测试，使微压缩显示为“归档工具结果”而不是普通“正在思考”。

## 主要代码位置

- `packages/agent/harness_agent/context_pressure.py`
- `packages/agent/harness_agent/context_window.py`
- `packages/agent/harness_agent/context_projection.py`
- `packages/agent/harness_agent/run_context.py`
- `packages/agent/harness_agent/run_coordinator.py`
- `packages/agent/harness_agent/thread_persistence.py`
- `packages/cli/src/tui/state.ts`
- `packages/agent/tests/test_context_pressure.py`
- `packages/agent/tests/test_context_window.py`
- `packages/agent/tests/test_run_coordinator.py`
- `packages/cli/tests/tui/state.test.ts`

## 范围

- 总体水位、工具压力和顶层空闲条件三类信号。
- 可恢复工具结果的确定性 Artifact 化与占位替换。
- `mode=micro` 检查点及现有 context.updated/TUI 显示。
- 自动模型调用前的微压缩；手动与完整压缩由后续任务统一。

## 非范围

- 不在本任务调用摘要模型或完成完整压缩恢复链。
- 不实现 Anthropic cache editing、API 原生 context management 或 Prompt Cache 控制。
- 不清理系统规则、用户消息、助手决策文本、未完成工具调用或审批状态。
- 不实现后台定时压缩；空闲条件只在用户发起新顶层 Run 时检查。
- 不把具体阈值写入 Protocol 或允许模型自行修改策略。

## 验收清单

- [ ] Policy 对同一投影和配置产生确定 decision，且不修改消息或写数据库。
- [ ] 总体水位始终满足 report < micro < full < hard limit。
- [ ] 工具密集历史可在 full 水位前因工具压力触发微压缩。
- [ ] 达到 full 时先微压缩并使用 pressure_after 复判；旧估算不参与第二次决策。
- [ ] 微压缩足够时不调用摘要模型；不足时同一轮继续进入 full。
- [ ] 文本密集且无候选时 micro 为 no-op，并能直接升级 full。
- [ ] 最近 N 个候选和至少一个结果得到保留，tool call/result 原子组不破坏。
- [ ] 重复微压缩不会重复创建 Artifact 或占位。
- [ ] 空闲条件只在顶层 Run 首次模型调用触发，其他续跑路径不误触发。
- [ ] Transcript 保持不变，micro 检查点和 Artifact 同事务提交。

## 验证命令

```bash
cd packages/agent && .venv/bin/python -m pytest -q \
  tests/test_context_pressure.py \
  tests/test_context_window.py \
  tests/test_context_projection.py \
  tests/test_run_coordinator.py
cd packages/cli && bun test tests/tui/state.test.ts
cd ../.. && bun run typecheck
bun run test
bun run project:check
```

测试使用确定的 Token estimator 和 mock 时钟，不依赖真实供应商缓存或模型。

## 版本影响

自动上下文处理与 TUI 状态属于用户可见行为。实现完成时必须记录最终策略默认值和兼容依据，并通过正式版本流程决定版本影响。

本任务决定不单独调整根 `VERSION` 或 `CHANGELOG.md`：JSON-RPC v3 外形未变，
且本改动是尚未发布的上下文改造链中间步骤，后续仍由 HC-106 完成 full 闭环、
HC-107 完成最终用户行为切换。正式 SemVer 与 Changelog 统一在 HC-107 的版本流程中决定，
避免把未形成完整闭环的内部增量单独发布。

## HC-105 实施记录（2026-08-01）

- 新增 `packages/agent/harness_agent/context_pressure.py`，以无副作用的 `ContextPressurePolicy`、typed snapshot/decision 和 `ModelCallLifecycle` 集中管理压力测量、分级决策与调用阶段。
- 保留 `50% / 60% / 80% / 90%` 总体水位；工具压力默认 `8,192` token 或 `2` 个可回收结果，`keep_recent=1`，idle 默认开启且阈值为 `900,000 ms`（15 分钟）。默认值由工具密集、文本密集、混合 fixture 及上下文窗口回归校准，不复制参考项目固定数值。
- `ContextWindowMiddleware` 在 full 前先规划确定性工具 micro，使用新投影的 `pressure_after` 再走策略；低于 full 时通过 `ContextProjector`/`ThreadPersistence.commit_context` 原子提交 `mode=micro`，仍超出时只把内存投影交给现有摘要路径，不留下中间 micro checkpoint。
- 无 `ThreadPersistence` 时在自动 micro/full 入口失败关闭：原消息、LangGraph 投影和 Artifact 指针均不改写，不调用摘要模型；overflow 恢复沿用兼容的未改写 skip 语义。
- full 路径在 micro 后以新的 `pressure_after` 重算 `keep_turns` 和最终 checkpoint 的 `pressure_before`；压力降到 full/hard 之间时走普通摘要并保留两轮。
- micro 草稿只在最终 full 事务中提交；Summary 的 Artifact 索引和最终 CompressionCheckpoint 同时声明 micro 与 history Artifact，并由现有 project/thread/digest 校验，摘要不携带旧指针或事务失败时均不留下孤儿写入。
- 候选只包括旧、已完成、tool call/result 配对且尚未含 Artifact 指针的结果；占位包含工具名、首尾预览、SHA-256 与 Artifact 路径，保留最近候选并保持 Transcript 不变；重复处理为 no-op。
- Run 生命周期显式区分顶层首次调用、工具续跑、Interaction 恢复和子 Agent；异常/缺失时间戳不会触发 idle。CLI 将 `pressure_micro`/`idle_micro` 显示为“正在归档工具结果”，JSON-RPC v3 外形未变。

验收进度：

- [x] Policy 纯测量/决策无模型、SQLite 或消息副作用；总体水位关系校验集中在 typed config。
- [x] 工具压力提前触发；full 先 micro、复测后决定；micro 足够时不调用摘要模型，不足时同一轮进入 full。
- [x] 无持久化路径对中低/高水位和 overflow 保持原消息，`rewrite=False`，不产生 Artifact 指针或摘要模型调用。
- [x] full 以 micro 后快照重新决定保留轮数，并在 full/hard 之间使用普通摘要和两轮保留。
- [x] 最终 full Summary/CompressionCheckpoint 完整声明同事务的 micro+history Artifact；失败事务无孤儿 Artifact，重启后 latest-valid 可恢复。
- [x] 无候选时 no-op；保留最近候选、工具原子组和 Transcript；Artifact 占位幂等。
- [x] idle 仅由显式 Run 生命周期在顶层首次模型调用使用；TUI 状态与 focused/full 检查已验证。

- 固定 Luna Max 增量评审确认 3 个 P1 已关闭，无新增 P0/P1，HC-105 可进入完成检查。
- 主会话最终复跑 focused Python → `64 passed`，CLI TUI state → `10 passed`；`bun run typecheck`、`bun run project:check` 与 `git diff --check` 通过。
- `bun run test:py` → `617 passed / 1 skipped / 1 failed`；唯一失败是沙箱禁止绑定 `127.0.0.1:0`，沙箱外定点复跑 → `1 passed`。
- `bun run test:ts` → `111 passed / 1 skipped / 2 failed`；两项均为既有 TUI Markdown renderer / SearchPicker 时序失败，与本任务 state 映射无交集，未误记为通过。

本任务不包含 HC-106/HC-107 的完整恢复链、手动压缩新语义或长期记忆。

## 前置

- HC-104
