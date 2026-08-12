---
id: HC-101
title: Thread 规范记录与 Plugin Agent/Team 产品入口
priority: P0
status: 已完成
owner: Codex
branch: master
scope: 在 ThreadPersistence 中建立只追加的用户、助手和工具语义记录，并补齐 Plugin Agent/Agent Team 的只读目录、生成、启动、查询、取消 Protocol、Client 与 TUI 入口。
acceptance: 压缩后 threads.open 仍返回完整用户历史；用户可通过 /agents 和 /teams 操作扩展角色与 Team；成员任务统一经 AgentDelegator 执行并可恢复；旧数据迁移不伪造缺失记录。
user_docs: docs/user/交互使用.md、docs/user/插件管理.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/扩展与插件机制设计方案.md、docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
test_evidence: Python 非沙箱回归 808 passed、2 skipped；Host Agent/Team/Plugin 控制面专项通过；TypeScript 128 passed、1 skipped；bun run typecheck、bun run build、bun run protocol:check、bun run test:project 通过。
references: HC-096、HC-097、HC-098
completed_at: 2026-08-05
---

## 背景

Plugin Agent 已能被主 Agent 的 `task` 工具调用，TeamCoordinator 也已能执行固定 DAG，但用户尚无
稳定入口查看这些定义或直接启动 Team。因此代码具备执行核心，产品仍不可发现、不可控制。
当前 `ThreadPersistence` 同时保存 Thread 索引、Run binding、Context Artifact、Summary 和 LangGraph checkpoint。`threads.open` 通过 checkpoint 中的 `messages` 还原历史，而 `ContextWindowMiddleware` 会使用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 将这些消息替换成“摘要 + 最近消息”。

这意味着同一份 LangGraph 消息既是模型的有限工作上下文，又被当作 UI 恢复历史。压缩对模型是必要操作，却不应删除用户本地的完整会话记录。

本任务对应 `REQ-CTX-004` 和 `REQ-CTX-008`，先建立后续投影、压缩检查点和恢复能力共同依赖的事实层。

## 当前存在的问题

- Protocol 只声明 Plugin 管理接口，没有 `agents.*` 与 `teams.*`。
- TypeScript Client 无法类型化调用 Agent/Team 能力。
- TUI 的 `/agents`、`/teams` 尚未注册。
- Team 生成器只能由 Python 代码调用，用户无法预览后再显式启动。
### 1. UI 历史会随模型压缩一起缩短

`ThreadPersistence.open_thread()` 当前从 checkpoint 归一化 `ThreadMessage`。自动或手动压缩改写 checkpoint 后，更早的用户、助手和工具内容无法继续回放。

### 2. Run 受理只保存索引和模型绑定

`accept_run(AcceptRun)` 会原子写入 Thread 索引和 `RunExecutionBinding`，但没有写入独立的用户消息记录。助手和工具结果也只存在于 LangGraph checkpoint 与实时事件流中。

### 3. 实时事件不能直接当作规范记录

`content.delta`、`tool.delta` 等流式片段可能重复、取消或缺少终态。若逐帧写入，会把传输细节变成恢复事实，并在幂等重试时产生重复消息。

### 4. 旧数据无法还原已经丢失的历史

当前 v6 数据库只保留最新 checkpoint。迁移可以保存“现在还剩下什么”，但不能推断更早已被压缩掉的原文。

## 为什么现在要修改

- 后续 `ContextProjector` 必须有一个不会被压缩覆盖的输入事实源。
- Run 受理、完成、取消和重试已经由 [HC-089](HC-089-让Run生命周期成为deepm.md) 的 `RunCoordinator` 集中拥有，适合在明确生命周期点追加语义记录。
- 若先继续扩展现有 checkpoint 重写，未来只能通过更多兼容逻辑猜测完整历史。
文档要求扩展包里的 Agent 和由 AgentDefinition 生成的协作 Team 可实际使用。只交付内部类会让
安装成功与用户可用之间出现断层，也无法验证取消、恢复和唯一终态的生产链路。

## 目标设计

在 `ThreadPersistence` 内建立 Append-only Transcript。白话来说：

```text
/agents 或 /teams
  → TypeScript AgentClient
  → JSON-RPC agents.* / teams.*
  → 启动期 ExtensionCatalogSnapshot
  → AgentCatalog / TeamDefinition
  → TeamCoordinator
  → AgentDelegator
  → AgentEnginePool
模型怎么压缩都可以
    ↓
用户实际说过什么、Agent 最终回答什么、工具最终返回什么
始终另外按顺序保存在本地
```

建议领域形状：

```python
TranscriptRecord(
    record_id,
    thread_id,
    run_id,
    execution_id,
    sequence,
    kind,          # user / assistant / tool / context
    payload,
    created_at_ms,
)
```

稳定规则：

