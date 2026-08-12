---
id: HC-103
title: 让 Skill Catalog 在下一顶层 Run 热更新
priority: P0
status: 已完成
owner: Codex (Luna Max)
branch: master
scope: 将进程级永久 SkillRegistry 改为 Host 管理的可刷新 Catalog snapshot，并保证显式 Skill、Prompt 索引、虚拟文件和 AgentEngineProfile 在一次 Run 内引用同一快照。
acceptance: 新增、修改、删除、启停或更新 Skill 后，同一 Thread 的下一顶层 Run 使用新快照且无需重启；活动 Run 继续使用旧快照；能力变化只定向排空受影响 AgentEngine。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md
test_evidence: 固定窄评审：全部 P1 已关闭，无新增 P0/P1；最终直接评审测试 25 passed。独立相关回归：119 passed；安全能力隔离补充回归：60 passed。项目检查：typecheck passed；project:check passed；Python full 690 passed/1 skipped，唯一 sandbox WebSocket bind failure 在沙箱外单测 1 passed；TS full 112 passed/1 skipped，2 个既有 TUI renderer/SearchPicker 失败；git diff --check passed。
references: docs/developer/architecture/上下文管理改造需求.md、docs/developer/architecture/上下文管理顶层设计.md、docs/developer/task/HC-095-分离共享资源与AgentEng.md
completed_at: 2026-08-03
---

## 背景

HC-103 前的 `AgentHost` 持有一个进程级 `SkillRegistry`。Registry 在初始化时扫描 Skill，`load()` 发现 manifest 摘要变化后直接提示重启；Skill 启停、安装和删除结果也声明在 `next_thread` 生效。本任务将该段行为替换为 Host 管理、顶层 Run 刷新的 Catalog snapshot。

另一方面，Skill catalog fingerprint 已进入 `ResolvedAgentSpec` 和 `AgentEngineProfile`，而 [HC-095](HC-095-分离共享资源与AgentEng.md) 负责建立 snapshot 变化到受影响 AgentEngine 定向 draining 的资源链路。缺少的是“每次顶层 Run 取得哪一个 Skill snapshot”的一致准备过程。

本任务对应 `REQ-CTX-003`，依赖 HC-102 的 RunContextSnapshot 生效边界。

## 当前存在的问题

### 1. Skill 修改要求重启整个 Host

`SkillRegistry.load()` 会比较启动时 digest 和当前文件；不一致时拒绝加载。用户无法在当前 Thread 的下一次 Run 使用刚修改的 Skill。

### 2. requested Skill 在统一 Run 准备前解析

HC-103 前 `RunCoordinator.start()` 先通过独立 `skill_registry_provider` 解析 requested Skill，再调用 `PreparationProvider` 构造 Profile。若刷新发生在两步之间，显式 Skill 与 Prompt/Profile 可能来自不同快照；当前实现已将 requested Skill 预加载移入统一 `RunPreparation`。

### 3. 虚拟文件捕获构图时 Registry

`run_scoped_virtual_backend_factory()` 虽然按 RunContext 限制 Thread 历史，但仍闭包捕获 AgentEngine 构建时的 `SkillRegistry`。未来同一共享图不能安全读取本 Run 对应的 Skill snapshot。

### 4. mutation RPC 的生效边界不符合目标

HC-103 前启停、安装、更新和删除 Skill 只写磁盘或 state，当前 Registry 不更新，返回的 `effective_on=next_thread` 也与“当前 Thread 下一顶层 Run”不一致；当前 mutation manager 返回 `effective_on=next_run` 并在下一次刷新发布快照。

## 为什么现在要修改

- HC-102 已把系统上下文生效边界改为顶层 Run，Skill 必须与 AGENTS 使用同一边界。
- HC-095 提供定向失效后，不再需要通过重启 Host 避免旧图继续运行。
- requested Skill、系统提示和虚拟读取若不共享一个快照，会造成审计错误，严重时可能把未启用 Skill 暴露给模型。

## 目标设计

Host 持有 `SkillCatalogManager`，而不是一份永久 Registry：

