# Tasks

## Phase 1: 工具风险分级与审批基础设施

- [x] Task 1: 新增工具风险分级模块 `tool_risk.py`
  - [x] 1.1: 定义 `ToolKind` 枚举（Read/Edit/Delete/Execute/Agent/Interact/Plan/Fetch）
  - [x] 1.2: 定义 `TOOL_KIND_MAP` 映射表（工具名 → ToolKind）
  - [x] 1.3: 实现 `get_tool_kind(tool_name) -> ToolKind` 查询函数（未知工具 fail-closed 为 Execute）
  - [x] 1.4: 实现 `is_read_only(tool_name) -> bool` 快捷判断
  - [x] 1.5: 编写单元测试 `tests/test_tool_risk.py`

- [x] Task 2: 新增权限规则持久化模块 `permission_rules.py`
  - [x] 2.1: 定义 `PermissionRule` 数据类（tool/resource/effect）
  - [x] 2.2: 实现通配符匹配函数 `matches_rule(pattern, value) -> bool`
  - [x] 2.3: 实现规则加载：从 `.harness/settings.json`（project）和 `~/.harness/settings.json`（user）读取
  - [x] 2.4: 实现规则评估 `evaluate_rules(tool, resource, rules) -> "allow"|"deny"|"ask"|None`（最后匹配优先）
  - [x] 2.5: 实现规则写入 `save_rule(rule, scope)` 持久化到对应层级
  - [x] 2.6: 编写单元测试 `tests/test_permission_rules.py`

- [x] Task 3: 新增敏感路径保护模块 `sensitive_paths.py`
  - [x] 3.1: 定义 `SENSITIVE_FILES` 和 `SENSITIVE_DIRECTORIES` 常量
  - [x] 3.2: 实现 `is_sensitive_path(file_path) -> bool` 检测函数
  - [x] 3.3: 编写单元测试 `tests/test_sensitive_paths.py`

## Phase 2: 审批流水线重构

- [x] Task 4: 重构 `approval_policy.py` 为多级审批流水线
  - [x] 4.1: 实现 `evaluate_permission(tool_name, resource, rules, mode) -> PermissionDecision` 统一入口
  - [x] 4.2: L1 参数验证层（由工具自身负责，流水线跳过无效调用）
  - [x] 4.3: L2 deny 规则硬拦截（任何模式不可覆盖）
  - [x] 4.4: L3 只读工具放行（ToolKind.READ/INTERACT/PLAN 直接 allow）
  - [x] 4.5: L4 规则评估（session > project > user 顺序匹配）
  - [x] 4.6: L5 审批模式覆盖（plan/default/auto-edit/yolo 按 ToolKind 查表）
  - [x] 4.7: 敏感路径 safetyCheck 注入（yolo 免疫）
  - [x] 4.8: 保留 `PlanModeMiddleware` 兼容性
  - [x] 4.9: 更新现有测试 + 新增 `tests/test_approval_pipeline.py`

- [x] Task 5: 扩展审批响应协议
  - [x] 5.1: 修改 `protocol_generated.py` 中 `ApprovalResponse.decision` 新增 `approve_always` / `reject_with_feedback`
  - [x] 5.2: 修改 `server.py` 中 `_resume_value()` 处理新决策类型
  - [x] 5.3: `approve_always` 时调用 `permission_rules.save_rule()` 持久化
  - [x] 5.4: `reject_with_feedback` 时将反馈注入 ToolMessage
  - [x] 5.5: 修改 `packages/protocol/` 中对应 TypeScript 类型
  - [x] 5.6: 更新 IPC 测试

- [x] Task 6: 新增拒绝追踪机制
  - [x] 6.1: 在 `denial_tracking.py` 中实现连续拒绝计数器
  - [x] 6.2: 连续 ≥3 次拒绝时注入系统提示
  - [x] 6.3: 审批通过时重置计数器
  - [x] 6.4: 编写单元测试

## Phase 3: 新增工具实现

- [x] Task 7: 实现 web_search / web_fetch 工具
  - [x] 7.1: 在 `agent.py` 的 `_BUILTIN_TOOL_SHAPES` 中新增工具契约
  - [x] 7.2: 实现 `web_search` 工具执行逻辑（HTTP 调用搜索 API）
  - [x] 7.3: 实现 `web_fetch` 工具执行逻辑（HTTP GET + 内容转换）
  - [x] 7.4: 配置 ToolKind（web_search=READ, web_fetch=FETCH）
  - [x] 7.5: 编写测试（mock HTTP）

