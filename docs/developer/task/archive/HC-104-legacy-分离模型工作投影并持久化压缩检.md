---
id: HC-104-legacy
title: 分离模型工作投影并持久化压缩检查点
priority: P0
status: 已完成
owner: Codex
branch: master
scope: 建立 ContextProjector 和版本化 CompressionCheckpoint，使模型从最新有效检查点加后续规范记录构造有限历史，LangGraph checkpoint 只保留执行状态与投影缓存。
acceptance: threads.open 始终读取完整 Transcript；模型输入由 latest valid checkpoint + tail 确定生成；一次或多次压缩不覆盖规范记录；旧 v6 Context Artifact/Summary 可迁移且错误检查点不会被采用。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
test_evidence: 54 项 persistence/projection 定点通过；typecheck/project:check/diff check 通过；全量 Python 600 passed/1 skipped，两个环境/时序失败沙箱外定点 2 passed；TS 111 passed/1 skipped，2 个既有 TUI 失败已隔离记录；Luna Max 最终增量评审无 P0/P1
references: docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md、6a09aff
completed_at: 2026-08-01
---

## 背景

[HC-101](HC-101-legacy-建立Host控制租约与可撤销W.md) 建立 Append-only Transcript 后，完整本地历史已经有独立事实来源。但当前模型仍直接从 LangGraph checkpoint 的 `messages` 恢复，`ContextWindowMiddleware` 和手动 `/compact` 继续通过 `RemoveMessage(REMOVE_ALL_MESSAGES)` 把重写结果当成唯一历史。

本任务建立“完整记录”和“模型看到的有限视图”之间的正式投影层，对应 `REQ-CTX-004`、`REQ-CTX-006` 和 `REQ-CTX-008`。本任务先迁移现有压缩行为，不改变具体水位策略。

## 当前存在的问题

### 1. 没有独立的模型投影所有者

Run 开始、自动压缩、手动压缩和 Thread 恢复都直接操作 LangGraph messages。调用方无法回答当前模型历史来自哪次重写、覆盖到哪条规范记录。

### 2. Context Summary 不是可直接恢复的检查点

`harness_context_summaries` 保存摘要文本和 source 范围，Artifact 保存归档原文，但没有保存完整 projected messages、触发方式、算法版本、父边界和输入来源摘要。

### 3. 多次压缩依赖“摘要套摘要”的消息现状

当前下一次压缩只能读取已经被上一次重写的 LangGraph messages。旧摘要仍存在，但没有明确的 latest-valid 选择和失效规则。

### 4. 手动压缩绕过持久化领域层

`AgentHost._compact_with_agent_engine()` 在成功后直接调用 `agent.aupdate_state()` 重写 messages。Server 因而知道 LangGraph reducer 和 Context 存储的组合顺序。

## 为什么现在要修改

- 分级压力策略必须只改变模型工作投影，不能再次覆盖 Transcript。
- 自动、手动和 overflow 只有共享同一检查点模型，才能形成确定恢复链。
- LangGraph checkpoint 应继续承担 Todo、interrupt 等第三方运行状态，但不应是完整会话事实或投影版本的唯一来源。

## 目标设计

新增 `ContextProjector`：

```text
读取最新有效 CompressionCheckpoint
  ├─ 有：checkpoint.projected_messages + checkpoint 之后的 Transcript
  └─ 无：从 Transcript 构造模型消息
→ 校验 user / assistant / tool 原子组
→ 输出有序 LangChain BaseMessage
→ 同步到 LangGraph 模型消息缓存
```

建议检查点：

```python
CompressionCheckpoint(
    checkpoint_id,
    thread_id,
    source_record_sequence,
    source_digest,
    mode,                 # micro / full
    rewrite_version,
    projected_messages,
    artifact_ids,
    trigger,
    pressure_before,
    pressure_after,
    created_at_ms,
)
```

“CompressionCheckpoint”是 Harness 的模型投影版本，不是 LangGraph 自己的 checkpoint。多个版本全部保留用于审计，但下一次模型输入只选择来源边界有效的最新一份。

稳定规则：

