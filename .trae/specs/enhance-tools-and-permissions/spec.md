# 内置工具补全与多级审批流程升级 Spec

## Why

当前项目仅有 10 个内置工具（ls/read_file/write_file/edit_file/glob/grep/execute/write_todos/task/ask_user），缺少所有竞品标配的 web_search、web_fetch、delete_file 等工具。审批流程仅支持 approve_once/approve_thread/reject 三种选项，缺乏规则持久化、工具风险分级、敏感路径保护和拒绝追踪等竞品普遍具备的能力。本方案参考 Claude Code、OpenCode、Grok-Build、Qwen-Code 四个竞品的成熟实现进行补全。

## 竞品调研摘要

### 工具覆盖对比

| 工具 | harness-code | Claude Code | OpenCode | Grok-Build | Qwen-Code |
|------|:-----------:|:-----------:|:--------:|:----------:|:---------:|
| ls/read_file/glob/grep | ✅ | ✅ | ✅ | ✅ | ✅ |
| write_file/edit_file | ✅ | ✅ | ✅ | ✅ | ✅ |
| execute (shell) | ✅ | ✅ | ✅ | ✅ | ✅ |
| write_todos | ✅ | ✅ | ✅ | ✅ | ✅ |
| task (subagent) | ✅ | ✅ | ❌ | ✅ | ✅ |
| ask_user | ✅ | ✅ | ✅ | ✅ | ✅ |
| **web_search** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **web_fetch** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **delete_file** | ❌ | ✅(bash) | ✅(bash) | ✅ | ✅ |
| **lsp** | ❌ | ✅ | ❌ | ✅ | ✅ |
| **enter/exit_plan_mode** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **task_output/task_stop** | ❌ | ✅ | ❌ | ✅ | ✅ |
| **apply_patch** | ❌ | ❌ | ✅ | ✅ | ❌ |
| **monitor** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **tool_search** | ❌ | ✅ | ❌ | ✅ | ✅ |
| **memory_search/save** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **notebook_edit** | ❌ | ✅ | ❌ | ❌ | ✅ |
| **cron_create/list/delete** | ❌ | ✅ | ❌ | ✅ | ✅ |

### 审批流程对比

| 维度 | harness-code | Claude Code | OpenCode | Grok-Build | Qwen-Code |
|------|:-----------:|:-----------:|:--------:|:----------:|:---------:|
| 审批选项 | 3种 | 4种+规则建议 | 3种(once/always/reject) | 4种+per-command记忆 | 5种 |
| 权限模式 | 4种 | 6种 | 规则集 | 3种 | 5种 |
| 规则持久化 | ❌ | ✅ 多层级文件 | ✅ SQLite | ✅ TOML | ✅ JSON |
| 风险分级 | 二元 | 3级+属性标记 | action+resource | 只读/变更+exec_risk | 10类Kind |
| 命令细粒度 | 整体白名单 | 通配符规则 | 通配符 | per-command记忆 | AST+glob |
| 敏感路径保护 | 工作区边界 | safetyCheck免疫bypass | .env | 受保护编辑 | .git/CI |
| 拒绝追踪 | ❌ | 3次/20次回退 | ❌ | 3次连续 | 3次连续 |

## What Changes

### 工具补全

- 新增 `web_search` 工具：网络搜索，返回结构化搜索结果
- 新增 `web_fetch` 工具：获取 URL 内容，支持 text/markdown/html 格式
- 新增 `delete_file` 工具：删除指定文件（需审批）
- 新增 `lsp` 工具：语言服务协议操作（定义跳转、引用查找、诊断）
- 新增 `enter_plan_mode` / `exit_plan_mode` 工具：显式计划模式切换
- 新增 `task_output` / `task_stop` 工具：后台任务输出获取和终止
- 新增 `apply_patch` 工具：应用 unified diff 格式补丁
- 新增 `monitor` 工具：后台持续执行命令并监控输出
- 新增 `tool_search` 工具：搜索可用的 MCP 工具
- 新增 `memory_search` / `memory_save` 工具：跨会话记忆存取

### 审批流程升级

- 扩展审批选项：新增 `approve_always`（永久允许）和 `reject_with_feedback`（拒绝+反馈）
- 新增权限规则持久化系统：支持 session/project/user 三层级规则存储
- 新增工具风险分级：ToolKind 枚举（Read/Edit/Delete/Execute/Agent/Interact/Plan）
- 新增多级审批流水线：L1参数验证 → L2 deny硬拦截 → L3只读放行 → L4规则评估 → L5模式覆盖
- 新增敏感路径保护：.git/.harness/.bashrc 等路径写入强制审批（yolo 免疫）
- 新增拒绝追踪与回退：连续拒绝 ≥3 次触发回退机制
- 新增 Shell 命令细粒度规则：支持 `execute(git *)` 通配符匹配

