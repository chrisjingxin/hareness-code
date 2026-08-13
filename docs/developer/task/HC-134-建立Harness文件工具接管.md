---
id: HC-134
title: 建立 Harness 文件工具接管边界与 Thread Snapshot 基础设施
feature_area: Agent 文件读写可靠性
parent_task: -
decomposed_by: Codex
priority: P0
status: 已过时
owner: Codex (Luna Max)
branch: codex/zc-134-file-tools-interposition
reviewed_at: 2026-08-10
review_due: -
scope: 在不 fork 第二套 Agent runtime 的前提下，为 DeepAgents 0.6.8 建立 Harness-owned 文件工具 interposition seam，使模型看到的 canonical schema 与实际执行都由 Harness 接管；同时实现按 Thread/路径/backend 隔离、有界、记录已读行的 SnapshotStore，以及本地/远端 text mutation backend adapter contract。
acceptance: 主 Agent 与所有可写受控子 Agent 的 read/write/edit/delete 都经过同一 Harness seam；模型请求中每个文件工具只有一个 schema，DeepAgents builtin handler 不会处理被接管调用；Snapshot 不能跨 Thread/路径/backend 复用，重复同内容读取合并 seen ranges，LRU/TTL/字节预算淘汰返回 expired；虚拟 /.harness/ 不生成可写 Snapshot；local/remote adapter 能力与 CAS 不支持错误有契约测试；未提前暴露 HC-133 尚未确认的生产 schema。
user_docs: -
developer_docs: docs/developer/spec/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/architecture/架构总览.md
test_evidence: focused: packages/agent/.venv/bin/python -m pytest -q packages/agent/tests/threads/test_snapshots.py packages/agent/tests/threads/test_text_backend.py packages/agent/tests/runtime/test_file_tools.py -> 11 passed, 1 warning; existing file/policy/approval/deferred focused -> 123 passed, 1 warning; Agent full in sandbox -> 1725 passed, 3 skipped, 9 environment failures (websocket loopback bind permission), same 9 tests with local loopback permission -> 9 passed, 1 warning; bun run test:project -> 9 passed; bun run typecheck -> passed; bun run project:check -> passed; bun run test -> TypeScript 534 passed, 1 skipped, 6 existing environment failures (5 EADDRINUSE loopback ports, 1 EISDIR Bun bundle path), test:py separately recorded above; compileall and git diff --check -> passed; no version change.
references: docs/developer/task/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/task/HC-133-建立企业弱模型文件编辑评测与s.md
completed_at: -
---

> 2026-08-10 流程复核：本文件是同一功能内部的历史基础设施步骤，不再作为独立 Task。
> 仍有效的范围、Todo 与证据已收回 [HC-131](HC-131-统筹弱模型优先的文件读写可靠性.md)；以下内容仅保留历史追溯。

## 服务的用户结果

本任务建立后续唯一实现边界：即使 DeepAgents 自动注入同名文件工具，模型 schema 和工具调用
也只能进入 Harness 的验证路径，不能偶尔走 builtin、偶尔走 Snapshot service。

## 当前问题

- `create_deep_agent()` 把 `FilesystemMiddleware` 视为必需脚手架，不能简单排除。
- 只向 `tools=` 再添加同名工具会产生 schema/ToolNode 歧义。
- Harness 共享 AgentEngine 可服务不同 Thread；进程级按路径缓存会跨 Thread 泄漏。
- 本机 `LocalShellBackend` 和远端 sandbox 能力不同，不能让 service 直接依赖 `Path`。

## 实施步骤

1. 在构图边界实现 `HarnessFileToolsMiddleware`（或同职责深模块）：模型调用前替换 builtin
   文件工具定义，工具执行前按名称接管并禁止落到 builtin handler。
2. 用架构测试固定：每个模型请求只出现一份 canonical schema；主 Agent、默认子 Agent、
   managed/inline 子 Agent 的可写文件调用都经过同一 seam。
