# Tasks

- [x] Task 1: 协议层新增 `mcp.status` 方法
  - [x] 1.1 修改 `packages/protocol/schema/v2.json`：`minor` 改为 5，`client_methods` 追加 `"mcp.status"`，`server_capabilities` 追加 `"mcp.read"`
  - [x] 1.2 运行 `bun run protocol:generate`（或等效命令）重新生成 `generated.ts` 和 `protocol_generated.py`
  - [x] 1.3 在 `packages/protocol/src/generated.ts` 中确认新增 `McpServerStatus`、`McpStatusParams`、`McpStatusResult` 接口（若生成脚本不自动生成接口则手动补充，遵循现有 `ThreadsListResult` 等模式）
  - [x] 1.4 在 `packages/protocol/src/index.ts` 中新增 `Method.MCP_STATUS = "mcp.status"` 常量和 `assertMcpStatusParams` 断言函数
  - [x] 1.5 验证：`bun run typecheck` 通过，协议生成检查通过

- [x] Task 2: Python 端 McpConnectionManager 逐服务器状态
  - [x] 2.1 修改 `packages/agent/harness_agent/mcp.py`：`McpConnectionManager` 新增 `_server_statuses: dict[str, dict]` 字段，在 `connect_all()` 和 `_build_connections()` 中记录每个服务器的状态（connected/failed/skipped + error 信息）
  - [x] 2.2 新增 `get_server_statuses() -> list[dict]` 方法：返回每个服务器的 `{name, transport, status, error?, tool_names[]}`，工具名通过 `{server_name}_` 前缀从 `self._tools` 中匹配
  - [x] 2.3 修改 `packages/agent/tests/test_mcp.py`：新增 `TestMcpServerStatuses` 测试类，覆盖全部连接、部分失败、全部跳过、空配置场景
  - [x] 2.4 验证：`cd packages/agent && .venv/Scripts/python -m pytest tests/test_mcp.py -q` 通过

- [x] Task 3: Python 端 `mcp.status` JSON-RPC 处理器
  - [x] 3.1 修改 `packages/agent/harness_agent/server.py`：在 `self._handlers` 中注册 `"mcp.status": self._handle_mcp_status`
  - [x] 3.2 实现 `_handle_mcp_status` 方法：调用 `self._mcp_manager.get_server_statuses()` 返回 `{"servers": [...], "total_tools": N}`；无 MCP 配置时返回空列表
  - [x] 3.3 修改 `packages/agent/tests/test_server.py`（或新增测试）：验证 `mcp.status` 方法分发正确、未初始化时返回错误
  - [x] 3.4 验证：`cd packages/agent && .venv/Scripts/python -m pytest tests/test_server.py -q` 通过

- [x] Task 4: CLI 端 `/mcp` 斜杠命令与状态面板
  - [x] 4.1 修改 `packages/cli/src/ipc/client.ts`：新增 `mcpStatus(): Promise<McpStatusResult>` 便捷方法，调用 `this.call("mcp.status", {})`
  - [x] 4.2 修改 `packages/cli/src/tui/commands.ts`：`SlashCommandName` 追加 `"mcp"`，`slashCommandDefinitions` 追加 `{ name: "mcp", description: "查看 MCP 服务器状态" }`
  - [x] 4.3 修改 `packages/cli/src/tui/app.tsx`：在 `executeSlashCommand` 的 switch 中新增 `case "mcp"`，调用 `client.mcpStatus()` 并通过 `appendNotice` 渲染格式化的 MCP 状态面板（服务器列表 + 工具 + 汇总）
  - [x] 4.4 修改 `packages/cli/src/tui/model.ts`：`TuiRuntime` 新增 `mcpSummary?: string` 字段，`createTuiRuntime` 从 `config_summary.mcp_servers` 提取摘要；`runtimeStatusSummary` 输出追加 MCP 行
  - [x] 4.5 更新 `packages/cli/tests/tui/commands.test.ts`：将 `findSlashCommands("/mcp")` 断言改为匹配新注册的 mcp 命令
  - [x] 4.6 更新 `packages/cli/tests/tui/app-interaction.test.ts`：新增 `/mcp` 命令的集成测试（mock sidecar 返回 mcp.status 响应，验证面板渲染）
  - [x] 4.7 验证：`cd packages/cli && bun test` 通过，`bun run typecheck` 通过

- [x] Task 5: 文档更新与项目检查
  - [x] 5.1 更新 `docs/user/交互使用.md`：将 `/mcp` 从"尚未接入"列表移除，添加 `/mcp` 命令说明
  - [x] 5.2 运行 `bun run project:check` 确认文档链接和任务状态一致（协议检查通过；ZC-006 引用缺失为预存问题）
  - [x] 5.3 运行 `bun run typecheck` 和 `bun run test` 确认全项目通过

# Task Dependencies

- Task 2 依赖 Task 1（需要协议类型定义）
- Task 3 依赖 Task 2（需要 `get_server_statuses()` 方法）
- Task 4 依赖 Task 1 和 Task 3（需要协议类型和 Python 端处理器）
- Task 5 依赖 Task 1-4（需要全部实现完成）
- Task 1 和 Task 2 可部分并行（2.1/2.2 不依赖协议类型，但 2.3 测试可能需要）