## Impact

- Affected specs: 工具系统、审批系统、JSON-RPC 协议、CLI 交互
- Affected code:
  - `packages/agent/harness_agent/agent.py` — 工具契约声明扩展
  - `packages/agent/harness_agent/approval_policy.py` — 审批策略重构为多级流水线
  - `packages/agent/harness_agent/approval_mode.py` — 新增 auto 模式
  - `packages/agent/harness_agent/protocol_generated.py` — 审批响应类型扩展
  - `packages/agent/harness_agent/server.py` — 审批交互流程适配
  - `packages/agent/harness_agent/` — 新增 `permission_rules.py`（规则持久化）
  - `packages/agent/harness_agent/` — 新增 `tool_risk.py`（风险分级）
  - `packages/agent/harness_agent/` — 新增 `sensitive_paths.py`（敏感路径保护）
  - `packages/agent/harness_agent/` — 新增工具实现模块
  - `packages/protocol/` — 新增审批相关类型
  - `packages/cli/` — 审批 UI 适配新选项

## ADDED Requirements

### Requirement: web_search 工具
系统 SHALL 提供 `web_search` 工具，允许 Agent 执行网络搜索并获取结构化结果。

参数：
- `query` (string, 必填): 搜索关键词
- `num_results` (integer, 可选, 默认5): 返回结果数量

返回：搜索结果数组，每项包含 title/url/snippet。

#### Scenario: 基本搜索
- **WHEN** Agent 调用 `web_search(query="Python asyncio tutorial")`
- **THEN** 返回最多 5 条搜索结果（title/url/snippet）

#### Scenario: 审批级别
- **WHEN** 在 default 模式下调用 web_search
- **THEN** 无需审批，直接执行（只读工具）

### Requirement: web_fetch 工具
系统 SHALL 提供 `web_fetch` 工具，允许 Agent 获取指定 URL 的内容。

参数：
- `url` (string, 必填): 目标 URL（必须为 http/https）
- `format` (string, 可选, 默认"markdown"): 输出格式（text/markdown/html）

#### Scenario: 获取网页内容
- **WHEN** Agent 调用 `web_fetch(url="https://example.com", format="markdown")`
- **THEN** 返回网页内容的 markdown 格式文本

#### Scenario: 审批级别
- **WHEN** 在 default 模式下调用 web_fetch
- **THEN** 需要用户审批（网络访问）

### Requirement: delete_file 工具
系统 SHALL 提供 `delete_file` 工具，允许 Agent 删除指定文件。

参数：
- `file_path` (string, 必填): 要删除的文件路径

#### Scenario: 删除文件
- **WHEN** Agent 调用 `delete_file(file_path="src/temp.py")`
- **THEN** 文件被删除，返回确认信息

#### Scenario: 审批级别
- **WHEN** 在任何非 yolo 模式下调用 delete_file
- **THEN** 始终需要用户审批（不可逆操作）

### Requirement: lsp 工具
系统 SHALL 提供 `lsp` 工具，允许 Agent 通过语言服务协议获取代码智能信息。

参数：
- `action` (string, 必填): 操作类型（definition/references/diagnostics/hover）
- `file_path` (string, 必填): 目标文件路径
- `line` (integer, 可选): 行号
- `column` (integer, 可选): 列号

#### Scenario: 跳转定义
- **WHEN** Agent 调用 `lsp(action="definition", file_path="src/main.py", line=10, column=5)`
- **THEN** 返回符号定义位置（文件路径+行号）

#### Scenario: 审批级别
- **WHEN** 调用 lsp 工具
- **THEN** 无需审批（只读工具）

### Requirement: enter_plan_mode / exit_plan_mode 工具
系统 SHALL 提供显式计划模式切换工具，允许 Agent 主动进入/退出计划模式。

#### Scenario: 进入计划模式
- **WHEN** Agent 调用 `enter_plan_mode()`
- **THEN** 后续工具调用受计划模式约束（仅允许只读工具）

#### Scenario: 退出计划模式
- **WHEN** Agent 调用 `exit_plan_mode()`
- **THEN** 恢复到进入前的审批模式

### Requirement: task_output / task_stop 工具
系统 SHALL 提供后台任务管理工具。

- `task_output(task_id)`: 获取指定后台任务的当前输出
- `task_stop(task_id)`: 终止指定后台任务

#### Scenario: 获取后台任务输出
- **WHEN** Agent 调用 `task_output(task_id="abc123")`
- **THEN** 返回该任务的最新输出内容

