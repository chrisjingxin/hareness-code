# Tasks

- [ ] Task 1: Agent 定义模型与解析器
  - [ ] 1.1 在 `packages/agent/harness_agent/` 新建 `subagents.py`，定义 `AgentDefinition` dataclass（name, description, tools, disallowed_tools, model, color, max_turns, background, system_prompt, source, file_path）
  - [ ] 1.2 实现 `parse_agent_markdown(path) -> AgentDefinition` 解析器（YAML frontmatter + 正文 prompt）
  - [ ] 1.3 实现 `serialize_agent_markdown(defn) -> str` 序列化函数
  - [ ] 1.4 编写单元测试 `tests/test_subagents_parse.py`

- [ ] Task 2: Agent 发现与注册表
  - [ ] 2.1 在 `subagents.py` 中实现 `discover_agents(workspace, user_home) -> list[AgentDefinition]`，按 project(`.harness/agents/`) > user(`~/.harness/agents/`) > builtin 优先级扫描
  - [ ] 2.2 实现 `AgentRegistry` 类：`load()` / `get(name)` / `list()` / `register(defn)` / `unregister(name)`，同名高优先级覆盖
  - [ ] 2.3 注册内置 agent 定义（general-purpose 保持现有、explore 只读、plan 规划）
  - [ ] 2.4 编写单元测试 `tests/test_subagents_discovery.py`

- [ ] Task 3: 工具作用域过滤与深度限制
  - [ ] 3.1 实现 `filter_tools_for_agent(all_tools, defn) -> list` 函数：白名单 > 黑名单 > 全量
  - [ ] 3.2 实现 `SubagentDepthGuard` 中间件：子代理的 task 工具调用直接拒绝（深度=1）
  - [ ] 3.3 内置 explore agent 排除写入工具（write_file, edit_file, execute 写命令）
  - [ ] 3.4 编写单元测试 `tests/test_subagents_tools.py`

- [ ] Task 4: 集成到 create_harness_agent
  - [ ] 4.1 修改 `agent.py` 的 `_create_default_subagents()` 为 `_build_subagents(registry, workspace, approval_mode)`，遍历 registry 构建 deepagents subagent dict 列表
  - [ ] 4.2 每个 subagent dict 包含：name, description, system_prompt（agent 自定义或默认）, tools（过滤后）, middleware（深度守卫 + 工作区边界 + 审批模式）
  - [ ] 4.3 task 工具描述动态生成：从 registry 读取所有非 primary agent 的 name+description 拼入描述
  - [ ] 4.4 编写集成测试验证多 agent 注册后 task 工具可正确路由

- [ ] Task 5: JSON-RPC agents.* 方法
  - [ ] 5.1 在 `server.py` 注册 `agents.list` / `agents.create` / `agents.update` / `agents.remove` handler
  - [ ] 5.2 `agents.list` 返回所有 agent 的 name/description/color/source/tools/max_turns
  - [ ] 5.3 `agents.create` 接收参数 -> 序列化 -> 写入文件 -> 热加载到 registry
  - [ ] 5.4 `agents.update` 读取 -> 合并修改 -> 写回 -> 热加载
  - [ ] 5.5 `agents.remove` 校验非 builtin -> 删除文件 -> 从 registry 移除
  - [ ] 5.6 在 `packages/protocol/` 新增对应 TypeScript 类型定义
  - [ ] 5.7 编写 Python 端测试 `tests/test_subagents_rpc.py`

- [ ] Task 6: Subagent 事件通知扩展
  - [ ] 6.1 在 server.py 的事件发送中，识别子代理来源并填充 `source: {kind: "subagent", agent_type, agent_name}`
  - [ ] 6.2 子代理启动/完成/失败时发送 `run.subagent_started` / `run.subagent_completed` / `run.subagent_failed` 事件
  - [ ] 6.3 编写测试验证事件格式

- [ ] Task 7: CLI /agents 命令与 TUI 展示
  - [ ] 7.1 在 `packages/cli/` 新增 `/agents` 斜杠命令注册
  - [ ] 7.2 实现列表视图：调用 `agents.list` RPC，表格展示（颜色圆点 + 名称 + 描述 + 来源标签）
  - [ ] 7.3 实现创建向导：引导输入 name/description -> 选择工具 -> 选择颜色 -> 调用 `agents.create`
  - [ ] 7.4 实现详情查看：选择 agent -> 展示完整配置和 prompt
  - [ ] 7.5 实现删除确认：选择 agent -> 确认 -> 调用 `agents.remove`
  - [ ] 7.6 subagent 运行时 TUI 状态指示：颜色标记 + spinner + 描述文本

- [ ] Task 8: 文档与端到端验证
  - [ ] 8.1 更新 `docs/user/` 新增 subagent 使用说明（定义文件、/agents 命令、内置类型）
  - [ ] 8.2 更新 `docs/developer/架构总览.md` 补充 subagent 模块说明
  - [ ] 8.3 运行 `bun run project:check` + `bun run typecheck` + `bun run test` 全量验证
  - [ ] 8.4 运行 `cd packages/agent && .venv/Scripts/python -m pytest -q` 验证 Python 测试

# Task Dependencies

- Task 2 depends on Task 1（解析器是发现的基础）
- Task 3 depends on Task 1（需要 AgentDefinition 结构）
- Task 4 depends on Task 2 + Task 3（需要 registry 和工具过滤）
- Task 5 depends on Task 2（需要 registry 的 CRUD）
- Task 6 depends on Task 4（需要 subagent 执行集成）
- Task 7 depends on Task 5（需要 RPC 方法）
- Task 8 depends on all above
