# HC-143：Snapshot Store 并发一致性设计

> 原始需求：[HC-143](../task/archive/HC-143-Snapshot并发一致性.md)

## 通俗问题说明

一个 Host 只创建一个 `ThreadSnapshotStore`，但不同 Thread 的 `read_file` 可以并行执行。
异步文件工具会把同步处理送入 `asyncio.to_thread`，所以多个真实 OS 线程会同时更新
Snapshot 记录、identity 索引和总字节计数。

修复前 Store 的一次操作通常包含“查索引、写记录、更新索引、调整计数、执行淘汰”等多个步骤，
这些步骤没有共同的同步边界。CPython 的 GIL 不能把整组步骤变成一个原子操作；线程恰好在步骤
之间切换时，会出现重复 identity、已经失效的记录复活，甚至 Store 关闭后重新插入记录。

本设计采用以下主线：

```text
backend 并行读取并计算文件 identity
  → 进入 ThreadSnapshotStore 的短临界区
  → 原子完成 scope/TTL/identity/记录/索引/字节预算/LRU 更新
  → 返回 immutable SnapshotRecord
  → 退出 Store 临界区后继续审批、CAS 和 diagnostics
```

修复只同步 Store 的进程内元数据，不扩大 Host 的工具锁，也不把不同 Thread 的文件读取串行化。

## 已确认的现实竞态

2026-08-11 在未修改生产代码的工作树上，用 `threading.Barrier` / `Event` 注入确定时序，已经稳定
复现三类问题：

1. 两个线程同时记录同一 `thread + path + backend + content hash`。两者都在 identity 查询为空
   后继续创建，最终得到两个 Snapshot ID；`records` 有 2 条、identity index 只有 1 条，字节预算
   计算了两次。
2. `resolve` 取得旧记录后暂停，另一线程执行 `invalidate_path`，随后 `resolve` 继续把 touch 后的
   记录写回。最终记录被复活，但 identity index 已删除、`total_bytes` 为 0。
3. `record_read` 通过 `_ensure_open` 后暂停，另一线程执行 `close`，随后读取线程继续插入。
   最终 `closed=True`，但 Store 仍包含记录和非零字节。

原型观测值如下：

```text
same-identity-record: size=2, total_bytes=12, identity_index=1
resolve-invalidate:   size=1, total_bytes=0,  identity_index=0
record-close:         closed=True, size=1, total_bytes=3
```

因此本任务不再把并发损坏视为只需防御的假设；实现必须加入 Store 内部同步，并把上述时序转为
永久回归测试。

## 实施前已确认现状

- `AgentHost` 为整个 Host 创建一个 `ThreadSnapshotStore`，Host 关闭时调用 `store.close()`。
- `SnapshotFileToolContract.adispatch()` 使用 `asyncio.to_thread(self._dispatch, request)`；并行
  `read_file` 会在不同工作线程中调用同一个 Store。
- `ConcurrencyGuardMiddleware` 的 Host `AsyncRWLock` 允许只读工具共享读锁；因此它有意允许多个
  `read_file` 并发，不能承担 Store 内部互斥。
- edit/delete 等写工具通常在 Host 写锁下运行，但 Store 还会被并行 read、淘汰和 Host 生命周期
  操作访问；Store 不能把调用方锁当作自己的 contract。
- `SnapshotRecord` 已是 frozen dataclass，可以在临界区外安全读取；可变共享状态只有
  `_records`、`_identity_index`、`_total_bytes` 和 `_closed`。
- TTL 没有后台清扫线程，只在 `record_read` 和 `resolve` 入口触发；LRU/容量淘汰只在新版本记录时
  触发。
- `close_thread` 当前表示一次性清除该 Thread 已保留的 Snapshot，不是永久禁止该 Thread 后续
  再读；当前生产 Host 只在整体关闭时调用永久性的 `close`。

## 目标与非目标

### 目标

- 对 Store 的每个公开状态操作定义唯一线性化点。
- 任意时刻保持 records、identity index 与总字节预算一致。
- 同一 identity 的并发读取只保留一条记录，并合并同一 Thread 的 `seen_lines`。
- TTL、LRU、路径失效、Thread 清理和 Host close 不会与在途调用产生复活或部分删除。
- 保持 backend I/O、内容 hash/行信息计算和不同 Thread 的读取并行。
- 用真实 `adispatch → asyncio.to_thread` 路径证明修复覆盖生产调度方式。

### 非目标

- 不改变 Snapshot ID、TTL、LRU、容量、scope 或错误码的产品语义。
- 不把 Snapshot 写入 SQLite、Transcript 或工作区。
- 不把 Host `AsyncRWLock` 改成全局串行锁，也不新增 path lock。
- 不让内部锁跨 backend I/O、HITL 审批、CAS 提交或 diagnostics。
- 不处理其他进程或 IDE 在本机 CAS 检查与最终 rename 之间的竞态。

