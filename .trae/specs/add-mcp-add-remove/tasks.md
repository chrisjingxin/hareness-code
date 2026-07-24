# Tasks

- [x] Task 1: 协议层新增 `mcp.add` 和 `mcp.remove` 方法
  - [x] 1.1 修改 `packages/protocol/schema/v2.json`：`minor` 改为 6，`client_methods` 追加 `"mcp.add"` 和 `"mcp.remove"`
  - [x] 1.2 修改 `packages/protocol/scripts/generate.ts`：在 renderTypeScript 模板中新增 `McpAddParams`、`McpAddResult`、`McpRemoveParams`、`McpRemoveResult` 接口；在 renderPython 模板中新增对应 Pydantic 模型
  - [x] 1.3 运行 `bun run protocol:generate` 重新生成两端代码
  - [x] 1.4 修改 `packages/protocol/src/index.ts`：新增 `Method.MCP_ADD`、`Method.MCP_REMOVE` 常量和 `assertMcpAddParams`、`assertMcpRemoveParams` 断言
  - [x] 1.5 验证：`bun run typecheck` 通过

- [x] Task 2: Python 端 TOML 配置写入模块
  - [x] 2.1 修改 `packages/agent/pyproject.toml`：新增 `tomli-w>=1.1.0` 依赖
  - [x] 2.2 安装依赖：`cd packages/agent && .venv\Scripts\pip install tomli-w`（或使用项目的 Python 环境）
  - [x] 2.3 新建 `packages/agent/harness_agent/mcp_config_writer.py`：实现 `add_server_to_config(config_path, server_dict)` 和 `remove_server_from_config(config_path, name)` 函数，使用 tomllib 读取 + tomli_w 写入，原子替换（先写临时文件再 rename）
  - [x] 2.4 新建或扩展测试：覆盖文件不存在、文件已存在追加、删除存在/不存在的服务器、原子写入
  - [x] 2.5 验证：pytest 通过

- [x] Task 3: Python 端 McpConnectionManager 热连接/热断开
  - [x] 3.1 修改 `packages/agent/harness_agent/mcp.py`：新增 `async add_server(config: McpServerConfig) -> dict` 方法，创建临时 MultiServerMCPClient 连接单个服务器，合并工具到 `self._tools`，更新 `_server_statuses`
  - [x] 3.2 新增 `remove_server(name: str) -> bool` 方法，按 `{name}_` 前缀过滤移除工具，更新状态
  - [x] 3.3 新增测试覆盖热连接成功、热连接失败、热断开
  - [x] 3.4 验证：pytest 通过

- [x] Task 4: Python 端 `mcp.add` 和 `mcp.remove` JSON-RPC 处理器
  - [x] 4.1 修改 `packages/agent/harness_agent/server.py`：注册 `"mcp.add"` 和 `"mcp.remove"` 处理器
  - [x] 4.2 实现 `_handle_mcp_add`：校验参数（名称合法性、必填字段、重复检查），调用 `mcp_config_writer.add_server_to_config` 写入配置，调用 `McpConnectionManager.add_server` 热连接，返回结果
  - [x] 4.3 实现 `_handle_mcp_remove`：校验服务器存在，调用 `mcp_config_writer.remove_server_from_config` 删除配置，调用 `McpConnectionManager.remove_server` 热断开，返回结果
  - [x] 4.4 新增测试覆盖添加/删除/重复/不存在场景
  - [x] 4.5 验证：pytest 通过

- [x] Task 5: CLI 端 `/mcp add` 和 `/mcp remove` 子命令
  - [x] 5.1 修改 `packages/cli/src/ipc/client.ts`：新增 `mcpAdd(params)` 和 `mcpRemove(name)` 便捷方法
  - [x] 5.2 修改 `packages/cli/src/tui/app.tsx`：重构 `case "mcp"` 分支，解析 `command.argument` 中的子命令（add/remove/无），实现参数解析（`--url`、`--sse` 标志），调用对应 RPC 方法并格式化结果
  - [x] 5.3 更新 `packages/cli/tests/tui/app-interaction.test.ts`：新增 `/mcp add` 和 `/mcp remove` 集成测试
  - [x] 5.4 验证：`bun run typecheck` 和 `bun test` 通过

- [x] Task 6: 文档更新与项目检查
  - [x] 6.1 更新 `docs/user/交互使用.md`：添加 `/mcp add` 和 `/mcp remove` 用法说明
  - [x] 6.2 运行 `bun run typecheck`、`bun run test`、pytest 确认全项目通过

# Task Dependencies

- Task 2 和 Task 3 可并行（分别处理配置写入和运行时连接）
- Task 4 依赖 Task 1、2、3（需要协议类型、配置写入和热连接）
- Task 5 依赖 Task 1 和 Task 4（需要协议类型和 Python 端处理器）
- Task 6 依赖 Task 1-5