- `accept_run()` 在同一事务中写入 Run binding、Thread 索引和本次用户消息；同一 Run 幂等重试不得重复追加。
- 助手只在形成完整语义消息后追加；流式 delta 不单独进入 Transcript。
- 工具记录保留稳定 `tool_call_id`、工具名、结果状态和用户可见内容。大型正文进入不可变 Artifact，Transcript 保存摘要、内容哈希、长度和 Artifact 引用。
- 内部压缩或恢复事件可以保存为 `kind=context`，但默认不作为普通 user / assistant 对话展示。
- 顺序使用持久化的单调 sequence，并以稳定 record ID 或 `(run_id, execution_id, sequence, kind)` 保证幂等。
- 所有读取继续按 `project_fingerprint + thread_id` 隔离；Artifact 引用不能跨 Project 或 Thread 解析。

事实所有权：

```text
ThreadPersistence Transcript = 完整本地会话事实
LangGraph checkpoint         = 当前执行状态和暂时的模型消息
实时 AgentEvent              = 表现层流，不是持久化事实
```
Team 启动请求立即返回 `accepted`；客户端随后通过 `teams.inspect` 查询 SQLite 中的可恢复状态。
取消只设置对应 Team 的取消令牌，不扩大到无关 Run。

## 实施步骤

1. 在 Protocol schema 增加 `agents.read`、`teams.read`、`teams.manage` 和类型化方法。
2. Server 从同一启动快照返回 Agent/Team 脱敏摘要，拒绝 Prompt、绝对路径或密钥进入响应。
3. 将 Team 生成结果作为 Host 生命周期内的显式预览定义；固定 Plugin Team 仍来自不可变 catalog。
4. 异步执行 Team，使用 SQLite TeamStateStore、AgentDelegator 和 AgentEnginePool；实现查询与取消。
5. TypeScript Client 增加类型化调用，TUI 注册 `/agents`、`/teams` 并展示可读结果。
6. 增加 Python dispatcher、Protocol fixture、Client、Command Registry/Dispatcher 和 Controller 测试。
1. 盘点 `RunCoordinator._stream_agent()` 当前产出的 user、assistant、tool 语义边界，明确哪些事件可以形成完成记录，哪些只是 delta。
2. 在 `thread_persistence.py` 增加 Transcript typed command/result 和当前 v6 后继 schema；表级 SQL 不暴露给调用方。
3. 扩展 `AcceptRun`，让 Run binding、Thread 索引和用户记录同事务提交；冲突与幂等继续沿用现有 Run ID 语义。
4. 为完成的助手消息和工具结果增加一次性 append 生命周期操作；取消、失败和重试不能留下半条工具记录或重复回答。
5. 复用 Context Artifact 保存大型工具原文；记录中保留可验证的摘要、哈希、原始长度和 Artifact ID。
6. 将 `open_thread()` 改为从 Transcript 生成 `ThreadMessage`，保持现有 JSON-RPC v3 user / assistant / tool 返回形状。
7. 为当前 v6 数据建立一次性 legacy bootstrap：把现有 checkpoint 消息写成初始记录，并持久化 `legacy_incomplete_history=true` 或等价事实。
8. Schema 升级前使用 SQLite backup API 或等价机制生成可恢复备份；迁移失败必须回滚，不能留下部分新表或错误版本号。
9. 删除生产路径对“checkpoint 等于完整 UI 历史”的依赖，不保留长期双写的旧历史读取分支。
10. 更新用户和开发者文档，说明本地完整记录与模型压缩视图的区别。

## 主要代码位置

- `packages/agent/harness_agent/thread_persistence.py`
- `packages/agent/harness_agent/run_coordinator.py`
- `packages/agent/harness_agent/server.py`
- `packages/agent/tests/test_thread_persistence.py`
- `packages/agent/tests/test_run_coordinator.py`
- `packages/agent/tests/test_thread_rpc.py`
- `packages/agent/tests/test_server.py`

## 范围

- Plugin Agent 的列表和详情。
- 固定 Team 与生成 Team 的列表、详情、启动、查询和取消。
- 当前 Host 生命周期内的生成预览；Team 运行状态持久化。
- 保存当前线性 Thread 的 user、assistant、tool 和内部 context 记录。
- 保持现有 `threads.open` wire shape，不新增 Timeline、Fork 或 Rewind 协议。
- 对大型内容继续使用本机 Artifact，避免在多个表中重复保存原文。
- 诚实迁移现有 v6 checkpoint 中还能读取到的历史。

## 非范围

- 不实现模型工作投影或新的压缩检查点；由后续任务完成。
- 不实现 Cline 风格从完整记录重新摘要。
- 不实现跨 Thread 搜索、向量索引、长期记忆或云端同步。
- 不把 token delta、工具进度帧或审批等待逐帧保存为 Transcript。
- 不增加 Agent 邮箱或 peer-to-peer 消息。
- 不允许客户端上传任意 TeamDefinition、模型名、Policy 或工具列表。
- 不实现跨 Host 的远程 Team。

## 验收清单

