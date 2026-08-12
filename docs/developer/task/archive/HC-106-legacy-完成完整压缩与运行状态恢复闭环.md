---
id: HC-106-legacy
title: 完成完整压缩与运行状态恢复闭环
priority: P0
status: 已完成
owner: Codex (Luna Max)
branch: master
scope: 统一自动、手动和溢出路径的完整压缩服务，按 Qwen 风格校验并原子提交 full 检查点，再从当前 RunContext 与 LangGraph state 确定恢复真实运行状态。
acceptance: 自动压缩只在微压缩后仍超水位时生成摘要；手动 compact 强制请求完整压缩；overflow 只恢复一次；成功后 Todo、模式、Artifact 和当前 Run 系统上下文正确恢复，失败不覆盖上一个有效投影。
user_docs: docs/user/交互使用.md、docs/user/故障排查.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
test_evidence: 窄复审：3 个 P1 已关闭，无新增 P0/P1；直接回归 52+2+7 passed。独立相关回归：70 passed/34 deselected。项目检查：typecheck passed；project:check passed；Python full 670 passed/1 skipped，唯一 sandbox WebSocket bind failure 在沙箱外单测 1 passed；TS full 112 passed/1 skipped，2 个既有 TUI renderer/SearchPicker 失败；git diff --check passed。
references: docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
completed_at: 2026-08-03
---

## 背景

当前 `ContextWindowMiddleware` 已支持结构化摘要、20% 最低节省率、Artifact、三次失败熔断和一次 `ContextOverflowError` 恢复；`context.compact` 也能在空闲 Thread 上强制摘要。

[HC-104](HC-104-legacy-分离模型工作投影并持久化压缩检.md) 把这些结果迁移为模型投影检查点，[HC-105](HC-105-legacy-建立分级上下文压力策略与确定性.md) 建立“先微压缩、重新计量、仍不足再升级”的分级策略。本任务完成剩余的完整压缩、原子提交和压缩后连续性，对应 `REQ-CTX-006` 与 `REQ-CTX-007`。

## 当前存在的问题

### 1. 自动、手动和 overflow 编排仍不统一

自动路径位于 Middleware，手动路径由 Server 直接调用 `compact_now()` 并更新 LangGraph state，overflow 又在模型调用异常分支中执行。三者容易出现不同校验、持久化或重试规则。

### 2. 摘要承担了不该承担的恢复职责

当前压缩结果主要是“摘要 + 最近消息”。如果 AGENTS、Skill、Todo、审批模式或 Artifact 只靠摘要文字保留，模型可能漏掉、误写或固化旧版本。

### 3. 完整压缩缺少一次原子领域提交

Artifact、Summary、熔断状态、Projection checkpoint 和 LangGraph 缓存之间需要同成同败。中间失败不能让 latest checkpoint 引用未提交 Artifact。

### 4. 多次压缩与失败恢复缺少统一保证

连续摘要、空结果、摘要膨胀或 provider 错误必须保留上一个有效检查点；自动失败熔断不能阻止用户显式 `/compact`，但手动路径也不能绕过结果校验。

## 为什么现在要修改

- 分级微压缩只有接入同一完整压缩服务后，才能真正减少不必要的摘要调用。
- RunContextSnapshot 已把系统上下文从历史投影中分离，适合在压缩后重新注入当前版本。
- Todo、Interaction 和执行模式已经有真实结构化状态，不应再从 Artifact 文本或摘要中反向解析。

## 目标设计

完整压缩主链：

```text
当前投影
→ 自动路径先接受 HC-105 的 micro projection；手动路径显式 force full
→ 选取可摘要前缀和必须保留的最近原子组
→ 调用无工具摘要模型
→ 校验内容、长度、来源、结构和节省量
→ 生成 mode=full CompressionCheckpoint 草稿
→ 恢复当前结构化状态
→ 原子提交 Artifact + Summary + checkpoint + 熔断状态
→ ContextProjector 同步 LangGraph 投影缓存
→ 继续当前 Run 或完成手动请求
```

触发语义：

```text
自动：micro 后仍 >= full watermark 才执行
手动：绕过自动 watermark，明确请求 full；短历史或不节省时返回 skipped
overflow：micro → 必要时 full → 只重试模型一次
```

`RuntimeStateRehydrator` 第一阶段只渲染已经真实存在的状态：

- Todo；
- 当前执行模式与审批模式；
- 当前 RunContextSnapshot 的能力/上下文身份；
- 压缩过程中产生或继续引用的 Artifact；
- 必要的最近完整 tool call / result 原子组。

AGENTS、Skill 和工具 Schema 不写进摘要；它们始终由当前 RunContextSnapshot 和 Middleware 独立注入。未来长期记忆同样不能永久固化进 full summary。

## 实施步骤

