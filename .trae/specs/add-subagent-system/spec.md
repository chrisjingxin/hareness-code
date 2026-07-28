# Subagent 能力补全方案 Spec

## Why

当前项目基于 deepagents 0.6.8 的 `create_deep_agent(subagents=...)` 接口，仅注册了一个 `GENERAL_PURPOSE_SUBAGENT`，缺乏自定义 agent 定义、管理命令、工具作用域控制、agent 记忆、颜色标识等上层框架能力。竞品（Claude Code、OpenCode、Grok-Build、Qwen-Code）均已实现完整的 subagent 生态，包括 Markdown 定义文件、/agents CRUD 命令、权限过滤、后台执行、fork 模式等。本方案在复用 deepagents 已有接口的前提下，补全上层框架能力。

## 竞品调研摘要

| 维度 | Claude Code | OpenCode | Grok-Build | Qwen-Code | DeepAgent(教学) |
|------|-------------|----------|------------|-----------|-----------------|
| 定义格式 | Markdown+YAML | Markdown+YAML / JSON | Markdown+YAML | Markdown+YAML | Python TypedDict |
| 存储层级 | user/project/policy | global/project | project/user/bundled/builtin | session/project/user/extension/builtin | 代码内 |
| 工具控制 | tools白名单+disallowedTools黑名单+三层过滤 | permission ruleset(allow/deny/ask) | capability mode + tools/disallowed_tools | tools白名单+disallowedTools+排除集 | tools字段精确选取 |
| 触发方式 | 模型自动+fork | 模型自动+@提及 | 模型自动+orchestrator强制 | 模型自动+显式提及 | 模型自动 |
| 执行模式 | 前台/后台/自动后台化(120s) | 前台/后台(实验性)/promotion | 前台(600s预算)/后台 | 前台/后台/fork | 同步 |
| 深度限制 | 1层 | 可配置(默认1) | 1层 | AsyncLocalStorage追踪 | 无(教学) |
| 记忆 | user/project/local三scope MEMORY.md | 无独立记忆 | memory_config继承 | 无独立记忆 | files状态共享 |
| 颜色 | 8色枚举 | #RRGGBB/主题色/哈希分配 | 8色枚举 | 8色+auto | 无 |
| 管理命令 | /agents 交互式菜单 | CLI agent create/list | /agents modal+编辑器 | /agents create/manage 6步向导 | 无 |
| 编排 | 并行tool_use+teammate/swarm | 并行+后台inject | Coordinator Actor+并行 | 并行+fork+team | 并行tool调用 |
| 隔离 | worktree | 子会话 | worktree | worktree | 上下文隔离 |

## What Changes

- 新增 agent 定义格式：Markdown + YAML frontmatter，存放于 `.harness/agents/` 和 `~/.harness/agents/`
- 新增 agent 发现与加载模块：按优先级 project > user > builtin 加载
- 新增内置 agent 类型：`general-purpose`（已有）、`explore`（只读搜索）、`plan`（只读规划）
- 扩展 `create_harness_agent()` 的 subagents 参数，支持多 agent 注册
- 新增工具作用域过滤：tools 白名单 + disallowed_tools 黑名单
- 新增 agent 专属 system prompt 装配（替代/追加默认 prompt）
- 新增 JSON-RPC 方法 `agents.list` / `agents.create` / `agents.update` / `agents.remove`
- CLI 新增 `/agents` 斜杠命令（列表、创建、查看、编辑、删除）
- 新增 agent 颜色标识（8 色枚举）
- 新增 subagent 深度限制（默认 1 层）
- 新增 subagent 事件通知（`source.kind: "subagent"`）
- 新增 task 工具描述动态生成（列出可用 agent 类型及描述）

## Impact

- Affected specs: agent 执行、工具系统、JSON-RPC 协议、CLI 命令
- Affected code:
  - `packages/agent/harness_agent/agent.py` — subagents 参数扩展
  - `packages/agent/harness_agent/` — 新增 `subagents.py`（定义、发现、加载、过滤）
  - `packages/agent/harness_agent/server.py` — 新增 agents.* RPC 方法
  - `packages/protocol/` — 新增 agents 相关类型
  - `packages/cli/` — 新增 /agents 命令和 subagent 状态显示

## ADDED Requirements

### Requirement: Agent 定义格式
系统 SHALL 支持 Markdown + YAML frontmatter 格式的 agent 定义文件。