- `source_record_sequence` 表示检查点已经覆盖到哪条 Transcript；之后记录按顺序追加。
- `source_digest`、rewrite version、消息解码和 Artifact 归属任一校验失败时，该检查点不能进入模型输入。
- `mode=micro` 保存确定性工具清理投影；`mode=full` 保存结构化摘要投影。
- 同一轮微压缩后继续完整压缩时，只提交最终 full 检查点；中间动作只留审计信息。
- LangGraph messages 是由 Projector 同步的缓存；恢复失败时必须从 Harness 持久化事实重新生成，不能反向覆盖 Transcript。

## 实施步骤

1. 定义 `ContextProjector` 的 typed input/output、检查点选择规则和 tool call / result 原子组校验。
2. 在 `thread_persistence.py` 增加 CompressionCheckpoint schema 与 `load_projection()`、`commit_projection()` 等生命周期操作；具体命名可按现有 module 风格调整。
3. 将 projected messages 以版本化、可校验格式保存，拒绝未知消息类型、跨 Thread Artifact 和损坏 JSON。
4. 在 Run 开始前从 latest checkpoint + tail 构造模型输入，并只通过 Projector 同步 LangGraph messages。
5. 调整 `ContextWindowMiddleware`：保留当前 50/60/80/90 行为，但输出 projection rewrite/checkpoint draft，不再把改写后的 messages 当作完整历史。
6. 调整手动 `/compact`：Server 只调用统一 Context 生命周期操作，不直接拼 `RemoveMessage`、持久化和索引刷新顺序。
7. 保存所有旧检查点用于审计；Projector 按 sequence、digest、version 和创建顺序确定唯一 latest valid。
8. 迁移 v6 Context Summary、Artifact 与当前 checkpoint：建立明确标记的 legacy 初始投影，不宣称能恢复缺失 Transcript。
9. 每次 schema 升级前创建可恢复备份；迁移和检查点提交必须原子完成。
10. 删除生产路径中“从 LangGraph messages 生成完整 UI 历史”及 Server 直接提交 Context rewrite 的旧入口。

## 主要代码位置

- `packages/agent/harness_agent/context_projection.py`
- `packages/agent/harness_agent/context_window.py`
- `packages/agent/harness_agent/thread_persistence.py`
- `packages/agent/harness_agent/run_coordinator.py`
- `packages/agent/harness_agent/server.py`
- `packages/agent/harness_agent/agent.py`
- `packages/agent/tests/test_context_projection.py`
- `packages/agent/tests/test_context_window.py`
- `packages/agent/tests/test_thread_persistence.py`
- `packages/agent/tests/test_thread_rpc.py`
- `packages/agent/tests/test_server.py`

## 范围

- 当前线性 Thread 的 latest checkpoint + tail 投影。
- micro/full 两种检查点 mode 的持久化形状；本任务只让现有压缩先写 full。
- LangGraph messages 与 Harness 投影之间的单向同步。
- 当前 v6 Context Summary、Artifact 和 checkpoint 的诚实迁移。

## 非范围

- 不在本任务改变自动压缩水位、候选工具选择或空闲触发。
- 不实现从完整 Transcript 重新摘要的第二种手动语义。
- 不实现历史编辑、Rewind、分支或任意检查点切换 UI。
- 不实现长期记忆。

## 验收清单

- [x] 没有检查点时，模型投影由 Transcript 确定生成。
- [x] 存在检查点时，模型只看到最新有效 checkpoint + tail。
- [x] 压缩前后 `threads.open` 返回相同的完整用户可见 Transcript。
- [x] 连续两个检查点都可审计，但恢复只使用第二个及其后记录。
- [x] source sequence、digest、版本、消息或 Artifact 校验失败的检查点不会进入模型输入。
- [x] Run 开始和压缩提交后，LangGraph messages 与 Projector 输出一致。
- [x] Server 和 Middleware 不直接组合 Transcript、Artifact、Summary、检查点表级写入。
- [x] v6 legacy Thread 可继续恢复，并明确标记早期历史不完整。

## 验证命令

