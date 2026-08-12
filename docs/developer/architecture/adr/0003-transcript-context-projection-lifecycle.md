# ADR 0003：Transcript 事实层与上下文投影生命周期

日期：2026-08-03
状态：已接受

关联任务：[HC-107](../../task/HC-107-修复WebComposer、补.md)
关联架构：[架构总览](../架构总览.md)

## 背景

上下文压缩、LangGraph 状态、Thread 恢复和下一次 Run 的系统上下文曾处于不同的生命周期。
如果把 LangGraph `messages` 当作用户历史，压缩就会使 UI 历史丢失；如果把旧
`PromptEpoch` 或常驻 Skill registry 当作下一次 Run 的来源，文件变化又会污染当前 Run 的
审计事实。v6 数据还可能只有 checkpoint 能证明的部分历史，不能在迁移时伪造完整 Transcript。

## 决策

### 两个明确的事实边界

- `ThreadPersistence` 的 append-only Transcript 是用户可见消息、Run binding、
  `RunContextSnapshot`、Artifact、Summary、`CompressionCheckpoint` 和结构化运行态的事实来源。
  `accept_run()` 在一个事务中原子受理 binding、snapshot 和首条用户 Transcript。
- LangGraph 是执行状态和模型工作历史的投影缓存，不是 UI 历史来源。`ContextProjector` 只从
  最新有效 checkpoint 加上其后的 Transcript tail 生成模型投影，再以全量缓存替换同步 LangGraph。
  `threads.open` 只读取完整 Transcript；两条路径不互相 fallback。

### Run 边界和唯一顺序

每个顶层 `run.start` 都重新读取一致的 AGENTS 与 Skill catalog snapshot，并在同一
`RunPreparation` 中解析 requested Skill、`ResolvedAgentSpec`、Profile 和
`RunContextSnapshot`。生产顺序固定为：

```text
RunPreparation（同一 AGENTS/Skill snapshot）
→ ThreadPersistence.accept_run（binding + snapshot + user Transcript 原子受理）
→ acquire 对应 AgentEngine
→ 构造该 Run 的 RunContext
→ ContextProjector（latest-valid checkpoint + Transcript tail）
→ 执行；模型调用按需触发分级压缩
```

Run 内不重新扫描 AGENTS/Skill，也不把当前 Run 的文件变化热切换到已有 snapshot。取消、
Interaction、幂等重试、Thread owner 和 AgentEngine lease 继续由各自既有 owner 管理。

### 分级压缩和恢复

自动压力处理先尝试确定性的 micro；micro 后必须重新计量，只有仍达到 full 水位才调用
结构化摘要模型。micro 足够时不调用摘要模型；同一轮 micro/full 只提交最终有效投影。每个
Artifact、Summary、ContextState 和 checkpoint 通过 `ThreadPersistence.commit_context()`
在同一事务中提交。恢复始终选择 latest-valid checkpoint 加 Transcript tail，连续多个 full
不会把较旧 checkpoint 重新拼入模型历史。

`context.updated` 和 TUI 只显示 action、预算、状态和安全诊断码；不显示 checkpoint ID、完整
Prompt、绝对路径、工具原文、密钥、Header 或认证 Query。诊断异常原文不会跨越服务端 wire
边界。

### 迁移与未来长期记忆

v1-v6 直接升级到当前 schema。父进程只持有迁移排他锁并做只读事实检查，完整的 verified backup、
DDL/data、final validation 和 commit 事务运行在可终止的 `harness_agent.migration_worker` child
中；child 超时先 terminate，再有界 kill+wait，父进程确认 child 已退出后才按 final/source/half
事实恢复或返回 typed fail-closed。超时 poison 在释放 lock 前发布，锁后再次检查；verified
backup/state 留给下一 owner。旧 `harness_prompt_epochs` 只在 verified v2-v6 source backup 的这次
事务内作为单向 adapter：转换为明确的 `legacy` snapshot、只回填可证明的旧 binding，随后在同一
成功事务中删除旧表；任一步失败不伪造历史，缺失或无法证明的消息保持 `legacy/incomplete`。
v2-v5 没有 Run binding 时不创建关联；v7 及更高普通库残留该表且没有受保护 migration state 时
fail closed；adapter 的退出条件就是
迁移成功且旧表不再存在。

长期记忆不属于本 ADR 的实现范围。未来若引入，只能作为 `ContextLifecycle` 的 Run 级动态块
注入本次 snapshot，受长度、权限和 AGENTS/Skill 约束；不得成为 Transcript 事实层、跨 Run
隐式 fallback 或新的模型历史来源。

## 后果

- UI 恢复和模型执行可以独立验证：压缩不会删除用户历史，LangGraph 缓存损坏也不会反向
  伪造 Transcript。
- 下一次 Run 会看到新的 AGENTS/Skill，而历史 Run 仍保存启动时的旧 snapshot。
- 迁移完成后生产代码不再写入或读取 PromptEpoch 表；非共享嵌入式调用保留的旧类型只允许
  显式兼容场景，不能被 Project-scoped AgentHost 路由。
- 压缩、迁移和生命周期测试必须使用临时目录、mock 模型和可验证的 typed fixture，不依赖
  真实 API 或真实凭据。

## 备选方案

- 以 LangGraph checkpoint 作为 UI 历史：拒绝，会把投影缓存误当事实并丢失完整 Transcript。
- 在每次模型调用时重新扫描 AGENTS/Skill：拒绝，会破坏 Run snapshot 的不可变审计边界。
- 长期保留 PromptEpoch 与新 snapshot 双写/双读：拒绝，会形成不可判定的事实来源；只保留
  v6 迁移事务内的单向 adapter。
