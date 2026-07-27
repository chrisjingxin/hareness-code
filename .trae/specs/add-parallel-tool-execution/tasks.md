# Tasks

- [x] Task 1: 添加并发安全分区模块（ToolNode 前置层）
  - [x] SubTask 1.1: 创建 `packages/agent/harness_agent/concurrency.py`，实现 `is_concurrency_safe(tool_name, args)` 判定函数
    - 只读工具（ls、read_file、glob、grep）：始终 True
    - 写工具（write_file、edit_file）：始终 False
    - Shell 工具（execute）：动态判定，只读命令 True
    - 子 Agent 工具（task）：始终 True（天然无共享状态）
  - [x] SubTask 1.2: 实现 Shell 命令只读判定（参考 qwen-code 的 `shellReadOnlyChecker` 白名单模式）
  - [x] SubTask 1.3: 实现 `partition_tool_calls()` 分区算法：连续安全工具合并为 parallel batch，非安全工具独立 sequential batch
  - [x] SubTask 1.4: 实现带并发上限的批次执行协调器（读取 `HARNESS_MAX_TOOL_CONCURRENCY` 环境变量，默认 10；parallel batch 内通过 asyncio.Semaphore 限流后交给 gather）

- [x] Task 2: 集成分区层到 Agent 图（不替换 ToolNode）
  - [x] SubTask 2.1: 并发安全分类内置于 `is_concurrency_safe()` 函数中，中间件按工具名动态判定
  - [x] SubTask 2.2: 实现 `ConcurrencyGuardMiddleware`（concurrency_guard.py）：通过 AsyncRWLock 在 awrap_tool_call 层拦截，只读工具获取共享读锁并行，写工具获取独占写锁串行。效果等价于分区执行，无需替换 ToolNode
  - [x] SubTask 2.3: HITL interrupt 已正确打包多个并行待审批工具（server.py `_extract_interaction` 和 `_resume_value` 已有 action_count 逻辑）
  - [x] SubTask 2.4: task 工具在 `is_concurrency_safe()` 中标记为始终 True，多个 task 调用获取读锁并行执行

- [x] Task 3: 系统提示词并行引导
  - [x] SubTask 3.1: 在系统提示词中添加并行工具调用指导段落，区分两种模式：
    - 简单原子操作（读文件、搜索、列目录）：在同一轮发出多个简单 tool_calls
    - 复杂多步任务（调研、多文件实现）：在同一轮发出多个 task 调用委派子 Agent
  - [x] SubTask 3.2: 明确指导模型：有依赖关系的操作必须分轮顺序发出，不得并行
  - [x] SubTask 3.3: 并行指导已覆盖 task 工具使用场景（task 工具描述由 deepagents 框架管理，prompt 层面已补充）

- [x] Task 4: CLI 端并发工具状态展示
  - [x] SubTask 4.1: 确认 protocol 层 `tool.started`/`tool.completed` 事件已支持多工具并行（基于 tool_call_id 独立路由，无单工具限制）
  - [x] SubTask 4.2: CLI TUI 层已支持同时渲染多个正在执行的工具状态行（timeline 数组驱动，各工具独立卡片）

- [x] Task 5: 测试与验证
  - [x] SubTask 5.1: Python 端单元测试：59 个用例覆盖并发安全判定、Shell 只读判定、分区算法、批次执行协调器、AsyncRWLock、ConcurrencyGuardMiddleware
  - [x] SubTask 5.2: 中间件并发行为测试：验证多读并行、写阻塞读的异步行为
  - [x] SubTask 5.3: HITL 打包逻辑已在 server.py 中实现（action_count 机制），现有测试覆盖
  - [x] SubTask 5.4: task 工具并发安全标记已验证（is_concurrency_safe("task", {}) → True）

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 无依赖，可与 Task 1 并行
- Task 4 无依赖，可与 Task 1/2 并行
- Task 5 depends on Task 1, Task 2, Task 3