```bash
cd packages/agent && .venv/bin/python -m pytest -q \
  tests/test_context_projection.py \
  tests/test_context_window.py \
  tests/test_thread_persistence.py \
  tests/test_thread_rpc.py \
  tests/test_server.py
bun run typecheck
bun run test
bun run project:check
```

测试必须覆盖损坏 JSON、错误 source digest、缺失 Artifact、跨 Project/Thread 引用和迁移回滚。

## 版本影响

本任务改变模型历史恢复和本地 schema，但保持 JSON-RPC v3 外形。实现完成时必须记录迁移与回滚证据；版本号只能通过项目版本脚本调整。

当前实现将本地 SQLite schema 从 v8 升级为 v10：v9 引入 CompressionCheckpoint，v10 增加整条 Context rewrite 的稳定幂等载荷。没有改动 JSON-RPC v3、根 `VERSION` 或 `CHANGELOG.md`，因此本任务无需调整产品版本号。

## 实施与证据（2026-08-01）

- 新增 `context_projection.py`：严格版本化 user/assistant/tool 消息、工具原子组校验、latest-valid 选择、Transcript tail 构造和 LangGraph 缓存单向刷新。
- `ThreadPersistence` v9 在同一 `BEGIN IMMEDIATE` 事务提交 Artifact、Summary、熔断状态和 CompressionCheckpoint；source digest 覆盖完整 typed Transcript payload，Artifact 按 project/thread、内容摘要和字节数校验。
- v8→v9 在升级前使用既有 backup 机制；旧 Artifact 先补齐可验证元数据，只有严格解码、工具组完整且 Artifact 归属成立的旧 LangGraph 视图才会建立 `legacy/incomplete` 初始投影。
- 自动和手动压缩保留现有 50/60/80/90 水位、候选选择、摘要和 20% 节省规则；现有重写均先写 `mode=full` 检查点，没有提前实现 HC-105/HC-106。
- 投影专项：`.venv/bin/python -m pytest -q tests/test_context_projection.py` → `14 passed`。
- focused suite：`.venv/bin/python -m pytest -q tests/test_context_projection.py tests/test_context_window.py tests/test_thread_persistence.py tests/test_thread_rpc.py tests/test_server.py` → `87 passed`；除沙箱禁止 pytest cache 写入外，出现一次既有 server 测试未及时关闭 aiosqlite worker 的 warning，未影响结果。
- `bun run typecheck` 和 `bun run project:check` 通过。`bun run test` 的 TS 阶段为 `111 passed / 1 skipped / 2 failed`，失败是与本任务无关的两项已知 TUI 时序波动；两个文件单独复跑 `16 passed`。
- `bun run test:py` 为 `587 passed / 1 skipped / 1 failed`，唯一失败是沙箱禁止绑定 `127.0.0.1:0`；沙箱外单独复跑该 WebSocket 用例 `1 passed`。所有自动测试均使用 fake/mock 模型，未使用真实模型、API 或凭据。
- 增量评审 P1 修复：`_dehydrate()` / `_summarize()` 的 checkpoint 现在按最终 `projected_messages` 提取完整 Artifact 引用；持久化层继续验证每个声明 ID 都存在于当前 project/thread，而返回值与 `context.updated.artifact_ids` 仍只报告本次新建项。
- 同一 `checkpoint_id` 重试会比较解析后的 `source_record_sequence` 与 `source_digest`：隐式 latest 边界在 Transcript 前进后不再静默接受，显式固定边界在后续追加记录后仍可幂等重试。
- P1 定点回归：`.venv/bin/python -m pytest -q tests/test_context_projection.py::test_checkpoint_commit_is_idempotent_and_conflict_fails tests/test_context_window.py::test_consecutive_rewrites_declare_all_artifacts_and_restart_from_latest` → `2 passed`。连续真实执行脱水、摘要、再脱水，覆盖旧+新 Artifact 声明以及关闭/重开 SQLite 后恢复 latest-valid checkpoint。
- P1 直接相关 suite：`.venv/bin/python -m pytest -q tests/test_context_projection.py tests/test_context_window.py tests/test_thread_persistence.py` → `49 passed`；`bun run typecheck` 与 `bun run project:check` 通过。仅有沙箱禁止写 `.pytest_cache` 的 warning。
- 固定 Luna Max 第二轮 P1 修复：legacy/incomplete checkpoint 默认只供审计，正常 `project()` / `sync_cache()` 从可用 Transcript 重建；`load_latest_valid_compression_checkpoint(..., include_legacy_incomplete=True)` 是显式审计入口。
- `commit_payload` 覆盖完整 `CommitContextRewrite` 稳定语义，checkpoint ID 精确重试返回首次 Artifact/Summary/State/checkpoint 结果；Artifact、Summary、State 或来源边界任一不同均冲突并回滚。带 checkpoint 且调用方未指定 ID 的 Artifact 使用内容与命令边界生成稳定 ID。
- Transcript 写入与 Projector 解码都要求 `arguments_status=valid` 时存在显式对象参数；缺失字段、非字符串 typed ID/名称不再通过 `{}` 或 `str()` 猜测。所有持久化 JSON 读取统一使用拒绝 NaN/Infinity 的严格 decoder，最新损坏候选会回退较早有效版本。
- 本轮投影定点 `18 passed`；迁移与生命周期 `40 passed`；直接相关组合 suite 为 `101 passed / 1 failed`，唯一失败是既有 stdio 子进程 2 秒超时，单独复跑 `1 passed`。`bun run typecheck` 与 `bun run project:check` 通过；最终复跑证据见本轮交接。
- 同一直接相关组合 suite 最终复跑 → `102 passed`；仅有沙箱禁止写 `.pytest_cache` 的 warning。
- 固定评审最后一项 P1：legacy assistant/tool result 导入不再对 call ID、name、type、error、result ID/status 使用 `str()`。只有原值为契约字符串时才进入关联字段；畸形值记录为 `legacy_invalid_fields` 和 unmatched，结果内容及 incomplete 边界仍保留，`open_thread` 不伪造工具名，Projector 对该历史失败关闭。
- malformed legacy 定点（同时覆盖合法字符串、唯一无 ID 关联、显式空参数）→ `5 passed`；限定 persistence + projection suite → `47 passed`。仅有沙箱禁止写 `.pytest_cache` 的 warning。
- 最后复审两分支：非字符串 error 会进入 `legacy_invalid_fields`，所有 `arguments_status != valid` 的 legacy call 均不进入 pending/late-bind，合法 ID 的后续 result 也保持 unmatched。公开 `TranscriptAppend` 已移除 `legacy_import`；迁移宽松许可改为私有事务参数，普通 append/batch 对嵌套或结果 legacy marker 均失败关闭且不产生部分记录。
- 本轮两分支定点 → `4 passed`；限定 persistence + projection suite 最终复跑 → `49 passed`。仅有沙箱禁止写 `.pytest_cache` 的 warning。
- 极窄 error 复审：legacy `error` 改为按字段存在性与严格类型判断。显式 `None` 与缺失等价；空字符串保持既有无错误语义；非空字符串保留为可证明错误；任何存在且非 None 的非字符串值（含 `{}`、`[]`、`0`、`False`）均加入 `legacy_invalid_fields=["error"]`，call 不进入 pending，后续 result 保持 unmatched。
- error 定点参数化 → `6 passed`；限定 persistence + projection suite → `54 passed`。仅有沙箱禁止写 `.pytest_cache` 的 warning。
- 固定 Luna Max 最终单项复审确认 falsey error 分支关闭，无新增 P0/P1，HC-104 可进入完成检查。
- 主会话最终复跑限定 persistence + projection suite → `54 passed`；`bun run typecheck`、`bun run project:check` 与 `git diff --check` 通过。
- `bun run test:py` → `600 passed / 1 skipped / 2 failed`：一个失败是沙箱禁止绑定 `127.0.0.1:0`，另一个是既有 stdio 子进程 2 秒时序波动；两项在沙箱外定点复跑 → `2 passed`。
- `bun run test:ts` → `111 passed / 1 skipped / 2 failed`；两项均为与本任务 Python 上下文投影无交集的既有 TUI 渲染/交互时序失败，本轮隔离复跑仍为 `14 passed / 2 failed`，未把它们误记为通过。

## 前置

- HC-101
