---
id: HC-112
title: Skill 安全文件层与 AGENTS 参考读取支持 Windows
priority: P0
status: 进行中
owner: Trae
branch: master
scope: 让 packages/agent 的 Skill 安全文件原语和 AGENTS.md 稳定参考读取在 Windows 上可用：POSIX 保留 fd 锚定实现，Windows 使用逐层拒绝 symlink 的路径锚定实现，对外保持同一组原语签名和 fail-closed 语义。
acceptance: Windows 上 `bun run dev` 能完成 sidecar 启动与 initialize 握手；Skill 扫描、安装、恢复与快照在两个平台行为一致；symlink 与路径逃逸在两个平台都被拒绝；Python 全量测试通过。
user_docs: docs/user/故障排查.md
developer_docs: 不涉及
test_evidence: -
references: -
completed_at: -
---

## 背景

用户在 Windows 上运行 `bun run dev` 时，CLI 通过 stdio JSON-RPC 启动 Python
sidecar（`python -m harness_agent`）。sidecar 在 `AgentHost.__init__` 里构造
`SkillCatalogManager`，后者调用 `harness_agent/extensions/skills.py` 的安全
文件层恢复 Skill 安装 journal。该文件层完全依赖 POSIX 能力：

- `os.O_NOFOLLOW` / `os.O_DIRECTORY`：Windows Python 没有这两个常量；
- `os.open("/")` 与 `dir_fd=` 系列操作（stat/open/unlink/rmdir/replace/scandir）：
  Windows 不支持对目录 `os.open`，也不支持这些函数的 `dir_fd` 参数；
- 目录 fsync：Windows 没有等价能力。

`threads/context_lifecycle.py` 的 `_read_stable_reference`（读取 AGENTS.md）
同样只走 `O_NOFOLLOW + dir_fd` 路径，Windows 上直接抛
`CONTEXT_REFERENCE_NOFOLLOW_UNAVAILABLE`。

## 当前存在的问题

1. Windows 启动即崩溃：`SkillCatalogManager.__init__` 抛
   `SkillError("secure directory opening is unavailable")`，sidecar 立刻退出，
   CLI 只能看到无意义的 `za38: Agent transport closed`。
2. 即使绕过启动，`run/start` 构造上下文时读取 AGENTS.md 也会失败，
   Windows 上任何 Run 都无法开始。
3. CLI 在 transport 关闭时吞掉了 sidecar stderr，真实报错不可见，
   排障成本高（本条不在本任务修复，见非范围）。

## 为什么现在要修改

仓库开发环境包含 Windows 用户；当前代码使 sidecar 在 Windows 上完全不可用，
阻塞所有后续功能验证。这不是新功能，而是现有安全文件层缺失的平台分支。

## 目标设计

保持单一调用路径：所有调用方仍使用同一组模块级原语
（`_open_directory_path`、`_open_directory_at`、`_stat_entry_at`、
`_list_directory_names`、`_read_file_at`、`_replace_path`、`_remove_tree_at`、
`_ensure_directory_path`、`_fsync_directory` 等），签名不变。

引入 `_DirectoryHandle` 作为"已锚定受信目录"的句柄：

```text
输入：绝对路径或父句柄 + 名称
→ POSIX：保持现有 fd 锚定实现（句柄内部持有真实 fd）
→ Windows：逐级 lstat 校验"非 symlink 且为目录"后持有固定路径
→ 输出：句柄 + stat 身份；后续读取/枚举/rename/删除都从句柄出发
→ 关闭：句柄 close()；POSIX 释放 fd，Windows 无资源可释放
```

Windows 分支的安全语义降级必须显式记录：

- 每一级目录和最终条目都用 `os.stat(follow_symlinks=False)` 拒绝 symlink；
- 文件读取前后比较 lstat/fstat 身份，检测读取期间被替换；
- "stat 与 open 之间目录被换成 symlink"的竞态窗口在 Windows 无法用 fd
  消除，接受该降级并在注释中说明；
- 目录 fsync 在 Windows 上是 no-op（NTFS 元数据由日志保证）。

`context_lifecycle._read_stable_reference` 增加同构的路径分支：
无 `O_NOFOLLOW` 时用 lstat 拒绝 symlink、拒绝非常规文件，打开后比较
读取前后身份，复用同一套错误码与截断/脱敏收尾。

## 实施步骤

1. `skills.py`：新增 `_DirectoryHandle` 与 `_IS_WINDOWS` 平台分支，把上述
   原语的调用方从裸 fd（`os.close(fd)`、`os.fstat(fd)`）迁移到句柄方法；
   POSIX 分支逻辑保持逐行不变。
2. `context_lifecycle.py`：`_read_stable_reference` 在无 `O_NOFOLLOW` 的
   平台改走路径锚定分支，共享收尾校验。
3. 运行 sidecar 直接启动 + initialize 握手验证，再跑全量 Python 测试。

## 范围

- `packages/agent/harness_agent/extensions/skills.py`
- `packages/agent/harness_agent/threads/context_lifecycle.py`
- 对应测试与 `docs/user/故障排查.md`

## 非范围

- CLI 吞掉 sidecar stderr 的错误展示问题（单独任务）。
- `config_change_service.py` 的 fcntl 文件锁与 `thread_persistence.py`
  迁移路径的 `os.fchmod`/目录 fsync：仅在配置提交与数据迁移时触发，
  不阻塞启动与 Run，留待后续任务。
- 不承诺 Windows 上的 symlink 竞态防护与 POSIX 等强。

## 验收清单

- [ ] Windows 上 sidecar 可直接启动并完成 initialize 握手。
- [ ] Windows 上 Skill 扫描、启停状态、快照校验与安装恢复不抛平台异常。
- [ ] symlink 与 `..` 穿越在两个平台都被拒绝；既有 failpoint 测试通过。
- [ ] POSIX 分支行为无回归：Python 全量测试通过。
- [ ] 用户文档说明 Windows 支持状态。