## 状态 invariant

Store 打开和关闭状态都必须满足：

1. 每个 `_records[snapshot_id]` 的 `snapshot_id` 与 key 相同。
2. 每个 retained record 都有且只有一个 `_identity_index[record.key] == snapshot_id`。
3. 每个 identity index 项都指向存在且 key 完全匹配的 record；不存在悬空或错指索引。
4. 同一 `SnapshotRecord.key` 最多保留一条 record。
5. `_total_bytes == sum(record.byte_length for record in _records.values())`，且永不为负数。
6. `seen_lines` 只在完全相同的 Thread/path/backend/content identity 内合并。
7. 每次公开操作返回或抛错时，TTL、path version、总数量和总字节限制已经收敛。
8. `_closed=True` 时 records/index 为空且 total bytes 为 0；之后所有创建、解析和失效入口都拒绝，
   重复 `close()` 仍幂等。

测试可以读取私有容器来断言上述内部 invariant，但生产接口不新增暴露源码路径或 Snapshot 句柄的
诊断方法。

## 同步决策

### Store 内部状态锁

`ThreadSnapshotStore` 增加一把 `threading.Lock`，唯一保护以下状态：

```text
_records
_identity_index
_total_bytes
_closed
```

所有读取或修改这些字段的公开入口和属性都经过该锁。`_expire`、`_evict`、`_remove` 改成只允许
在已持锁状态下调用的私有 helper；它们不自行再次获取锁，因此不需要可重入锁，也不会形成隐式
锁顺序。

锁内只允许：dict/list 操作、immutable record replacement、区间合并、时钟读取和确定性淘汰。
以下工作必须在锁外完成：

- backend 文件读取及 raw bytes 获取；
- 内容 hash、行数、换行和已读区间等候选元数据计算；
- HITL/Policy 等待；
- mutation prepare/commit、CAS、提交后重读；
- LSP diagnostics、日志和 metrics。

Snapshot ID 候选在锁外生成。进入锁后必须重新检查 `closed` 和 identity；若 identity 已由另一线程
创建，则丢弃候选并合并已有记录。极低概率的 ID 碰撞不能覆盖旧记录，应退出临界区生成新候选后
重试。

### 为什么不用 Host AsyncRWLock

Host 锁保护的是工作区工具语义：多个只读工具可以并行，写工具独占。Snapshot Store 锁保护的是
一个工具内部的短期内存索引事务。两者职责和运行层不同：

```text
event loop: ConcurrencyGuard 获取 Host read/write permit
  → worker thread: backend I/O 与文件工具同步实现
    → worker thread: 短暂获取 Snapshot Store threading.Lock
```

Store 从不尝试获取 Host `AsyncRWLock`，Host 锁也不在线程锁持有期间发生 await，所以不存在反向锁
顺序。`adispatch` 仍可让多个 backend read 在线程池并行；只有最终登记元数据的短临界区串行。

## 操作与线性化点

| 操作 | 线性化点 | 并发语义 |
| --- | --- | --- |
| `record_read` | 持锁后，复用记录的 replace，或新记录+索引+计数+淘汰全部完成的时刻 | 同 identity 后到者合并 `seen_lines`；close 先完成则返回 `SNAPSHOT_STORE_CLOSED` |
| `resolve` | 持锁完成 TTL、scope 校验并写回 `last_used_at` 的时刻 | invalidate/close 先完成则 expired/closed；resolve 先完成可返回 immutable record，之后的失效不复活它 |
| `has_seen` | 读取传入 immutable `SnapshotRecord.seen_lines` 的时刻 | 不访问 Store 状态、不续期、不复活句柄；句柄存活性已经由之前的 `record_read/resolve` 决定 |
| `invalidate_path` | 持锁删除全部匹配记录、索引并调整计数完成的时刻 | 与重叠的 record 按锁顺序解释；它是一次性失效，不阻止之后的新读取 |
| `close_thread` | 持锁删除该 Thread 当前全部记录完成的时刻 | 与 record 按锁顺序解释；不建立永久 thread tombstone |
| `close` | 持锁清空状态并设置 `_closed=True` 的时刻 | 永久拒绝随后线性化的 record/resolve/invalidate/close_thread；重复 close 为 no-op |
| TTL | 触发它的 `record_read/resolve` 线性化点的一部分 | 无后台清扫；过期记录的删除与当前操作原子可见 |
| path/bytes/count LRU | 新版本 `record_read` 线性化点的一部分 | 插入、所有淘汰和最终预算状态一次性可见 |
| `size/total_bytes/closed` | 各属性持锁取得值的时刻 | 单个属性值是强一致读取；不新增跨三个属性的公开事务快照 |

