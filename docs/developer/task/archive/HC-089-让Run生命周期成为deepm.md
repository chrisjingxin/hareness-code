---
id: HC-089
title: 让 Run 生命周期成为 deep module
priority: P0
status: 已完成
owner: codex
branch: codex/zc-089-run-lifecycle
scope: 从 AgentHost 的 protocol implementation 中提取完整 Run lifecycle module，集中拥有受理、owner、幂等、AgentEngine lease、执行、Interaction、终态和资源释放。
acceptance: Protocol dispatcher 只负责校验、capability、wire 转换与 event fanout；Run 行为可通过一个稳定 interface 测试，不再依赖 Server private 方法。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构重构计划.md、docs/developer/architecture/架构总览.md、docs/developer/architecture/adr/0002-project-host-multi-connection.md
test_evidence: bun run typecheck；bun run test（TS 123 passed/1 skipped，Python 498 passed/1 skipped）；bun run project:check；RunCoordinator 生命周期回归 10 passed；packages/agent/.venv/bin/python -m py_compile 通过
references: codex/zc-089-run-lifecycle
completed_at: 2026-07-29
---

## 背景

JSON-RPC v3 和 AgentHost 已经支持一个 Project 内的多个 Connection、单 Thread 单 active Run、Run owner、Interaction、AgentEnginePool 和事件 fanout。Host/Connection 领域边界已经建立，但 Run 的完整执行仍直接实现在 `server.py` 的 `AgentHost` 中。

当前一次 Run 涉及：

```text
run.start
  → registry lock / busy / idempotency
  → Skill 与模型解析
  → SQLite 原子受理
  → 先发 JSON-RPC response
  → AgentEnginePool lease
  → PromptEpoch / RunContext
  → DeepAgents stream
  → Interaction request/response
  → AgentEvent 翻译与 fanout
  → completed / failed / cancelled
  → checkpoint refresh / lease release / registry cleanup
```

这些步骤是一个领域生命周期，不是 transport 的细节。

## 当前存在的问题

### 1. AgentHost 同时是 protocol adapter 和 Run implementation

`server.py` 当前约 2,374 行。`AgentHost` 既处理：

- initialize、capability 和 JSON-RPC error；
- stdio/WebSocket Connection；
- attachment token；
- config、model、skill、MCP、Thread operation；
- Run registry 与完整执行。

Run 相关代码从 `_handle_run_start()` 一直延伸到 `_release_run_runtime()`，transport 与执行知识无法独立变化。

### 2. Run 状态分散在多个私有结构

`ActiveRun`、`_runs`、`_starting_threads`、`_registry_lock`、Connection pending request、AgentEngine lease 和 ThreadPersistence 分别保存部分状态。要判断一个 Run 是否已受理、可取消、正在 Interaction、已经终结或正在释放资源，需要跨多个字段推断。

### 3. 唯一终态依赖多处分支配合

`_execute_run()` 负责正常完成、失败和大多数取消；`_handle_run_cancel()` 还必须补偿“task 创建后尚未获得时间片”的取消终态；Connection 关闭和 Host 关闭又有各自收敛路径。

当前行为有测试保护，但 invariant 没有由一个 module 集中保证：

```text
每个 accepted Run 恰好一个终态
终态之后不再产生事件
所有 lease 和 pending Interaction 必须释放
```

### 4. Interaction 与 owner 规则嵌在 JSON-RPC implementation

Run lifecycle 直接创建反向 JSON-RPC request、管理 request ID/future、恢复 DeepAgents interrupt，并在 owner 断开时 fail closed。Core 执行因此无法在不模拟整个 protocol peer 的情况下测试。

### 5. 事件翻译和 fanout 混在一起

DeepAgents stream → AgentEvent 是执行 adapter；AgentEvent → 多 Connection JSON-RPC notification 是 protocol fanout。当前二者都由 AgentHost 私有方法完成，无法单独验证“所有观察者看到同一 event ID/sequence”与“模型 stream 正确翻译”。

### 6. 测试 surface 是 Server private implementation

Run 测试需要构造 AgentHost、伪造内部字段、调用私有方法或检查 `_runs`。重排 implementation 会产生大范围测试修改，即使 external Run 行为不变。

## 为什么现在要修改

- stdio 与 WebSocket 已经是两个真实 transport adapter；继续把 Run 绑在 Server 上会让每个新表现层增加交叉复杂度。
- Run owner、幂等、Interaction 和资源释放都是高风险 invariant，应该由单一 module 保证，而不是依赖 handler 约定。
- [HC-086](HC-086-集中模型选择与Run执行绑定.md) 和 [HC-088](HC-088-按生命周期深化Thread持久.md) 会提供类型化执行绑定与生命周期持久化 seam，此时提取 Run 不需要复制旧的 dict/schema 知识。
- 后续任何 Run replay、steer 或 Subagent 能力都必须建立在稳定 Run Core 上；当前阶段只收敛已有行为，不提前实现这些功能。

## 目标设计

建立 Host-scoped `RunCoordinator`（名称可调整）作为 deep module。AgentHost 持有一个实例，但不能直接操作其 registry。