- [x] 同一 Run 的重复受理不会重复追加用户记录。
- [x] 完成的助手消息和工具结果按稳定顺序追加；失败/取消收尾只提交已完成语义，不提交流式半条助手缓冲。
- [x] 大型工具结果可以通过当前 Project/Thread 内的 Artifact 完整恢复，wire 1 MiB 截断前已进入 typed Transcript/Artifact 边界。
- [x] 自动或手动压缩 LangGraph 消息后，`threads.open` 仍返回完整用户可见历史。
- [x] 不同 Project 或 Thread 不能读取对方 Transcript 和 Artifact。
- [x] v6 迁移只保存现有 checkpoint 可证明的内容，并明确标记更早历史可能不完整、不伪造 run/execution 身份。
- [x] 迁移候选/正式阶段均以 SQLite 写锁重读版本；唯一临时备份路径、事务 rollback、失败/取消和双连接并发升级均有验证，不能把旧库覆盖已提交的新库。
- [x] Server、RunCoordinator 通过 ThreadPersistence typed lifecycle 接口工作，不依赖 Transcript 表名或 SQL 顺序。

## 实施记录

- v7 新增 `harness_thread_transcript`、`harness_thread_history_metadata`，并为 Context Artifact 增加 `content_sha256` 与 `byte_length`；`accept_run` 原子写 binding、Thread 索引和 user Transcript，`open_thread` 只从 Transcript 读取。
- RunCoordinator 在 root namespace 的原始 `AIMessage`/`AIMessageChunk` 与 `ToolMessage` 到达时捕获完整语义；assistant typed payload 保存稳定 `tool_calls`、参数对象/原文、编码和 valid/partial/invalid/unavailable 状态，tool-call-only assistant 也形成记录；无稳定 chunk ID 的分片按当前 assistant/tool 回合合并，大工具结果在 wire 截断前进入 Artifact。ToolMessage 到达后立即提交前置 assistant tool-call 与工具结果批次；失败/取消收尾保留已完成 pending 记录并丢弃未完成 assistant buffer。
- 增量修正 ToolMessage 关联：结果 ID 只精确命中已有映射，或绑定唯一的无 provider ID assistant 候选；稳定 ID mismatch、orphan result、多候选和无 ID/index 的第二个明确 call start 均 fail closed。provider 参数使用严格 JSON，非 JSON 值保留 invalid/raw，不由字符串化制造 valid。
- v6 bootstrap 保留 checkpoint 仍可证明的 AI tool_calls（含 tool-call-only assistant、call ID/name/args 和 invalid raw 状态）及 ToolMessage ID；legacy incomplete 只表达更早历史可能已丢失，不作为丢弃当前 tool-call 事实的理由。
- `subgraphs=True` 的非空 namespace 不进入 root Transcript；由于当前 Protocol v3 没有 execution/provenance 字段，live translation 也暂时显式抑制该 namespace，避免污染 root 的 tool correlation state；这是本任务的兼容边界，不改变 Protocol v3 wire shape。
- 并行 assistant 声明 A/B 而仅收到并提交 A 时，Transcript 有意保留“完整声明 + 已完成结果”的可检测不完整尾部；后续 HC-104 必须拒绝把 unmatched tool-call group 直接投影回模型，HC-101 不将其伪装成完整 LangChain 历史。
- ThreadPersistence 与 ProjectScopedAsyncSqliteSaver 共享同一 connection-level asyncio lock；`open_thread` 在显式只读事务中一次读取 summary、Transcript 和 legacy 标记，避免并发追加造成混合快照。
- v6 bootstrap 按 project-scoped checkpoint 导入当前仍可证明的消息，记录 `legacy_incomplete_history=1`，不生成虚假 run/execution 或 HC-102 snapshot；迁移前备份只作恢复材料，事务失败优先 rollback。
- 迁移使用“候选写锁重读 → 锁外唯一临时 backup 并校验版本 → 正式写锁重读 → 单事务 DDL/bootstrap/user_version”的两阶段边界；并发两个 project Host 的回归测试证明最终保持 v7、两个 project 的同名 thread 不串线。

## 验证命令

```bash
cd packages/agent && .venv/bin/python -m pytest -q \
  tests/test_thread_persistence.py \
  tests/test_run_coordinator.py \
  tests/test_thread_rpc.py \
  tests/test_server.py
bun run typecheck
bun run test
bun run project:check
```

自动化测试不得调用真实模型或使用真实 API Key。

## 版本影响

本任务改变本地数据库和 Thread 恢复语义。实现完成时必须在任务证据中记录 schema 迁移与回滚验证；是否调整产品版本只能通过 `bun run version:set` 决定，立项阶段不修改版本。

## 前置

- HC-088
- HC-089
- [x] `/agents` 能显示当前启动快照中可派发的 Plugin Agent。
- [x] `/teams` 能列出固定 Team、生成预览并启动或取消运行。
- [x] Team 成员仍只通过 AgentDelegator 进入 Managed AgentEngine。
- [x] Team 查询可在进程恢复后读取 SQLite 状态。
- [x] Protocol Python/TypeScript 生成物、dispatcher 与 TUI 测试通过。