- [x] Task 8: 实现 delete_file / apply_patch 工具
  - [x] 8.1: 实现 `delete_file` 工具（路径校验 + 工作区边界 + 删除）
  - [x] 8.2: 实现 `apply_patch` 工具（解析 unified diff + 应用）
  - [x] 8.3: 配置 ToolKind（delete_file=DELETE, apply_patch=EDIT）
  - [x] 8.4: 编写测试

- [x] Task 9: 实现 lsp / tool_search 工具
  - [x] 9.1: 实现 `lsp` 工具（对接 LSP 服务器或 IDE 能力）
  - [x] 9.2: 实现 `tool_search` 工具（搜索已注册 MCP 工具）
  - [x] 9.3: 配置 ToolKind（lsp=READ, tool_search=READ）
  - [x] 9.4: 编写测试

- [x] Task 10: 实现 enter_plan_mode / exit_plan_mode 工具
  - [x] 10.1: 实现模式切换逻辑（PlanModeState 类）
  - [x] 10.2: 配置 ToolKind（PLAN）
  - [x] 10.3: 与 PlanModeMiddleware 集成
  - [x] 10.4: 编写测试

- [x] Task 11: 实现 task_output / task_stop / monitor 工具
  - [x] 11.1: 实现 `task_output` 获取后台任务输出
  - [x] 11.2: 实现 `task_stop` 终止后台任务
  - [x] 11.3: 实现 `monitor` 后台持续执行命令（BackgroundTaskManager）
  - [x] 11.4: 配置 ToolKind（task_output=READ, task_stop=EXECUTE, monitor=EXECUTE）
  - [x] 11.5: 编写测试

- [x] Task 12: 实现 memory_search / memory_save 工具
  - [x] 12.1: 设计记忆存储格式（`~/.harness/memory/` 目录，JSON 文件）
  - [x] 12.2: 实现 `memory_save` 持久化
  - [x] 12.3: 实现 `memory_search` 关键词检索
  - [x] 12.4: 配置 ToolKind（memory_save=INTERACT, memory_search=READ）
  - [x] 12.5: 编写测试

## Phase 4: 集成与协议同步

- [x] Task 13: 更新 `agent.py` 工具注册与中间件集成
  - [x] 13.1: 扩展 `_BUILTIN_TOOL_SHAPES` 包含所有新工具（22 个）
  - [x] 13.2: 更新 `concurrency.py` 并发安全性分类（新工具）
  - [x] 13.3: 更新 `workspace_boundary.py` 覆盖新文件操作工具
  - [x] 13.4: 确保 `shell_allow_list.py` 与新 execute 类工具兼容

- [x] Task 14: CLI 审批 UI 适配
  - [x] 14.1: 修改 `packages/protocol/` 审批请求/响应类型
  - [x] 14.2: CLI 审批弹窗新增 "永久允许" 和 "拒绝并反馈" 选项
  - [x] 14.3: 拒绝并反馈时传递 feedback 字段
  - [x] 14.4: 更新 IPC 测试

- [x] Task 15: 端到端集成测试
  - [x] 15.1: 测试完整审批流水线（L1-L5 各路径）
  - [x] 15.2: 测试规则持久化读写
  - [x] 15.3: 测试敏感路径保护在 yolo 下的行为
  - [x] 15.4: 测试拒绝追踪回退
  - [x] 15.5: 运行 `bun run typecheck` + `bun test` + `pytest`

# Task Dependencies

- Task 4 依赖 Task 1, 2, 3（审批流水线需要风险分级、规则和敏感路径模块）
- Task 5 依赖 Task 2（approve_always 需要规则持久化）
- Task 6 依赖 Task 5（拒绝追踪基于审批响应扩展）
- Task 7-12 依赖 Task 1（新工具需要 ToolKind 分类）
- Task 13 依赖 Task 7-12（集成需要所有工具就绪）
- Task 14 依赖 Task 5（CLI 适配需要协议扩展）
- Task 15 依赖 Task 13, 14（端到端测试需要全部就绪）
- Task 7-12 之间无依赖，可并行执行