建议 external interface：

```python
class RunCoordinator:
    async def start(self, command: StartRun, owner: ConnectionRef) -> RunExecution: ...
    async def cancel(self, run: RunRef, requester: ConnectionRef) -> CancelResult: ...
    async def owner_disconnected(self, connection: ConnectionRef) -> None: ...
    async def close(self) -> None: ...
```

`RunExecution` 返回 accepted result 与冷启动/受控启动的 `AsyncIterable[AgentEvent]`。Protocol dispatcher 必须先发送 accepted response，再开始消费事件，从 interface 上保持“response 严格早于首事件”，而不是让 Run module 调用 JSON-RPC。

内部依赖：

```text
Protocol dispatcher
  → RunCoordinator
      ├─ RunRegistry
      ├─ ExecutionBindingResolver
      ├─ ThreadPersistence
      ├─ AgentEnginePool
      ├─ DeepAgents stream adapter
      └─ InteractionPort
            ├─ Protocol interaction adapter
            └─ in-memory test adapter
```

职责划分：

- RunCoordinator：受理、owner、幂等、状态机、执行、Interaction、唯一终态、资源释放。
- AgentHost：Project 资源所有权、Connection 生命周期、capability、event fanout。
- Protocol dispatcher：Schema 校验、wire DTO、错误映射、请求/响应顺序。
- DeepAgents adapter：库事件翻译为领域 AgentEvent，不拥有 Run registry。
- InteractionPort：向 owner 请求审批/问答；Run module 只认识类型化 Interaction 与结果。

稳定错误必须由 Run module 以领域 code 表达：`THREAD_BUSY`、`RUN_NOT_FOUND`、`RUN_NOT_OWNER`、`RUN_ID_CONFLICT`、`INTERACTION_EXPIRED`。英文 message 仍只用于诊断。

## 实施步骤

1. 用状态图明确 `accepted → running ↔ interacting → completed|failed|cancelled`，列出每个状态允许的命令和资源。
2. 定义 `StartRun`、`RunRef`、`RunExecution`、`RunCompletion`、领域错误和 `InteractionPort`。
3. 将 `_runs`、`_starting_threads`、registry lock 和 owner 校验迁入 RunCoordinator。
4. 接入 HC-086 的 resolved binding 与 HC-088 的 `accept_run()`，保持相同 Run ID 的幂等与冲突语义。
5. 将 AgentEngine lease、RunContext、stream loop、Interaction resume 和终态释放迁入 module。
6. 把 DeepAgents stream 翻译移到内部 adapter；module 产出统一 AgentEvent，AgentHost 只分发。
7. 让 stdio/WebSocket dispatcher 先返回 accepted，再消费 RunExecution.events。
8. 将 owner disconnect、立即 cancel、Interaction 超时和 Host close 统一走状态机收敛。
9. 用 in-memory Interaction/Event adapter 与真实临时 SQLite 测完整 lifecycle；删除依赖 Server private 字段的等价测试。

## 范围

- 建立覆盖 `start`、`cancel` 和 owner 断开收敛的最小 interface。
- module 内集中执行 Thread busy/Run ID 幂等、Run owner、模型绑定、PromptEpoch、AgentEnginePool lease、DeepAgents stream、Interaction 和唯一终态。
- AgentHost 保留 Project 资源所有权；ProtocolConnection 保留连接状态；transport adapter 不进入 Run module。
- 用可控的执行与事件 adapter 测试完整生命周期，删除穿透 Server private implementation 的等价测试。

## 非范围

- 不按 RPC handler 拆 module。
- 不实现 active Run replay、owner takeover、daemon 或远程部署。
- 不改变 JSON-RPC v3 wire contract。

## 验收清单

- [ ] 同一 Thread 单 active Run、不同 Thread 并发语义保持不变。
- [ ] owner-only cancel/Interaction 与断开 fail-closed 行为保持不变。
- [ ] 每个 Run 恰好产生一个终态并完整释放 AgentEngine lease。
- [ ] accepted response 在首个 AgentEvent 前发送。
- [ ] AgentHost 不再直接读写 RunRegistry 内部状态。
- [ ] stdio 与 WebSocket 对同一 Run 复用完全相同的 lifecycle implementation。
- [ ] Server 测试不再通过 private 方法构造 Run 生命周期。

## 实施结果

已新增 `packages/agent/harness_agent/run_coordinator.py` 作为 Host-scoped deep module：它集中管理 Run registry、受理幂等、owner、取消、DeepAgents stream、Interaction resume、终态事件和 AgentEngine lease 释放。`AgentHost` 只负责把 JSON-RPC 参数转换为类型化 Run command、accepted response 顺序和 event fanout；stdio 与 WebSocket 共用同一个 Coordinator。

Run module 的执行和事件翻译已通过 in-memory interface 测试；原先依赖 `server.py` 私有执行方法的测试已迁移到 `RunCoordinator` 类型和 adapter seam。完整项目检查仍待运行；当前工作区另有 Runtime→AgentEngine 迁移改动，不在本任务范围内。

## 前置

- HC-086
- HC-088