```text
顶层 Run 准备
→ 扫描受支持的 Skill 来源
→ 校验 manifest、路径和状态
→ 内容相同：复用 immutable SkillRegistry
→ 内容变化：创建新 snapshot
→ 从该 snapshot 解析 requested Skill
→ 用同一 snapshot 生成 Prompt 索引、Profile 和虚拟挂载
→ RunContext 固定 snapshot
```

一致性不变量：

```text
requested Skill
= System Instruction 中的 Skill index
= /.harness/skills/... 读取来源
= ResolvedAgentSpec.skill_registry
= AgentEngineProfile.skill_catalog_fingerprint
= RunContextSnapshot 记录的 Skill snapshot ID
```

活动 Run 持有旧 snapshot 直到终态；新 Run 不得 acquire 已因旧 Skill snapshot 进入 draining 的 AgentEngine。

## 实施步骤

1. 在 `skills.py` 中建立 `SkillCatalogManager`，以文件元数据做快速变化判断，以 SHA-256 内容摘要确认真实变化。
2. 保留 `SkillRegistry` 为 immutable snapshot；删除“单例 Registry 永久代表整个进程”的假设。
3. 把 requested Skill 解析移入统一 `RunPreparation`，删除 `RunCoordinator` 在 preparation 前通过全局 Registry 解析的路径。
4. 让 `ContextLifecycle` 从同一 Registry 生成 Skill 索引，并把 snapshot ID 放入 RunContextSnapshot。
5. 让 `resolve_builtin_main_agent_spec()` 和 AgentEngineProfile 只消费该 Run 准备结果，不重新读取 Catalog manager。
6. 调整 `virtual_files.py`：`/.harness/skills/...` 从当前 RunContext 取得本轮 Registry；缺失、跨 Run 或摘要不匹配时失败关闭。
7. 将 skills list/inspect 和 mutation RPC 接入 Catalog manager；修改成功后标记下一顶层 Run 重新扫描，并把用户可见生效提示改为 `next_run` 或等价明确语义。
8. 接入 HC-095 的 snapshot/profile 定向失效：变化只 draining 受影响 Engine，不关闭仍被活动 Run 借用的 Registry 或共享资源。
9. 保留现有 front matter、大小、symlink、路径穿越和读取时 digest 校验；刷新不能降低任何安全检查。
10. 更新用户与开发者文档，说明修改 Skill 后对当前 Run 和下一 Run 的影响。

## 主要代码位置

- `packages/agent/harness_agent/skills.py`
- `packages/agent/harness_agent/context_lifecycle.py`
- `packages/agent/harness_agent/run_context.py`
- `packages/agent/harness_agent/run_coordinator.py`
- `packages/agent/harness_agent/virtual_files.py`
- `packages/agent/harness_agent/agent_spec.py`
- `packages/agent/harness_agent/server.py`
- `packages/agent/harness_agent/agent_engine.py`
- `packages/agent/tests/test_skills.py`
- `packages/agent/tests/test_run_coordinator.py`
- `packages/agent/tests/test_server.py`

## 范围

- 内置、用户级、项目级和当前已支持市场安装来源的可刷新 snapshot。
- 顶层 Run 开始时刷新，Run 内固定。
- Skill mutation 的下一 Run 生效语义。
- 通过 HC-095 定向失效受影响 AgentEngine。

## 非范围

- 不在活动 Run 中热替换 Skill。
- 不新增 Skill 来源、Marketplace 协议或远端文件监听 daemon。
- 不允许 Skill 修改真实工具或 Policy；Skill 仍只是受限上下文与资源。
- 不实现跨 Host Catalog 共享。

## 验收清单

