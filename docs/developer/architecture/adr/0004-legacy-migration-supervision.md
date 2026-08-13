# ADR 0004：Legacy migration 的 durable supervision 协议

日期：2026-08-11
状态：已接受

关联任务：[HC-108](../../task/HC-108-收敛迁移子进程强制清理与残留文.md)
关联架构：[架构总览](../架构总览.md)

## 背景

旧 schema 迁移运行在可终止的 Python child 中。只依靠 child 的 `finally` 和 parent 的
`terminate/kill` 返回值，无法覆盖 parent 崩溃、信号失败、无法取得 returncode、child 在
SQLite 临时文件写入中被杀等窗口。parent 如果继续恢复或打开主库，未知 child 仍可能晚到；
如果按文件名或时间清理残留，又可能删除被替换的外来文件。

## 决策

### Parent 独占 attempt manifest

每次 legacy migration 在 spawn 前写入
`threads.sqlite3.migration-attempt.json`。`preparing`、`prepared` 和 `exit_unknown` 都是 durable
active guard；fresh opener 取得 migration lock 后、任何 SQLite 访问前必须拒绝。只有 parent
写主 manifest，child 通过私有 attempt 目录中的一次性 `child-ready.json` 报到。

```text
完整 source fingerprint
→ durable preparing
→ 创建并登记 attempt 目录、backup.tmp、restore.tmp 的文件身份
→ durable prepared
→ spawn child
→ child-ready durable 后才允许 child 连接 SQLite
```

child 只接受 `prepared`。parent 已转为 `exit_unknown` 时，晚启动 child 必须停在 SQLite 外。
新 migration state 使用 version 2，并把 `attempt_id`、完整 source fingerprint 和固定 backup
basename 绑定到同一 attempt；缺少 matching attempt 的 v2 state 不按旧 v1 恢复。

### 退出证明只来自当前 Popen owner

`poll`、`wait`、`terminate`、`kill` 和最终 reap 分别捕获平台进程错误。timeout 后成功 kill 并取得
returncode 仍属于 `exited_reaped`；只有无法取得可靠 returncode 才是 `exit_unknown`。
`ProcessLookupError` 只说明信号目标不存在，不能替代本 `Popen` 的 wait/reap 证明。

当前 parent 只有在持有内存 `_MigrationReapAuthority` 时才能在 active guard 下按数据库事实收敛。
fresh process 不从 PID、process birth identity、主库已经是 final 或磁盘 JSON 伪造这份 authority。
`exit_unknown` 必须在释放 migration lock 前依次发布进程内 poison、尝试把 manifest 改为
`exit_unknown`、再尽力写 durable poison；在线路径不 restore、不清 temp、不正常 open。

### 精确身份清理和离线恢复

每个 attempt 只处理固定登记角色及其固定 staging。parent 仅删除类型、owner/mode 和登记文件
身份匹配的普通文件；不同 identity 归类为 `foreign_replacement` 并保留。主库、verified backup、
WAL、SHM、journal 和未登记文件永不进入临时清理集合。

child 已 reaped 时在当前 owner 内立即收敛。`exit_unknown` 没有在线墙钟保留上界；每个 attempt
的受控文件数量固定有界，用户确认所有 Harness worker 已停止后，通过
`python -m harness_agent.threads.migration_recovery` 在同一 migration lock 下离线收敛。命令默认
只读，必须显式传入 `--confirm-all-harness-workers-stopped` 才写入；不扫描或终止 PID。

### 威胁模型

协议覆盖意外崩溃、静态 symlink/路径替换、错误 owner/mode、损坏 marker 和普通并发 opener。
它不宣称抵御同一 UID 的活跃恶意进程在 child 运行期间持续 rename/replace 路径；Python SQLite
会按路径重新打开文件，仅靠 `O_EXCL`、`O_NOFOLLOW` 和 `lstat` 不能完全消除该竞态。发现身份变化
时仍失败关闭，但这不是同 UID 对抗安全边界。

POSIX 文件身份使用 `st_dev + st_ino`。Windows 必须以稳定 FileId adapter 和真实 runner 证明
identity cleanup 后才能宣告该平台完成 ZC-108 验收；在此之前需要 legacy migration 的数据库
返回 `CHECKPOINT_MIGRATION_FILE_IDENTITY_UNSUPPORTED` 并失败关闭，属于明确的平台阻塞项。

## 后果

- active attempt 存在时，数据库即使看似 final，fresh opener 也不会越过未知 child。
- child 不再清 migration state；parent 只在 reaped、数据库事实和登记文件全部收敛后封口。
- 新 marker 存在时不能直接回退到不识别协议的旧代码，必须先用当前版本离线收敛。
- `exit_unknown` 优先保证数据安全，不承诺自动或定时删除。

## 备选方案

- 根据 PID 不存在自动解除 guard：拒绝，PID 可复用且不能替代当前 owner 的 reap 证明。
- 按 glob、mtime 或文件内容清理 `.tmp`：拒绝，无法证明文件归属且会扩大删除集合。
- child 与 parent 共同更新主 manifest：拒绝，两个 `replace` 会覆盖彼此的状态转换。
