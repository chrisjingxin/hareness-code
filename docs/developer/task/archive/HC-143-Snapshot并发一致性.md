---
id: HC-143
title: 验证并加固 Snapshot Store 并发一致性
feature_area: Agent 文件读写可靠性
parent_task: -
decomposed_by: Codex
priority: P1
status: 已完成
owner: Codex
branch: codex/zc-138-140
reviewed_at: 2026-08-11
review_due: -
scope: 为 Host 共享的 ThreadSnapshotStore 建立可重现并发证据，验证多 Thread 并行 read_file、LRU/TTL 淘汰、路径失效和 Host/Thread 关闭时的记录、identity index 与字节预算一致性；若现实竞态可触发，以最小内部同步修复且不牺牲安全的并行读取。
acceptance: 测试可稳定覆盖同内容同时 record、不同 Thread/路径同时 record/resolve、淘汰与 resolve 竞态、invalidate/close 与在途读取；Store 不产生重复 identity、负数或错误 total_bytes、跨 Thread seen range 合并、已移除记录残留或崩溃；如需加锁，锁边界、顺序和 close 语义明确且不与 Host AsyncRWLock 死锁；有与当前生产 adispatch/to_thread 路径一致的回归证据。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-143-Snapshot并发一致性.md、docs/developer/architecture/架构总览.md
test_evidence: 旧实现 3 failed/7 passed；修复后 Store + contract 57 passed、组合 focused 83 passed；Agent full 1854 passed, 2 skipped, 1 个既有 stdio timeout；项目测试 9 passed；CLI 541 passed, 1 skipped, 1 个既有 Web takeover fail + 1 error；typecheck、project:check、git diff --check 通过；无 Protocol/用户文档/版本变更
references: docs/developer/task/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/spec/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/spec/HC-143-Snapshot并发一致性.md
completed_at: 2026-08-11
---

## 背景

`AgentHost` 为整个 Host 创建一个 `ThreadSnapshotStore`。文件工具的异步路径通过
`asyncio.to_thread` 执行，而只读工具可在 Host `AsyncRWLock` 下并行；Store 内部的
records、identity index 和 total bytes 是多步可变状态，当前没有专门并发证据。

这是需要先证明的竞态风险，不应在没有可运行回归前直接扩大锁粒度。

## 用户结果

```text
不同 Thread 并行读取/编辑
  → Snapshot Store 各自记录正确 scope 和 seen range
  → 淘汰/失效/关闭与在途调用线性化
  → 不泄漏跨 Thread 证明，不损坏资源计数
```

## 已确认设计

- barrier/Event 原型已稳定证明三类现实竞态：同 identity 重复记录、resolve 在 invalidate 后复活记录，
  以及 Host close 后在途 record 重新插入；具体证据和最终方案见同 ID Design。
- `ThreadSnapshotStore` 使用独立 `threading.Lock` 原子保护 records、identity index、total bytes 和
  closed；内容计算与 backend I/O 保持在锁外。
- TTL 是触发它的 record/resolve 原子操作的一部分，LRU 是新版本 record 原子操作的一部分；
  `has_seen` 只读取 immutable record，不续期或复活句柄。
- Store 锁不获取 Host `AsyncRWLock`，也不跨 diagnostics、CAS 或用户审批；不同 Thread 的 backend
  read 仍可在 `adispatch/to_thread` 中并行。

## 范围

- `ThreadSnapshotStore` 的 record/resolve/has_seen/invalidate/close/TTL/LRU 不变式与同步。
- Host 共享 Store、`SnapshotFileToolContract.adispatch` 和 `ConcurrencyGuardMiddleware` 的实际交互。
- 线程并发、多 Thread scope、资源上限、close 和取消回归测试。

## 非范围

- 不改变 Snapshot ID、工具 schema、TTL/LRU 产品语义或持久化边界。
- 不把所有文件读取改为 Host 全局串行。
- 不处理不遵循 Harness 锁的外部进程在 CAS 校验与最终 rename 之间的竞态。

## 验收清单

- [x] 并发测试覆盖同 identity、不同 scope、淘汰、invalidate 和 close 竞态。
- [x] 所有调度下 records/index/total_bytes 与 Thread/path/backend scope 保持一致。
- [x] 如需加锁，锁只保护 Store 内部多步状态，不覆盖 backend I/O、审批或 diagnostics。
- [x] focused 并发回归可稳定重复，Agent 全量测试和资源关闭用例不回归。