- [x] 新增、修改、删除、启停或更新 Skill 后，同一 Thread 下一 Run 使用新 snapshot，无需重启（直接准备路径、manager mutation 和刷新回归已覆盖）。
- [x] 内容未变化时复用 snapshot，不重建 AgentEngine（manager 对象 identity 和 Skill Profile 定向 predicate 已覆盖）。
- [x] requested Skill、Prompt 索引、虚拟文件、ResolvedAgentSpec 和 Profile 使用同一 snapshot ID（identity guard 与直接回归已覆盖）。
- [x] 活动 Run 在文件变化后仍持有自己的旧 snapshot；requested Skill 已预加载，后续 Run 使用新正文（共享图真实模型 Run 的完整终态仍待更大范围验证）。
- [x] 新 Run 不会 acquire 已因旧 Skill snapshot 进入 draining 的 AgentEngine（调用 HC-095 `AgentEnginePool.invalidate` 定向 seam；真实 Pool 组合验证待固定窄评审）。
- [x] 非法 front matter、symlink、路径穿越、超限和 TOCTOU 摘要变化继续失败关闭。
- [x] 只修改 Skill 文本不能增加工具、绕过审批或改变 EffectivePolicy（新增正文隔离回归覆盖 ResolvedAgentSpec、Profile、Context capability prompt、typed Policy、工具、Sandbox 和 MCP）。
- [x] RPC 与用户文档不再声称必须等待 `next_thread` 或重启，mutation 结果改为 `next_run`。

## 实施结果

- `SkillCatalogManager` 由 `AgentHost` 独占，使用文件元数据做快速判断、状态/manifest digest 做复核；内容未变化复用原 immutable `SkillRegistry`，变化才发布新 snapshot。
- Run 准备在同一 snapshot 上完成 requested Skill 预加载、Prompt index、`ResolvedAgentSpec`、Profile 和 `RunContextSnapshot`；共享图的 `/.harness/skills` 从当前 `RunContext` 取得 Registry 和 snapshot ID。
- Skill catalog 变化通过 HC-095 `AgentEnginePool.invalidate(predicate, reason="skill_catalog_changed")` draining 旧 catalog 的全部 Profile；Profile 指纹同时包含策略裁剪后的 Skill view，catalog producer 无法从新 registry 反推旧 view，因此不能用未裁剪指纹做误判。旧 lease/活动 Run 不被替换。
- AgentEngine 构建阶段使用与 `ResolvedAgentSpec.runtime_profile` 相同的 `skill_view_fingerprint` 计算预期指纹，避免正常的策略裁剪被误报为 `RUNTIME_SKILL_SNAPSHOT_MISMATCH`。
- 直接相关测试命令已通过：`cd packages/agent && .venv/bin/python -m pytest tests/test_skills.py tests/test_virtual_files.py tests/test_context_lifecycle.py tests/test_run_coordinator.py tests/test_server.py tests/test_agent_engine.py -q -p no:cacheprovider`（119 passed）；失败路径不推进 manager dirty/current，成功 mutation 仍返回 `next_run`。
- 新增正文安全隔离回归已通过：`cd packages/agent && .venv/bin/python -m pytest tests/test_server.py tests/test_skills.py -q -p no:cacheprovider`（60 passed）；同一配置下仅修改 Skill body 会得到新 Skill snapshot/Prompt index/virtual content，但 ResolvedAgentSpec 其余字段、Profile 除 Skill fingerprint 外的 identity、实际 tools、EffectivePolicy/approval、Sandbox 和 MCP 均不变，伪造工具或审批文本不进入 typed fields。
- 固定窄评审 P1 回归覆盖 snapshot-owned 正文/资源隔离、Skill 树 manifest 父目录和嵌套资源目录的 fd 锚定 symlink 竞态、文件级 TOCTOU、非法 artifact 暂存校验，以及 journal 在 old→backup 崩溃、new→target 崩溃和 rollback 失败后的恢复；失败路径不推进 manager dirty/current，成功 mutation 仍返回 `next_run`。
- 固定窄评审确认全部 P1 已关闭、无新增 P0/P1；补充回归同时证明只修改 Skill 文本不会改变工具、EffectivePolicy、审批、Sandbox 或 MCP 等 typed 安全能力。

## 验证命令

```bash
cd packages/agent && .venv/bin/python -m pytest -q \
  tests/test_skills.py \
  tests/test_run_coordinator.py \
  tests/test_server.py \
  tests/test_agent_engine.py
bun run typecheck
bun run test
bun run project:check
```

增加并发测试：一个 Run 持有旧 snapshot 时修改 Skill，第二个 Thread/后续 Run 取得新 snapshot，两个执行均不得串线。

## 版本影响

Skill 生效语义属于用户可见变化，用户文档已同步更新。本任务不单独调整版本；统一版本切换与发布记录留到依赖本任务的 HC-107 完成，避免中间能力形成半发布状态。

## 前置

- HC-095
- HC-102
