# MCP 服务器添加与删除 Spec

## Why

当前 `/mcp` 仅支持状态查看，用户必须手动编辑 `~/.harness/config.toml` 并重启才能增删 MCP 服务器。claude-code 提供 `claude mcp add/remove`，opencode 提供 `opencode mcp add` + HTTP API，均支持运行时管理。本次变更的目标是：**让用户在 TUI 中通过 `/mcp add` 和 `/mcp remove` 子命令动态增删 MCP 服务器，配置持久化到用户 TOML 文件，并尝试热连接新服务器**。

## What Changes

- 修改 `packages/protocol/schema/v2.json`：新增 `mcp.add`、`mcp.remove` 客户端方法，协议 minor 升至 6
- 修改 `packages/protocol/scripts/generate.ts`：新增 `McpAddParams`、`McpAddResult`、`McpRemoveParams`、`McpRemoveResult` 接口
- 修改 `packages/protocol/src/index.ts`：新增 `Method.MCP_ADD`、`Method.MCP_REMOVE` 和对应断言
- 修改 `packages/agent/pyproject.toml`：新增 `tomli-w` 依赖用于 TOML 写入
- 修改 `packages/agent/harness_agent/mcp.py`：`McpConnectionManager` 新增 `add_server()` 和 `remove_server()` 方法
- 新增 `packages/agent/harness_agent/mcp_config_writer.py`：TOML 配置文件读写（添加/删除 `[[mcp.servers]]` 条目）
- 修改 `packages/agent/harness_agent/server.py`：新增 `_handle_mcp_add` 和 `_handle_mcp_remove` 处理器
- 修改 `packages/cli/src/tui/app.tsx`：`/mcp` 命令支持 `add` 和 `remove` 子命令解析
- 修改 `packages/cli/src/ipc/client.ts`：新增 `mcpAdd()` 和 `mcpRemove()` 便捷方法
- 更新测试和文档

## Impact

- Affected specs: 协议层（新增方法）、Agent 服务端（新增处理器 + 配置写入）、CLI 表现层（子命令解析）
- Affected code:
  - `packages/protocol/schema/v2.json`、`generated.ts`（生成）、`index.ts`
  - `packages/agent/pyproject.toml`
  - `packages/agent/harness_agent/mcp.py`
  - `packages/agent/harness_agent/mcp_config_writer.py`（新增）
  - `packages/agent/harness_agent/server.py`
  - `packages/cli/src/tui/app.tsx`
  - `packages/cli/src/ipc/client.ts`
  - 测试文件、`docs/user/交互使用.md`

## ADDED Requirements

### Requirement: MCP 服务器添加协议

系统 SHALL 提供 `mcp.add` JSON-RPC 方法，将新 MCP 服务器写入用户配置并尝试热连接。

请求参数（`McpAddParams`）：

```typescript
interface McpAddParams {
  name: string
  transport: "stdio" | "http" | "sse"
  command?: string
  args?: string[]
  url?: string
  env?: Record<string, string>
  headers?: Record<string, string>
}
```

响应结果（`McpAddResult`）：

```typescript
interface McpAddResult {
  added: boolean
  connected: boolean
  tool_names: string[]
  error?: string
}
```

#### Scenario: 添加 stdio 服务器
- **WHEN** CLI 发送 `mcp.add`，`transport: "stdio"`，`command: "npx"`，`args: ["-y", "@modelcontextprotocol/server-filesystem"]`
- **THEN** 系统将配置追加到 `~/.harness/config.toml` 的 `[[mcp.servers]]`，尝试连接，返回 `added: true` 和连接结果

#### Scenario: 添加 http 服务器
- **WHEN** `transport: "http"`，`url: "http://localhost:3001/mcp"`
- **THEN** 同上，配置中写入 `transport = "http"` 和 `url`

#### Scenario: 名称重复
- **WHEN** 配置中已存在同名服务器
- **THEN** 返回 `-32602` 错误，提示服务器已存在