Frontmatter 字段：
- `name` (string, 必填): 唯一标识
- `description` (string, 必填): 能力描述（给父 agent 看）
- `tools` (string[], 可选): 工具白名单，省略则继承全部
- `disallowedTools` (string[], 可选): 工具黑名单
- `model` (string, 可选): 模型覆盖，`inherit` 表示继承父级
- `color` (enum, 可选): red/blue/green/yellow/purple/orange/pink/cyan
- `maxTurns` (int, 可选): 最大推理轮次
- `background` (bool, 可选): 是否始终后台运行（v0.2 预留）

Markdown 正文为 agent 的 system prompt。

#### Scenario: 项目级 agent 定义
- **WHEN** 用户在 `.harness/agents/code-reviewer.md` 创建定义文件
- **THEN** 系统加载该 agent 并注册为可用 subagent 类型

#### Scenario: 用户级 agent 定义
- **WHEN** 用户在 `~/.harness/agents/researcher.md` 创建定义文件
- **THEN** 系统在所有项目中加载该 agent

### Requirement: Agent 发现与优先级
系统 SHALL 按 project > user > builtin 优先级加载 agent 定义，同名时高优先级覆盖低优先级。

#### Scenario: 同名覆盖
- **WHEN** project 和 user 目录均存在 `explorer.md`
- **THEN** 使用 project 级定义

### Requirement: 内置 Agent 类型
系统 SHALL 提供以下内置 agent：
- `general-purpose`: 全能子代理（已有，保持）
- `explore`: 只读搜索专家，仅允许 read_file/grep/glob/ls/execute(只读命令)
- `plan`: 架构规划师，仅允许 read_file/grep/glob/ls + write_todos

#### Scenario: explore agent 工具限制
- **WHEN** 父 agent 委派任务给 explore 类型
- **THEN** 子代理只能使用只读工具，无法写入文件或执行修改命令

### Requirement: 工具作用域过滤
系统 SHALL 支持通过 tools/disallowedTools 字段过滤子代理可用工具集。

#### Scenario: 白名单模式
- **WHEN** agent 定义 `tools: [read_file, grep, glob]`
- **THEN** 子代理仅能调用这三个工具

#### Scenario: 黑名单模式
- **WHEN** agent 定义 `disallowedTools: [execute, write_file]`
- **THEN** 子代理不能调用 execute 和 write_file

### Requirement: Subagent 深度限制
系统 SHALL 限制 subagent 嵌套深度为 1 层（子代理不能再启动子代理）。

#### Scenario: 深度超限
- **WHEN** 子代理尝试调用 task 工具
- **THEN** 返回错误 "Subagent depth limit reached"

### Requirement: Agent 管理 RPC
系统 SHALL 提供 JSON-RPC 方法管理 agent 定义。

#### Scenario: 列出 agents
- **WHEN** 客户端调用 `agents.list`
- **THEN** 返回所有已加载 agent 的 name/description/color/source/tools

#### Scenario: 创建 agent
- **WHEN** 客户端调用 `agents.create` 携带 name/description/prompt/tools/color
- **THEN** 在指定层级目录创建 .md 文件并热加载

#### Scenario: 删除 agent
- **WHEN** 客户端调用 `agents.remove` 携带 name
- **THEN** 删除对应 .md 文件（builtin 不可删除）

### Requirement: CLI /agents 命令
系统 SHALL 在 TUI 中提供 `/agents` 斜杠命令。

#### Scenario: 列表展示
- **WHEN** 用户输入 `/agents`
- **THEN** 显示所有 agent 列表（名称、描述、颜色标记、来源）

#### Scenario: 创建向导
- **WHEN** 用户选择创建新 agent
- **THEN** 引导输入名称、描述、选择工具、选择颜色，生成 .md 文件

### Requirement: Subagent 事件通知
系统 SHALL 在 subagent 执行期间通过 event notification 报告进度。

#### Scenario: 子代理工具调用
- **WHEN** 子代理执行工具调用
- **THEN** 发送 `source: {kind: "subagent", agent_type, agent_name}` 的事件

### Requirement: Task 工具描述动态生成
系统 SHALL 在 task 工具描述中动态列出所有可用 subagent 类型及其描述。

#### Scenario: 自定义 agent 出现在工具描述中
- **WHEN** 用户定义了 `code-reviewer` agent
- **THEN** 父 agent 的 task 工具描述包含 "code-reviewer: Reviews code for best practices"

## MODIFIED Requirements

### Requirement: create_harness_agent 子代理注册
当前 `_create_default_subagents()` 仅返回一个 GENERAL_PURPOSE_SUBAGENT。修改为：
- 加载所有已发现的 agent 定义
- 为每个 agent 构建 deepagents 兼容的 subagent dict（name/description/system_prompt/tools/middleware）
- 注入深度限制中间件（阻止子代理调用 task 工具）
- 注入工具作用域过滤

## REMOVED Requirements

无。
