# Tasks

- [x] Task 1: 实现 MCP 配置解析模块
  - [x] 1.1 新建 `packages/agent/harness_agent/mcp.py`，定义 MCP 服务器配置数据模型（`McpServerConfig` dataclass：name、transport、command、args、env、url、headers、timeout）
  - [x] 1.2 实现 `parse_mcp_config(config_data: dict) -> list[McpServerConfig]` 函数，从 TOML 解析的 `[mcp]` 区段提取服务器列表
  - [x] 1.3 实现环境变量 `${VAR}` 展开逻辑（连接时解析，缺失时警告并跳过）
  - [x] 1.4 实现配置校验：stdio 必须有 command+args，http/sse 必须有 url，无效配置记录警告并跳过
  - [x] 1.5 修改 `config_manifest.py`：将 `mcp` 区段状态改为 `"implemented"`，`allowed_sources` 设为 `ACTIVE_TOML_SOURCES`
  - [x] 1.6 修改 `config.py`：在 `load_config()` 中解析 `[mcp]` 区段，将结果存入 `Za38Config.mcp_servers`

- [x] Task 2: 实现 MCP 连接管理与工具加载
  - [x] 2.1 在 `mcp.py` 中实现 `McpConnectionManager` 类，持有 `MultiServerMCPClient` 实例和连接状态
  - [x] 2.2 实现 `async connect_all()` 方法：根据配置构建 `langchain-mcp-adapters` 的 Connection dict，创建 `MultiServerMCPClient`，调用 `get_tools()` 获取所有工具
  - [x] 2.3 实现 `async close_all()` 方法：关闭所有 MCP 连接，释放资源
  - [x] 2.4 实现 `get_tools() -> list[BaseTool]` 方法：返回已加载的 MCP 工具列表（带服务器名前缀）
  - [x] 2.5 实现 `mcp_config_fingerprint(configs: list[McpServerConfig]) -> str` 函数：计算脱敏配置指纹（仅含 name+transport，不含路径/URL/凭据）
  - [x] 2.6 连接超时处理：默认 30 秒，失败时记录 stderr 警告并跳过，不阻止启动

- [x] Task 3: 集成到 Agent 构图和 Server 生命周期
  - [x] 3.1 修改 `server.py`：在 `JsonRpcServer` 初始化时创建 `McpConnectionManager`，在 `initialize` 处理完成后调用 `connect_all()`
  - [x] 3.2 修改 `server.py`：将 MCP 工具传入 `_build_default_runtime()` → `create_harness_agent(tools=[...mcp_tools])`
  - [x] 3.3 修改 `server.py`：在 `run()` 的 finally 块中调用 `close_all()`
  - [x] 3.4 修改 `runtime_profile.py`：`default_runtime_profile()` 接受 `mcp_fingerprint` 参数，用实际配置计算指纹
  - [x] 3.5 修改 `agent.py`：MCP 工具通过 `tools` 参数参与 `extra_tools` schema 指纹计算（已有逻辑验证通过）

- [x] Task 4: MCP 工具审批集成
  - [x] 4.1 修改 `approval_policy.py`：`interrupt_on_for_approval_mode` 新增 `extra_interrupt_tools` 参数，default/auto-edit 模式下 MCP 工具需审批
  - [x] 4.2 确保 `PlanModeMiddleware` 的白名单不包含 MCP 工具（plan 模式下自动拒绝）
  - [x] 4.3 确保 `yolo` 模式下 MCP 工具不注册 HITL

- [x] Task 5: 测试与验证
  - [x] 5.1 新建 `tests/test_mcp.py`：测试配置解析（有效 stdio/http/sse、无效配置、环境变量展开）
  - [x] 5.2 测试连接管理：空配置连接、连接失败不抛异常、连接构建
  - [x] 5.3 测试环境变量展开：存在/缺失/多变量场景
  - [x] 5.4 测试错误处理：连接失败不阻止启动
  - [x] 5.5 测试审批集成：default 模式下 MCP 工具触发 HITL、plan 模式下被拒绝（test_approval_policy.py 新增 2 个测试）
  - [x] 5.6 测试配置指纹：配置变化产生不同指纹、脱敏（不含路径/凭据）、顺序无关
  - [ ] 5.7 端到端验证：启动一个真实 MCP 服务器，通过 Harness Agent 调用其工具并获取响应（需要用户配置实际 MCP Server 后手动验证）

# Task Dependencies

- Task 2 依赖 Task 1（需要配置解析结果）
- Task 3 依赖 Task 2（需要连接管理器）
- Task 4 依赖 Task 3（需要知道 MCP 工具名列表）
- Task 5 依赖 Task 1-4（需要完整实现）