1. 把自动、手动和 overflow 的完整压缩逻辑收敛为一个 typed service；调用方只传 trigger、当前投影、RunContext 和结构化 state。
2. 保留现有结构化摘要章节，明确摘要只记录目标、事实、决策、改动、测试、未决项和 Artifact 引用，不复制系统规则。
3. 在调用摘要模型前限制输入上限，并按完整 user/tool 原子组选择前缀；摘要输入过长时确定性裁剪最旧组，不截断半个工具调用。
4. 统一校验空结果、输出上限、截断、来源边界、消息结构、投影膨胀和最低节省率；失败返回 typed reason。
5. 建立 `RuntimeStateRehydrator` 或 ContextLifecycle 内部 renderer，从 LangGraph state 和当前 RunContext 恢复 Todo、模式与 Artifact，不解析摘要文本猜测状态。
6. 自动路径只在 HC-105 重新计量后仍超过 full watermark 时调用；同一轮 micro + full 只提交最终 full 检查点。
7. 手动 `/compact` 强制 full，但继续执行相同校验；自动熔断打开时仍允许手动尝试。
8. overflow 使用相同微压缩和 full 服务，只允许一次模型重试；再次 overflow 原样抛出可诊断错误。
9. 扩展 `ThreadPersistence` 的原子提交，使 Artifact、Summary、full checkpoint 和失败状态不会部分写入。
10. 成功后由 ContextProjector 更新 LangGraph 投影；失败继续使用上一个有效检查点和投影。
11. 保留三次连续自动失败熔断，成功后重置；手动结果不得错误重置无关 Thread 的状态。
12. 更新 context.updated、用户文档和故障排查，说明 auto/manual/overflow 的可观察差异。

## 本轮实现结果

- 已将生产压缩入口收敛到 `ContextCompactor`，`ContextWindowMiddleware` 只负责 canonical projection、事件翻译和一次 overflow retry；手动 Server 入口传递 typed `CompressionRequest`，保留 JSON-RPC v3 外形。
- 已增加 `RuntimeStateSnapshot`/`RuntimeStateRehydrator` 与 schema v11，运行态只从 LangGraph channel、当前 `RunContext`/snapshot 和完整工具组恢复，不从摘要或 Artifact 正文猜测。
- 本轮窄复审补充真实模型 input cap（含摘要 prompt/framing/安全余量）的原子组边界、当前 ResolvedAgentSpec 策略恢复和 runtime-state fail-closed/事务保留回归；3 个 P1 已关闭，无新增 P0/P1。

## 主要代码位置

- `packages/agent/harness_agent/context_window.py`
- `packages/agent/harness_agent/context_compaction.py`
- `packages/agent/harness_agent/runtime_state.py`
- `packages/agent/harness_agent/context_pressure.py`
- `packages/agent/harness_agent/context_projection.py`
- `packages/agent/harness_agent/context_lifecycle.py`
- `packages/agent/harness_agent/thread_persistence.py`
- `packages/agent/harness_agent/run_context.py`
- `packages/agent/harness_agent/run_coordinator.py`
- `packages/agent/harness_agent/server.py`
- `packages/agent/tests/test_context_window.py`
- `packages/agent/tests/test_context_projection.py`
- `packages/agent/tests/test_run_coordinator.py`
- `packages/agent/tests/test_server.py`

## 范围

- 当前 root Agent 的自动、手动和 overflow 完整压缩。
- full CompressionCheckpoint、摘要校验、失败熔断和一次 overflow 恢复。
- Todo、模式、当前 RunContextSnapshot 和 Artifact 的确定性恢复。
- 当前线性 Thread 的多次压缩。

## 非范围

- 不从完整 Transcript 重新生成另一套手动摘要语义。
- 不恢复尚不存在的后台任务、Agent Team、mailbox 或独立 Plan 状态。
- 不实现长期记忆检索；只保持 Run 动态上下文可以在未来重新注入。
- 不允许摘要模型调用工具或修改工作区。

## 验收清单

- [x] 自动路径只在微压缩后仍超过 full watermark 时调用摘要模型。
- [x] 微压缩足够时不生成 full checkpoint，也不消耗摘要模型调用。
- [x] 手动 `/compact` 明确尝试完整压缩；短历史、无有效前缀或节省不足时返回 skipped 而非假成功。
- [x] overflow 复用同一服务且最多重试一次，不形成递归压缩循环。
- [x] 空、超长、截断、膨胀、来源错误或低节省摘要不会覆盖上一个有效检查点。
- [x] Artifact、Summary、full checkpoint 和 ContextState 原子提交。
- [x] 压缩后 Todo、执行/审批模式和 Artifact 引用从结构化状态恢复。
- [x] AGENTS、Skill 和工具能力不进入摘要，模型继续使用当前 RunContextSnapshot。
- [x] 连续多次 full 压缩只使用 latest valid checkpoint + tail，所有旧检查点仍可审计。
- [x] 三次自动失败后熔断，成功后重置；手动路径不受自动熔断阻止但仍受结果校验。

## 验证命令

```bash
cd packages/agent && .venv/bin/python -m pytest -q \
  tests/test_context_window.py \
  tests/test_context_pressure.py \
  tests/test_context_projection.py \
  tests/test_run_coordinator.py \
  tests/test_server.py
bun run typecheck
bun run test
bun run project:check
```

测试以 mock summary model 覆盖成功、空输出、超长、低节省、异常、连续失败和二次 overflow，不使用真实 API Key。

## 版本影响

本任务改变 `/compact`、自动压缩和 overflow 的用户可见行为，相关用户文档与迁移证据已更新。本任务不单独调整版本；统一版本切换与发布记录留到依赖本任务的 HC-107 完成，避免中间能力形成半发布状态。

## 前置

- HC-102
- HC-105