`has_seen` 使用已经返回的 immutable record 是有意设计：invalidate 不需要撤回其他线程已经取得的
Python 对象；mutation 仍会用 backend 当前 identity/CAS 防止 stale 写入。生命周期调用方仍必须按
既有 Host 顺序先停止/收敛 Run，再关闭 Store。

## 错误与兼容语义

- 不新增工具、Protocol 字段或公开错误码。
- `close` 在线性化点之后，`record_read`、`resolve`、`invalidate_path`、`close_thread` 继续返回既有
  `SNAPSHOT_STORE_CLOSED`。
- `resolve` 与 TTL/LRU/invalidate 竞争失败时继续返回 `SNAPSHOT_EXPIRED`；scope 不匹配继续返回
  `SNAPSHOT_SCOPE_MISMATCH`。
- 并发同 identity 的调用可以返回不同时间点的 immutable record 值，但 Snapshot ID 必须相同；
  随后的 resolve 必须看到已经合并的最新 `seen_lines`。
- 单个新版本本身超过 `max_total_bytes` 时沿用现有语义：本次可返回 record，但 Store 在同一个
  线性化点将其淘汰，后续 resolve 返回 expired。

## 确定性回归设计

测试不能依赖高次数循环“碰运气”，也不能把 GIL 当作测试前提。永久回归使用
`threading.Barrier` / `Event` 和可注入 `clock`、可 monkeypatch 的 ID/replace seam 控制时序：

1. **同 identity record**：在两个线程生成 Snapshot ID 候选时用 barrier 同步，使旧实现必然都
   越过 identity miss；修复后断言只保留一个 ID、seen range 合并、字节只计一次。
2. **不同 scope record/resolve**：不同 Thread、path、backend 并发登记和解析，断言 ID 和 seen
   range 不串用，全部内部 invariant 成立。
3. **resolve 与 invalidate**：在 resolve 取得记录、写回 touch 之前暂停，同时启动 invalidate；
   释放后断言最终状态只能对应某个完整线性顺序，不存在 records/index/bytes 不一致或复活。
4. **record 与 Host close**：在 ID 候选生成处暂停 record，让 `close` 先完成；恢复后必须抛出
   `SNAPSHOT_STORE_CLOSED`，Store 保持空且 closed。反向顺序则 record 完成后由 close 完整清空。
5. **TTL/LRU 与 resolve**：用 fake monotonic clock 和 barrier 让过期/容量淘汰与解析竞争，结果可以
   是有效 resolve 或 `SNAPSHOT_EXPIRED`，但每次结束都满足 invariant。
6. **Thread/path 清理**：`close_thread`、`invalidate_path` 与其他 scope 的 record 并发，清理不能误删
   其他 Thread/backend，也不能留下悬空 index。
7. **生产调度 seam**：在 `SnapshotFileToolContract.adispatch()` 上用 `asyncio.gather` 并行读取，
   确认调用确实经 `to_thread` 落到共享 Store，并验证同 identity 合并及跨 Thread 隔离。
8. **可重复性**：focused 并发测试至少在同一进程重复运行 20 次，不使用 sleep 作为正确性条件；
   所有 wait/join 必须有短超时，失败时不会挂死测试进程。

回归中的时序注入只存在于测试；生产类不新增可被业务调用的调度 hook。

## 可观察验收

- 两个并行 `read_file` 读取同一内容后只产生一个 Snapshot ID；随后 resolve 能看到合并范围。
- 不同 Thread/path/backend 的并行读取从不合并 identity 或 `seen_lines`。
- resolve、TTL/LRU、invalidate 和 close 的所有受控交错后，records/index/bytes 始终一致。
- Host close 一旦先线性化，任何在途 record 都不能重新写入关闭后的 Store。
- 不同 Thread 的 backend 读取仍能同时进行；只有 Store 元数据提交短暂串行。
- focused 并发回归、Snapshot 文件 contract 测试和 Agent 全量测试通过，且没有线程泄漏或超时。

## 实施边界与回滚

预计生产改动只涉及 `packages/agent/harness_agent/threads/snapshots.py`；并发单测放在
`packages/agent/tests/threads/test_snapshots.py`，真实 `adispatch/to_thread` 证据放在
`packages/agent/tests/tools/test_snapshot_file_contract.py`。实现完成后同步架构总览中 Snapshot Store
的内部同步说明。

若内部锁造成 backend read 串行或出现 Host 锁死，说明锁边界违反本设计，应回退实现并保留先行
并发回归；不得通过删除回归、扩大 Host 写锁或依赖 GIL 规避问题。