## 实施计划

1. **先固化确定性并发回归。** 在 `tests/threads/test_snapshots.py` 使用 Barrier/Event 和可注入
   时序覆盖同 identity、resolve/invalidate、TTL/LRU、path/Thread 清理与 Host close；每个交错结束
   都统一检查 records/index/total bytes invariant。旧实现必须稳定失败，而不是依赖压力循环。
2. **增加 Store 内部最小同步。** 在 `threads/snapshots.py` 用一把 `threading.Lock` 保护全部共享
   状态，把 hash/行信息/ID 候选计算留在锁外，并将 expire/evict/remove 收敛为已持锁 helper；不改变
   Snapshot scope、TTL/LRU、错误码或公开工具 schema。
3. **验证生产异步 seam。** 在 `tests/tools/test_snapshot_file_contract.py` 通过
   `SnapshotFileToolContract.adispatch()` 和 `asyncio.gather` 触发真实 `to_thread` 并行读取，证明
   同 identity 合并、跨 Thread 隔离且 backend read 没有被 Store 锁串行。
4. **同步长期说明并完成检查。** 更新架构总览的 Store 内部线性化边界，依次运行 focused tests、
   Agent 全量测试和项目级检查；记录没有用户文档、Protocol 或版本字段变化。

## 执行 Todo

- [x] 在 `packages/agent/tests/threads/test_snapshots.py` 增加可复现旧竞态的 Barrier/Event 测试和
      统一 invariant 断言；完成信号是旧实现稳定失败且没有 sleep/无界等待。
- [x] 在 `packages/agent/harness_agent/threads/snapshots.py` 增加 Store 状态锁、持锁 helper 和一致的
      属性读取；完成信号是所有并发单测通过且 backend I/O/内容预处理不在锁内。
- [x] 覆盖同 identity/different scope、resolve/invalidate、TTL/LRU、invalidate/close_thread/close
      两种线性顺序；完成信号是记录不复活、identity 唯一、字节计数精确、close 后保持空。
- [x] 在 `packages/agent/tests/tools/test_snapshot_file_contract.py` 增加真实 `adispatch/to_thread` 并发
      回归；完成信号是并行 backend read 可重叠而 Store 状态保持隔离一致。
- [x] 更新 `docs/developer/architecture/架构总览.md` 的 Snapshot Store 同步说明，并运行
      `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/threads/test_snapshots.py tests/tools/test_snapshot_file_contract.py`。
- [x] 运行 `cd packages/agent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`、
      `bun run test`、`bun run typecheck`、`bun run project:check`，把实际结果写回 `test_evidence` 和
      验收清单；确认不修改 Protocol、用户文档和版本字段。

## 实施记录

- 2026-08-11（Codex）：先在无锁实现运行 focused 回归，10 个用例中 3 个按预期稳定失败，分别证明
  同 identity 重复记录、resolve/invalidate 后记录复活，以及 close 后在途 record 重新插入。
- 2026-08-11（Codex）：增加 Store 独立状态锁并完成内部 helper 线性化；同 identity 并发用例在同一
  进程重复 20 次。`test_snapshots.py` 为 29 passed；连同现有 Snapshot file contract 测试为
  54 passed；`snapshots.py` py_compile 与相关 `git diff --check` 通过。
- 2026-08-11（Codex）：补充真实 `adispatch → asyncio.to_thread` 四路并行读取，强制 backend
  I/O 重叠并验证同 Thread 合并、跨 Thread 隔离；另把 TTL 与容量 LRU 分开，增加 LRU/resolve
  受控竞态。最终 Store + contract 57 passed，组合 focused 83 passed；项目级检查仅保留与本任务
  无关的既有 stdio/Web 测试失败。

## 定期复核记录

- 2026-08-11（Codex）：从 HC-131 共享 Snapshot Store 的生产路径拆出独立并发一致性结果；本卡先要求可运行证据，不预设必须加锁，下次复核 2026-08-25。
- 2026-08-11（Codex）：完成同 ID Design。可注入时序已证明现有 Store 会产生重复 identity、失效后
  记录复活和 close 后重新插入；确认采用 Store 内部短临界区修复，不扩大 Host 工具锁。