#### Scenario: 终止后台任务
- **WHEN** Agent 调用 `task_stop(task_id="abc123")`
- **THEN** 任务被终止，返回确认

### Requirement: apply_patch 工具
系统 SHALL 提供 `apply_patch` 工具，支持应用 unified diff 格式的补丁。

参数：
- `patch` (string, 必填): unified diff 格式补丁内容

#### Scenario: 应用补丁
- **WHEN** Agent 调用 `apply_patch(patch="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new")`
- **THEN** 补丁被应用到对应文件

### Requirement: monitor 工具
系统 SHALL 提供 `monitor` 工具，允许后台持续执行命令并监控输出。

参数：
- `command` (string, 必填): 要执行的命令
- `interval` (integer, 可选, 默认5): 输出轮询间隔（秒）

#### Scenario: 监控开发服务器
- **WHEN** Agent 调用 `monitor(command="npm run dev")`
- **THEN** 命令在后台持续执行，可通过 task_output 获取输出

### Requirement: tool_search 工具
系统 SHALL 提供 `tool_search` 工具，允许 Agent 搜索可用的 MCP 外部工具。

参数：
- `query` (string, 必填): 搜索关键词

#### Scenario: 搜索 MCP 工具
- **WHEN** Agent 调用 `tool_search(query="database")`
- **THEN** 返回名称或描述匹配的 MCP 工具列表

### Requirement: memory_search / memory_save 工具
系统 SHALL 提供跨会话记忆存取工具。

- `memory_save(key, content)`: 保存记忆条目
- `memory_search(query)`: 语义搜索记忆

存储位置：`~/.harness/memory/`

#### Scenario: 保存记忆
- **WHEN** Agent 调用 `memory_save(key="project-arch", content="本项目使用...")`
- **THEN** 记忆被持久化，后续会话可检索

### Requirement: 审批选项扩展
系统 SHALL 将审批决策选项从 3 种扩展为 5 种。

```
ApprovalDecision = "approve_once" | "approve_thread" | "approve_always" | "reject" | "reject_with_feedback"
```

- `approve_once`: 本次允许（已有）
- `approve_thread`: 本线程允许（已有）
- `approve_always`: 永久允许该工具+资源组合，写入持久化规则
- `reject`: 拒绝（已有）
- `reject_with_feedback`: 拒绝并附带反馈文本，指导 Agent 调整行为

#### Scenario: 永久允许
- **WHEN** 用户对 `execute(git status)` 选择 `approve_always`
- **THEN** 规则 `{"tool": "execute", "resource": "git *", "effect": "allow"}` 写入项目设置
- **AND** 后续匹配的 git 命令自动放行

#### Scenario: 拒绝并反馈
- **WHEN** 用户选择 `reject_with_feedback` 并输入 "不要删除这个文件，改用重命名"
- **THEN** Agent 收到拒绝原因和反馈文本，据此调整后续行为

### Requirement: 权限规则持久化
系统 SHALL 支持三层级权限规则存储。

规则数据结构：
```python
@dataclass
class PermissionRule:
    tool: str        # 工具名，支持通配符 "*"
    resource: str    # 资源模式（路径/命令/URL），支持通配符
    effect: str      # "allow" | "deny" | "ask"
```

存储层级：
- `session`: 内存，当前会话有效
- `project`: `.harness/settings.json`，项目级持久化
- `user`: `~/.harness/settings.json`，用户级持久化

规则评估策略：最后匹配优先（findLast），与 OpenCode 一致。

#### Scenario: 项目级规则
- **WHEN** `.harness/settings.json` 包含 `{"permissions": [{"tool": "execute", "resource": "git *", "effect": "allow"}]}`
- **THEN** 所有 git 命令自动放行，无需审批

#### Scenario: deny 规则不可覆盖
- **WHEN** 用户设置包含 `{"tool": "execute", "resource": "rm -rf *", "effect": "deny"}`
- **THEN** 即使在 yolo 模式下，匹配命令也被阻止

### Requirement: 工具风险分级
系统 SHALL 为每个工具定义 ToolKind 风险分类。

```python
class ToolKind(Enum):
    READ = "read"         # ls, read_file, glob, grep, web_search, lsp, tool_search, memory_search
    EDIT = "edit"         # write_file, edit_file, apply_patch
    DELETE = "delete"     # delete_file
    EXECUTE = "execute"   # execute, monitor
    AGENT = "agent"       # task
    INTERACT = "interact" # ask_user, write_todos, memory_save
    PLAN = "plan"         # enter_plan_mode, exit_plan_mode
    FETCH = "fetch"       # web_fetch
```

