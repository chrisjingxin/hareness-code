# Checklist

## 并发安全分区（ToolNode 前置层）
- [x] `concurrency.py` 实现了 `is_concurrency_safe()` 函数，正确分类只读/写/动态/子Agent工具
- [x] task 工具（子 Agent）被标记为始终并发安全
- [x] Shell 命令只读判定覆盖常见只读命令（git status/log/diff、ls、cat、grep 等）和写操作排除（rm、mv、git push 等）
- [x] `partition_tool_calls()` 正确将工具序列分为交替的 parallel/sequential 批次
- [x] parallel batch 内通过 asyncio.Semaphore 限流，尊重 `HARNESS_MAX_TOOL_CONCURRENCY` 环境变量（默认 10）

## 集成（不替换 ToolNode）
- [x] ConcurrencyGuardMiddleware 通过 AsyncRWLock 实现并发控制后委托原始 handler 执行
- [x] 内置工具（ls、read_file、glob、grep、write_file、edit_file、execute、task）均有正确的并发安全标记
- [x] 多个只读 tool_calls 获取共享读锁，实际并行执行（测试验证 4 并发）
- [x] 写操作 tool_calls 获取独占写锁，不与其他工具并行（测试验证阻塞行为）
- [x] 多个 task 调用（子 Agent）获取读锁，可并行执行且上下文隔离（子 Agent 天然隔离）
- [x] HITL 场景下多个待审批工具正确打包为单个 interrupt（server.py action_count 机制）

## 提示词引导
- [x] 系统提示词包含并行工具调用指导，区分简单工具并行和 task 子 Agent 并行
- [x] 提示词明确指导有依赖的操作必须分轮顺序发出
- [x] task 工具并行使用指导已在系统提示词中覆盖（task 工具 description 由 deepagents 框架管理，非本仓库代码）

## CLI 展示
- [x] CLI 能同时展示多个正在执行的工具状态（timeline 数组驱动，各工具独立卡片，无单工具限制）

## 测试
- [x] Python 单元测试覆盖并发安全判定、分区算法、批次执行协调器（59 个用例全部通过）
- [x] 中间件并发行为测试验证多读并行、写阻塞读的异步行为
- [ ] 集成测试验证多 tool_calls 经分区后的完整事件流正确性（后续增强项，需 mock agent 图基础设施）
- [ ] 集成测试验证多个 task 调用并行执行的端到端行为（后续增强项，需 mock 子 Agent）
- [x] 未引入真实模型凭据，测试使用 mock
