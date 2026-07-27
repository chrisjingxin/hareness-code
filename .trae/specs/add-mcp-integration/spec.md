# MCP 集成 Spec

## Why

Harness Code 当前无法连接外部 MCP Server，缺少工具扩展能力。所有主流竞品（Claude Code、Codex、OpenCode、Grok Build）均已支持 MCP。项目已预留完整接口（`mcp_server_info` 参数、`RuntimeProfile.mcp_config_fingerprint`、`RuntimeResourceBundle.mcp_resources`、`langchain-mcp-adapters==0.2.1` 依赖），但无任何实际实现。本次变更的目标是：**让 Harness Code 能连接外部 MCP Server（stdio 和 HTTP/SSE），发现工具、注册到 Agent、成功调用并拿到响应**。

## What Changes

- 新增 `harness_agent/mcp.py` 模块：MCP 配置解析、连接管理、工具加载、生命周期
- 修改 `harness_agent/config.py`：激活 `[mcp]` TOML 配置区段，解析服务器定义
- 修改 `harness_agent/config_manifest.py`：将 `mcp` 区段状态从 `planned` 改为 `active`
- 修改 `harness_agent/agent.py`：在 `create_harness_agent` 中接收 MCP 工具并注入 Agent
- 修改 `harness_agent/server.py`：在 sidecar 启动时建立 MCP 连接，关闭时释放
- 修改 `harness_agent/runtime_profile.py`：用实际 MCP 配置生成指纹（替代固定 "disabled"）
- 修改 `harness_agent/approval_policy.py`：MCP 工具纳入 HITL 审批（default 模式需确认）
- 新增测试 `tests/test_mcp.py`：覆盖配置解析、工具加载、调用流程、错误处理

## Impact

- Affected specs: 工具调用、上下文工程（MCP 工具结果进入上下文）、安全控制（MCP 工具审批）
- Affected code:
  - `packages/agent/harness_agent/mcp.py`（新增）
  - `packages/agent/harness_agent/config.py`
  - `packages/agent/harness_agent/config_manifest.py`
  - `packages/agent/harness_agent/agent.py`
  - `packages/agent/harness_agent/server.py`
  - `packages/agent/harness_agent/runtime_profile.py`
  - `packages/agent/harness_agent/approval_policy.py`
  - `packages/agent/tests/test_mcp.py`（新增）

## ADDED Requirements

### Requirement: MCP 配置解析

系统 SHALL 支持在用户级 TOML 配置（`~/.harness/config.toml`）中定义 MCP 服务器。

配置格式：

```toml
[[mcp.servers]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

[mcp.servers.env]
API_KEY = "xxx"

[[mcp.servers]]
name = "github"
transport = "http"
url = "http://localhost:3001/mcp"

[mcp.servers.headers]
Authorization = "Bearer ${GITHUB_TOKEN}"

[[mcp.servers]]
name = "legacy-sse"
transport = "sse"
url = "http://localhost:8000/sse"
```

#### Scenario: 有效的 stdio 配置
- **WHEN** 用户配置了 `transport = "stdio"` 的服务器，包含 `command` 和 `args`
- **THEN** 系统解析为 `StdioConnection` 配置，等待连接阶段使用

#### Scenario: 有效的 HTTP 配置
- **WHEN** 用户配置了 `transport = "http"` 的服务器，包含 `url`
- **THEN** 系统解析为 `StreamableHttpConnection` 配置

#### Scenario: 有效的 SSE 配置
- **WHEN** 用户配置了 `transport = "sse"` 的服务器，包含 `url`
- **THEN** 系统解析为 `SSEConnection` 配置

#### Scenario: 无效配置
- **WHEN** 配置缺少必填字段（stdio 缺 command、http/sse 缺 url）
- **THEN** 系统记录警告日志并跳过该服务器，不阻止 sidecar 启动

#### Scenario: 环境变量引用
- **WHEN** 配置值包含 `${ENV_VAR}` 语法
- **THEN** 系统在连接时从环境变量解析实际值；变量缺失时记录警告并跳过该服务器

### Requirement: MCP 连接管理

系统 SHALL 在 sidecar 启动后、首次 `run.start` 前建立所有已配置 MCP 服务器的连接。

#### Scenario: 成功连接
- **WHEN** MCP 服务器可达且握手成功
- **THEN** 系统获取工具列表，将工具注册到 Agent 的可用工具集中

#### Scenario: 连接失败
- **WHEN** MCP 服务器不可达或握手超时（默认 30 秒）
- **THEN** 系统记录 stderr 警告，跳过该服务器，不阻止 Agent 正常运行