3. 将最终 schema 作为 HC-133 决策的注入参数；本任务先提供 contract/fixture，不提前硬编码
   未确认的 edits/insert 形状。
4. 实现 `ThreadSnapshotStore`：key 包含 Thread、不可猜测 ID、canonical path、backend
   identity；记录强 hash、text metadata、seen line intervals、创建/访问时间。
5. 为相同 Thread/路径/内容的重复读取复用记录并合并 seen intervals；内容变化产生新 ID；
   加入 path、version、总字节、TTL 上限和确定的淘汰语义。
6. 让 store 从 `RunContext` 取得 Thread，禁止共享图闭包捕获某个构图期 Thread；Thread
   close/Host close 时释放对应资源。
7. 定义 `TextMutationBackend` adapter：full text read、create-if-absent、compare-and-replace、
   delete-if-unchanged 和 post-write read；为 local、remote 与 unsupported fake 编写契约测试。
8. 保持 `/.harness/` CompositeBackend route 可读，但不给该 route 生成可写 Snapshot；原有
   Skill/history 隔离测试继续通过。

## 明确不修改

- 不切换生产模型 schema；由 HC-135 在 HC-133 决策后完成。
- 不实现 diff/approval UI、formatter 或 diagnostics。
- 不把 Snapshot 写进 Transcript、SQLite 或工作区。
- 不为不支持 CAS 的 remote backend 增加盲写 fallback。

## 验收清单

- [x] builtin 与 Harness 不会形成重复可见 schema 或双执行路径。
- [x] 主/子 Agent 接管行为一致，Capability 和 Deferred Tool 仍可过滤。
- [x] Thread/路径/backend/snapshot 作用域错误全部 fail closed。
- [x] seen ranges 合并、资源淘汰、Host close 和虚拟只读 route 有回归测试。
- [x] local/remote/unsupported adapter contract 明确。
- [x] Python focused tests 和 Agent runtime tests 通过；受限 sandbox 的既有 loopback 权限失败已在允许本机 loopback 后逐项通过。

## 当前实施证据

- 2026-08-10：已按 `bun run task:claim -- HC-134 --owner "Codex (Luna Max)" --branch "codex/zc-134-file-tools-interposition"` 认领；工作树原有 HC-132 设计、任务和生产改动保持不动。
- 2026-08-10：已实现 `HarnessFileToolsMiddleware`、注入式 exact-string contract、`ThreadSnapshotStore`、`TextMutationBackend` 及 local/remote/unsupported adapter；主图、declarative 子图和受控 inline 图均接入同一 contract 配置。
- 2026-08-10：已通过 `packages/agent/.venv/bin/python -m pytest -q packages/agent/tests/threads/test_snapshots.py packages/agent/tests/threads/test_text_backend.py packages/agent/tests/runtime/test_file_tools.py`（11 passed）以及既有文件边界/审批/deferred focused tests（123 passed）。
- 2026-08-10：Agent 全量可执行测试在受限 sandbox 中为 1725 passed、3 skipped，剩余 9 项均为既有 `websockets.serve` 绑定 `127.0.0.1:0` 的环境权限失败；放开本机 loopback 权限重跑这 9 项为 9 passed。`bun run typecheck`、`bun run project:check` 和 `bun run test:project` 通过。
- 2026-08-10：`bun run test` 的 TypeScript 端为 534 passed、1 skipped、6 项既有环境失败（5 个 loopback `EADDRINUSE`、1 个 Bun bundle `EISDIR`）；根脚本在 CLI 测试后停止，Python 全量已单独执行并记录上述证据。当前不提交、不推送，也不修改版本。

## 定期复核记录

- 2026-08-10（Codex）：从 HC-131 拆解；下一次复核 2026-08-24，重点确认 DeepAgents
  版本是否提供了更稳定的 filesystem middleware 注入 seam。若依赖升级已提供正式能力，应
  直接使用并删除临时 interposition，不保留两层实现。