各模式下的默认审批策略：
| ToolKind | plan | default | auto-edit | yolo |
|----------|------|---------|-----------|------|
| READ | allow | allow | allow | allow |
| EDIT | deny | ask | allow | allow |
| DELETE | deny | ask | ask | allow |
| EXECUTE | deny | ask | ask | allow |
| AGENT | deny | ask | ask | allow |
| INTERACT | allow | allow | allow | allow |
| PLAN | allow | allow | allow | allow |
| FETCH | allow | ask | ask | allow |

### Requirement: 多级审批流水线
系统 SHALL 实现 5 级递进审批评估。

```
L1: 参数验证 → 无效参数直接返回错误（不触发审批）
L2: deny 规则硬拦截 → 匹配 deny 规则直接拒绝（任何模式不可覆盖）
L3: 只读工具放行 → ToolKind.READ/INTERACT/PLAN 直接允许
L4: 规则评估 → 按 session > project > user 顺序匹配 allow/ask 规则
L5: 审批模式覆盖 → 按当前模式决定最终行为
```

#### Scenario: deny 规则优先
- **WHEN** yolo 模式下，deny 规则匹配 `execute(rm -rf /)`
- **THEN** 命令被阻止，不执行

#### Scenario: 只读工具免审批
- **WHEN** default 模式下调用 `grep(pattern="TODO")`
- **THEN** 直接执行，不触发审批弹窗

### Requirement: 敏感路径保护
系统 SHALL 对敏感文件/目录的写操作强制审批。

受保护路径：
```python
SENSITIVE_FILES = [".gitconfig", ".bashrc", ".zshrc", ".profile", ".mcp.json"]
SENSITIVE_DIRECTORIES = [".git/", ".vscode/", ".harness/"]
```

#### Scenario: yolo 模式下写入 .git
- **WHEN** yolo 模式下 Agent 尝试 `write_file(file_path=".git/config")`
- **THEN** 仍触发审批弹窗（safetyCheck 免疫 yolo）

#### Scenario: 正常路径不受影响
- **WHEN** yolo 模式下 Agent 写入 `src/main.py`
- **THEN** 直接执行，不触发审批

### Requirement: 拒绝追踪与回退
系统 SHALL 追踪连续拒绝次数，防止 Agent 陷入死循环。

- 连续拒绝阈值：3 次
- 触发行为：向 Agent 注入系统提示 "用户已连续拒绝 N 次操作，请重新评估方案或询问用户意图"

#### Scenario: 连续拒绝回退
- **WHEN** Agent 连续 3 次工具调用被用户拒绝
- **THEN** 系统注入提示，引导 Agent 改变策略

### Requirement: Shell 命令细粒度规则
系统 SHALL 支持对 execute 工具的命令内容进行通配符匹配。

规则格式：`execute(<glob_pattern>)`

#### Scenario: 允许所有 git 命令
- **WHEN** 规则 `{"tool": "execute", "resource": "git *", "effect": "allow"}`
- **THEN** `git status`、`git log`、`git diff` 等自动放行
- **AND** `git push --force` 仍匹配（通配符覆盖）

#### Scenario: 拒绝危险命令
- **WHEN** 规则 `{"tool": "execute", "resource": "rm -rf *", "effect": "deny"}`
- **THEN** 匹配的删除命令被硬拦截

## MODIFIED Requirements

### Requirement: approval_policy.py 重构
当前 `interrupt_on_for_approval_mode()` 基于硬编码工具集合判断。修改为：
- 基于 ToolKind 分类和 PermissionRule 规则集动态计算
- 保留 `_PLAN_ALLOWED_TOOLS` 白名单作为 plan 模式快速路径
- 新增 `evaluate_permission(tool_name, resource, rules, mode)` 统一入口

### Requirement: protocol_generated.py 审批响应扩展
当前 `ApprovalResponse.decision` 仅支持 3 种值。修改为：
- 新增 `approve_always` 和 `reject_with_feedback` 决策值
- `reject_with_feedback` 时 `feedback` 字段为必填

### Requirement: server.py 审批交互适配
当前 `_resume_value()` 映射 3 种决策。修改为：
- 处理 `approve_always`：将规则写入持久化存储后恢复执行
- 处理 `reject_with_feedback`：将反馈文本注入 ToolMessage 内容

### Requirement: 工具契约声明扩展
当前 `_BUILTIN_TOOL_SHAPES` 包含 9 个工具。修改为：
- 新增 web_search/web_fetch/delete_file/lsp/enter_plan_mode/exit_plan_mode/task_output/task_stop/apply_patch/monitor/tool_search/memory_search/memory_save 共 13 个工具契约

## REMOVED Requirements

无。