#### Scenario: 连接关闭
- **WHEN** sidecar 收到 `shutdown` 请求或进程退出
- **THEN** 系统按 `RuntimeResourceBundle.mcp_resources` 顺序关闭所有 MCP 连接/子进程

#### Scenario: stdio 子进程清理
- **WHEN** 关闭 stdio 类型的 MCP 连接
- **THEN** 系统终止子进程（SIGTERM → 等待 → SIGKILL）

### Requirement: MCP 工具发现与注册

系统 SHALL 将 MCP 服务器暴露的工具转换为 LangChain `BaseTool` 并注入 Agent。

#### Scenario: 工具发现
- **WHEN** 连接建立后
- **THEN** 系统调用 MCP `tools/list` 获取工具列表，通过 `langchain-mcp-adapters` 转换为 `BaseTool`

#### Scenario: 工具命名
- **WHEN** 多个 MCP 服务器暴露同名工具
- **THEN** 工具名自动添加服务器前缀（`{server_name}_{tool_name}`），避免冲突

#### Scenario: 工具注入 Agent
- **WHEN** MCP 工具加载完成
- **THEN** 工具通过 `create_harness_agent(tools=[...mcp_tools])` 注入，与内置工具并列

#### Scenario: 工具 Schema 进入 PromptEpoch
- **WHEN** 创建新 thread 的 PromptEpoch
- **THEN** MCP 工具的 schema 参与 `tool_fingerprint` 计算，影响 Runtime Profile

### Requirement: MCP 工具调用

系统 SHALL 支持 Agent 在对话中调用 MCP 工具并获取响应。

#### Scenario: 成功调用
- **WHEN** Agent 决定调用 MCP 工具（如 `github_search_repositories`）
- **THEN** 系统通过 MCP 协议发送 `tools/call`，将结果转为 `ToolMessage` 返回给 Agent

#### Scenario: 工具执行错误
- **WHEN** MCP 服务器返回 `isError: true`
- **THEN** 系统将错误信息作为 `ToolMessage(status="error")` 返回，Agent 可自行纠错

#### Scenario: 调用超时
- **WHEN** MCP 工具调用超过 60 秒无响应
- **THEN** 系统取消调用，返回超时错误 `ToolMessage`

#### Scenario: 大输出截断
- **WHEN** MCP 工具返回超过 1MB 的输出
- **THEN** 系统按现有 `_truncate_text` 逻辑截断，与内置工具一致

### Requirement: MCP 工具审批

系统 SHALL 将 MCP 工具纳入现有审批策略。

#### Scenario: default 模式
- **WHEN** 审批模式为 `default`，Agent 调用 MCP 工具
- **THEN** 系统向 TUI 发送审批请求，用户确认后执行

#### Scenario: yolo 模式
- **WHEN** 审批模式为 `yolo`
- **THEN** MCP 工具自动执行，无需审批

#### Scenario: plan 模式
- **WHEN** 审批模式为 `plan`
- **THEN** MCP 工具被 PlanModeMiddleware 拒绝（与 execute、write_file 一致）

### Requirement: MCP 配置指纹

系统 SHALL 将 MCP 配置纳入 Runtime Profile 指纹计算。

#### Scenario: 配置变化触发新 Runtime
- **WHEN** 用户修改 MCP 服务器配置（增删服务器、修改 transport）
- **THEN** `mcp_config_fingerprint` 变化，RuntimePool 构建新 Runtime

#### Scenario: 脱敏指纹
- **WHEN** 计算 MCP 配置指纹
- **THEN** 只包含服务器名称、transport 类型、工具过滤规则；不包含命令路径、URL、凭据

## MODIFIED Requirements

### Requirement: config_manifest mcp 区段

`ConfigManifest.SECTIONS["mcp"]` 状态从 `"planned"` 改为 `"active"`，`allowed_sources` 设为 `frozenset({"user", "explicit"})`（用户级和显式配置允许，项目级暂不允许）。

### Requirement: create_harness_agent 工具注入

`create_harness_agent` 的 `tools` 参数现在接受 MCP 工具。`mcp_server_info` 参数保留但不在本次使用（连接管理在 server 层完成）。

### Requirement: RuntimeProfile 指纹

`default_runtime_profile()` 的 `mcp_config_fingerprint` 从固定 `{"transport": "disabled"}` 改为根据实际 MCP 配置计算。无 MCP 配置时保持原值。

## REMOVED Requirements

无。
