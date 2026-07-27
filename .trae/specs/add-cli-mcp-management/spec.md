# CLI 端 MCP 管理 Spec

## Why

Agent 端 MCP 集成已完成（配置解析、连接管理、工具加载、审批），但 CLI 端完全无法感知 MCP 状态：没有 `/mcp` 斜杠命令、协议层无 MCP 查询方法、`/status` 不显示 MCP 信息、`createTuiRuntime` 忽略 `config_summary.mcp_servers`。用户无法在 TUI 中查看 MCP 服务器连接状态和已加载工具，也无法排查连接问题。参考 opencode（`/mcps` 对话框 + 侧边栏状态）和 claude-code（`/mcp` 设置界面 + `mcp list` 健康检查），本次变更的目标是：**让 CLI 端能查看 MCP 服务器连接状态、已加载工具列表，并在 `/status` 中展示 MCP 摘要**。

## What Changes

- 修改 `packages/protocol/schema/v2.json`：新增 `mcp.status` 客户端方法和 `mcp.read` 服务端能力，协议 minor 版本升至 5
- 修改 `packages/protocol/src/generated.ts`（通过生成脚本）：新增 `McpStatusParams`、`McpStatusResult`、`McpServerStatus` 类型
- 修改 `packages/protocol/src/index.ts`：新增 `Method.MCP_STATUS` 常量和 `assertMcpStatusParams` 断言
- 修改 `packages/agent/harness_agent/mcp.py`：`McpConnectionManager` 增加逐服务器状态跟踪（`get_server_statuses()`）
- 修改 `packages/agent/harness_agent/server.py`：新增 `_handle_mcp_status` 处理器
- 修改 `packages/agent/harness_agent/protocol_generated.py`（通过生成脚本）：新增对应 Pydantic 模型
- 修改 `packages/cli/src/tui/commands.ts`：注册 `/mcp` 斜杠命令
- 修改 `packages/cli/src/tui/app.tsx`：实现 `/mcp` 命令执行逻辑（渲染 MCP 状态面板）
- 修改 `packages/cli/src/tui/model.ts`：`TuiRuntime` 提取 MCP 配置摘要，`/status` 输出包含 MCP 信息
- 修改 `packages/cli/src/ipc/client.ts`：新增 `mcpStatus()` 便捷方法
- 更新测试：Python 端 `test_mcp.py`、TypeScript 端 `commands.test.ts`、`app-interaction.test.ts`
- 更新文档：`docs/user/交互使用.md`

## Impact

- Affected specs: 协议层（新增方法）、Agent 服务端（新增处理器）、CLI 表现层（新增命令和面板）
- Affected code:
  - `packages/protocol/schema/v2.json`
  - `packages/protocol/src/generated.ts`（生成）
  - `packages/protocol/src/index.ts`
  - `packages/agent/harness_agent/mcp.py`
  - `packages/agent/harness_agent/server.py`
  - `packages/agent/harness_agent/protocol_generated.py`（生成）
  - `packages/cli/src/tui/commands.ts`
  - `packages/cli/src/tui/app.tsx`
  - `packages/cli/src/tui/model.ts`
  - `packages/cli/src/ipc/client.ts`
  - `packages/cli/tests/tui/commands.test.ts`
  - `packages/cli/tests/tui/app-interaction.test.ts`
  - `packages/agent/tests/test_mcp.py`
  - `docs/user/交互使用.md`

## ADDED Requirements

### Requirement: MCP 状态查询协议

系统 SHALL 提供 `mcp.status` JSON-RPC 方法，允许 CLI 查询所有已配置 MCP 服务器的运行时状态。

请求参数（`McpStatusParams`）：空对象 `{}`。

响应结果（`McpStatusResult`）：

```typescript
interface McpServerStatus {
  name: string
  transport: "stdio" | "http" | "sse"
  status: "connected" | "failed" | "skipped"
  error?: string
  tool_names: string[]
}

interface McpStatusResult {
  servers: McpServerStatus[]
  total_tools: number
}
```

