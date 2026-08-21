# 子代理（Subagent）

子代理是由主代理通过 `task` 工具委派的独立执行单元。每个子代理拥有独立的上下文窗口、工具集和系统提示词，执行完毕后向主代理返回单一结果。

使用子代理的核心收益：

- **上下文隔离**：子代理的中间推理和工具输出不会污染主代理的上下文窗口。
- **专注任务**：每个子代理只关注单一目标，减少多任务切换带来的质量下降。
- **节省 token**：搜索、规划等只读任务使用精简工具集，避免加载不必要的 schema 和历史。

## 内置子代理类型

| 名称 | 用途 | 工具范围 |
| --- | --- | --- |
| `general-purpose` | 通用多步骤任务，拥有与主代理相同的工具集（不含 `task`） | 全部（自动排除项除外） |
| `explore` | 只读代码搜索，快速定位文件和代码内容 | `ls`、`read_file`、`glob`、`grep`、`execute`、`web_search`、`web_fetch`、`memory_search` |
| `plan` | 架构规划，分析代码结构并制定实施计划 | `ls`、`read_file`、`glob`、`grep`、`write_todos`、`memory_search` |

主代理会根据任务性质自动选择合适的子代理类型。你也可以在提示词中明确指定，例如"使用 explore 子代理搜索所有 TODO 注释"。

## 自定义 Agent

### 文件格式

自定义 Agent 使用 Markdown 文件定义，结构为 YAML frontmatter 元数据 + Markdown 正文（作为系统提示词）：

```markdown
---
name: code-reviewer
description: 代码审查专家，检查代码质量、安全漏洞和最佳实践
tools:
  - read_file
  - glob
  - grep
  - ls
color: orange
maxTurns: 20
---

你是一个严格的代码审查专家。

规则：
- 逐文件审查，关注安全漏洞、性能问题和可维护性
- 对每个发现给出严重级别（critical/warning/info）
- 最终输出结构化的审查报告
```

### 存放位置

| 级别 | 路径 | 说明 |
| --- | --- | --- |
| 项目级 | `<workspace>/.harness/agents/<name>.md` | 跟随项目仓库，团队共享 |
| 用户级 | `~/.harness/agents/<name>.md` | 个人全局可用 |

同名 Agent 的覆盖优先级为：项目级 > 用户级 > 内置。目录不存在时静默跳过；解析失败的文件记录警告并跳过，不影响其他 Agent 加载。

### 字段说明

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `name` | 是 | string | kebab-case 标识符，例如 `code-reviewer` |
| `description` | 是 | string | 一句话描述，主代理据此选择委派目标 |
| `tools` | 否 | string[] | 工具白名单；不填则继承全部可用工具 |
| `disallowedTools` | 否 | string[] | 工具黑名单，从白名单或全量中排除 |
| `model` | 否 | string | 指定模型 Profile ID；不填则使用当前 Run 的模型 |
| `color` | 否 | string | 界面标识颜色：`red`/`blue`/`green`/`yellow`/`purple`/`orange`/`pink`/`cyan` |
| `maxTurns` | 否 | integer | 最大执行轮次；不填则无限制 |
| `background` | 否 | boolean | 是否后台执行（默认 `false`） |

Markdown 正文即系统提示词，支持完整的 Markdown 语法。

### 完整示例

在项目中创建 `.harness/agents/code-reviewer.md`：

```markdown
---
name: code-reviewer
description: 代码审查专家，检查代码质量、安全漏洞和最佳实践
tools:
  - read_file
  - glob
  - grep
  - ls
  - execute
disallowedTools:
  - execute
color: orange
maxTurns: 20
---

你是一个严格的代码审查专家。你的任务是审查指定范围的代码变更。

## 审查维度

1. **安全性**：注入、越权、敏感信息泄露
2. **正确性**：边界条件、并发、资源泄漏
3. **可维护性**：命名、复杂度、重复代码

## 输出格式

对每个发现输出：
- 文件路径和行号
- 严重级别：critical / warning / info
- 问题描述和修复建议
```

## /agents 命令

交互式运行中输入 `/agents` 打开 Agent 管理界面，支持：

- **列表**：查看所有已注册 Agent（内置 + 自定义），显示名称、描述、来源和工具数。
- **创建**：通过引导流程创建新的自定义 Agent 文件。
- **查看**：查看 Agent 的完整定义，包括系统提示词。
- **删除**：移除自定义 Agent 文件（内置 Agent 不可删除）。

## 工具控制

### 白名单与黑名单

- `tools` 字段指定白名单：子代理只能使用列出的工具。
- `disallowedTools` 字段指定黑名单：从可用集中排除。
- 两者可同时使用：先应用白名单确定候选集，再移除黑名单项。
- 都不填时继承主代理的全部工具。

### 自动排除的工具

无论白名单/黑名单如何配置，以下工具始终从子代理中移除：

| 工具 | 排除原因 |
| --- | --- |
| `task` | 防止子代理嵌套委派（深度限制） |
| `enter_plan_mode` | 防止子代理切换主代理的执行模式 |
| `exit_plan_mode` | 同上 |
| `ask_user` | 子代理不能中断用户交互 |

## 深度限制

子代理不能再派生子代理。`task` 工具被强制排除，因此委派深度固定为 1 层：主代理 → 子代理。这避免了递归委派导致的资源失控和上下文膨胀。

## 审批继承

子代理继承父级的审批模式。如果主代理运行在 `plan` 模式下，子代理的工具调用同样受 `PlanModeMiddleware` 约束；`WorkspaceBoundaryMiddleware` 也在子代理的中间件栈中独立注册，确保文件操作不能逃逸工作区边界。

## 可用工具列表

当前项目注册的全部工具（19 个）：

| 工具 | 说明 |
| --- | --- |
| `ls` | 列出目录内容 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `edit_file` | 编辑文件 |
| `glob` | 文件名模式匹配 |
| `grep` | 文件内容搜索 |
| `execute` | 执行 shell 命令 |
| `write_todos` | 写入任务列表 |
| `task` | 委派子代理任务 |
| `web_search` | 网络搜索 |
| `web_fetch` | 获取网页内容 |
| `delete_file` | 删除文件 |
| `lsp` | 语言服务协议操作 |
| `tool_search` | 搜索已连接的 MCP 外部工具（`select:name1,name2` 精确选择或关键词搜索，返回参数 schema） |
| `memory_save` | 保存记忆 |
| `memory_search` | 搜索记忆 |
| `enter_plan_mode` | 进入计划模式 |
| `exit_plan_mode` | 退出计划模式 |
| `ask_user` | 向用户提问 |

子代理实际可用的工具是上述列表经过白名单、黑名单和自动排除规则过滤后的子集。