#### Scenario: 名称非法
- **WHEN** 名称包含空格或特殊字符（不匹配 `[a-zA-Z0-9_-]`）
- **THEN** 返回 `-32602` 错误

#### Scenario: 热连接失败
- **WHEN** 配置写入成功但连接失败
- **THEN** 返回 `added: true, connected: false, error: "..."`，配置已持久化，重启后自动重试

### Requirement: MCP 服务器删除协议

系统 SHALL 提供 `mcp.remove` JSON-RPC 方法，从用户配置中删除指定 MCP 服务器。

请求参数（`McpRemoveParams`）：`{ name: string }`

响应结果（`McpRemoveResult`）：`{ removed: boolean }`

#### Scenario: 删除已存在的服务器
- **WHEN** 配置中存在该名称的服务器
- **THEN** 从 TOML 文件中移除对应 `[[mcp.servers]]` 条目，从运行时工具列表中移除该服务器的工具，返回 `removed: true`

#### Scenario: 删除不存在的服务器
- **WHEN** 配置中不存在该名称
- **THEN** 返回 `-32602` 错误，提示服务器不存在

### Requirement: TOML 配置写入

系统 SHALL 将 MCP 服务器配置持久化到用户级 TOML 文件（`~/.harness/config.toml`）。

#### Scenario: 文件不存在
- **WHEN** `~/.harness/config.toml` 不存在
- **THEN** 创建文件（含父目录），写入 `[[mcp.servers]]` 条目

#### Scenario: 文件已存在
- **WHEN** 文件已存在且包含其他配置
- **THEN** 保留原有配置，仅追加/删除 `[[mcp.servers]]` 条目

#### Scenario: 写入原子性
- **WHEN** 写入配置
- **THEN** 先写临时文件再原子替换，避免写入中断导致配置损坏

### Requirement: /mcp add 和 /mcp remove 子命令

TUI 中 `/mcp` 命令 SHALL 支持 `add` 和 `remove` 子命令。

语法：
- `/mcp add <name> <command> [args...]` — 添加 stdio 服务器
- `/mcp add <name> --url <url>` — 添加 http 服务器（自动检测）
- `/mcp add <name> --url <url> --sse` — 添加 sse 服务器
- `/mcp remove <name>` — 删除服务器
- `/mcp` — 显示状态（已有）

#### Scenario: 添加 stdio 服务器
- **WHEN** 用户输入 `/mcp add filesystem npx -y @modelcontextprotocol/server-filesystem /tmp`
- **THEN** 解析为 `name=filesystem, transport=stdio, command=npx, args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]`，调用 `mcp.add`，显示结果

#### Scenario: 添加 http 服务器
- **WHEN** 用户输入 `/mcp add github --url http://localhost:3001/mcp`
- **THEN** 解析为 `name=github, transport=http, url=http://localhost:3001/mcp`

#### Scenario: 删除服务器
- **WHEN** 用户输入 `/mcp remove filesystem`
- **THEN** 调用 `mcp.remove`，显示删除结果

#### Scenario: 参数不足
- **WHEN** 用户输入 `/mcp add` 或 `/mcp remove`（缺少参数）
- **THEN** 显示用法提示

### Requirement: 热连接与热断开

`McpConnectionManager` SHALL 支持运行时添加和移除单个服务器。

#### Scenario: 热连接新服务器
- **WHEN** 调用 `add_server(config)` 且服务器可达
- **THEN** 建立连接，将新工具合并到 `self._tools`，更新 `_server_statuses`

#### Scenario: 热断开服务器
- **WHEN** 调用 `remove_server(name)`
- **THEN** 从 `self._tools` 中移除该服务器前缀的工具，更新 `_server_statuses` 为 `removed`

## MODIFIED Requirements

### Requirement: 协议版本

`PROTOCOL_MINOR` 从 `5` 升至 `6`，`client_methods` 新增 `"mcp.add"` 和 `"mcp.remove"`。

## REMOVED Requirements

无。