#### Scenario: 有已连接的 MCP 服务器
- **WHEN** CLI 发送 `mcp.status` 请求，且有 2 个服务器成功连接、共加载 5 个工具
- **THEN** 返回 `servers` 数组包含 2 个 `status: "connected"` 条目，`total_tools: 5`，每个条目的 `tool_names` 列出该服务器的工具名

#### Scenario: 有连接失败的服务器
- **WHEN** 某服务器连接超时或握手失败
- **THEN** 该服务器条目 `status: "failed"`，`error` 包含失败原因摘要，`tool_names` 为空

#### Scenario: 环境变量缺失被跳过
- **WHEN** 某服务器的 `${VAR}` 环境变量未设置
- **THEN** 该服务器条目 `status: "skipped"`，`error` 说明缺失的变量名

#### Scenario: 无 MCP 配置
- **WHEN** 用户未配置任何 MCP 服务器
- **THEN** 返回 `servers: []`，`total_tools: 0`

#### Scenario: 未初始化时调用
- **WHEN** CLI 在 `initialize` 完成前发送 `mcp.status`
- **THEN** 返回 `-32002` 错误（与现有未初始化保护一致）

### Requirement: McpConnectionManager 逐服务器状态

`McpConnectionManager` SHALL 跟踪每个服务器的独立连接状态，而非仅全局 `connected` 布尔值。

#### Scenario: 部分服务器失败
- **WHEN** 配置了 3 个服务器，其中 1 个连接失败
- **THEN** `get_server_statuses()` 返回 3 个条目：2 个 `connected`、1 个 `failed`；成功的服务器工具正常可用

#### Scenario: 工具归属
- **WHEN** 多个服务器连接成功
- **THEN** 每个服务器的 `tool_names` 仅包含该服务器提供的工具（通过工具名前缀 `{server_name}_` 匹配）

### Requirement: /mcp 斜杠命令

系统 SHALL 在 TUI 中提供 `/mcp` 斜杠命令，显示 MCP 服务器状态面板。

#### Scenario: 查看 MCP 状态
- **WHEN** 用户输入 `/mcp` 并回车
- **THEN** TUI 向 sidecar 发送 `mcp.status` 请求，在时间线中追加一个状态面板，显示：
  - 每个服务器的名称、传输类型、连接状态（带颜色标记）
  - 已连接服务器的工具列表
  - 失败服务器的错误信息
  - 底部汇总：N 个服务器、M 个工具

#### Scenario: 无 MCP 配置
- **WHEN** 用户输入 `/mcp`，但未配置任何 MCP 服务器
- **THEN** 显示提示信息"未配置 MCP 服务器"及配置方法引导（指向 `~/.harness/config.toml`）

#### Scenario: 请求失败
- **WHEN** `mcp.status` 请求超时或 sidecar 返回错误
- **THEN** 在时间线中显示错误通知，不阻塞 TUI

### Requirement: /status 包含 MCP 摘要

`/status` 命令的输出 SHALL 包含 MCP 连接摘要行。

#### Scenario: 有 MCP 服务器
- **WHEN** 用户执行 `/status`，且有 2 个 MCP 服务器（1 个连接、1 个失败）
- **THEN** 输出包含 `MCP: 1/2 connected, 3 tools` 格式的摘要行

#### Scenario: 无 MCP 服务器
- **WHEN** 未配置 MCP 服务器
- **THEN** 输出包含 `MCP: not configured`

## MODIFIED Requirements

### Requirement: 协议版本

`PROTOCOL_MINOR` 从 `4` 升至 `5`，`client_methods` 新增 `"mcp.status"`，`server_capabilities` 新增 `"mcp.read"`。

### Requirement: createTuiRuntime MCP 信息提取

`createTuiRuntime` 从 `InitializeResult.config_summary.mcp_servers` 提取 MCP 配置摘要（服务器数量和名称），存入 `TuiRuntime.mcpSummary` 字段，供 `/status` 使用。

### Requirement: 斜杠命令注册

`SlashCommandName` 联合类型新增 `"mcp"`，`slashCommandDefinitions` 新增 `{ name: "mcp", description: "查看 MCP 服务器状态" }`。

## REMOVED Requirements

无。
