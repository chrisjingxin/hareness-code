# 权限审批引擎重构 - 任务列表

## 阶段一：规则 DSL 引擎（Python policy 层）

- [ ] **Task 1: 实现规则字符串 DSL 解析器**
  - [ ] 1.1 新建 `packages/agent/harness_agent/policy/rule_parser.py`
  - [ ] 1.2 实现 `parse_rule(rule_str) → PermissionRule`：解析 `ToolName(RuleContent)` 格式，处理转义括号、旧工具名别名映射（Task→task、Write→write_file、KillShell→task_stop）
  - [ ] 1.3 支持三种 specifier 模式：命令前缀（Bash）、路径 glob（Edit/Read/Write）、域名（WebFetch domain:）
  - [ ] 1.4 实现 `serialize_rule(PermissionRule) → rule_str`：反向序列化为 DSL 字符串
  - [ ] 1.5 实现 `parse_rule_list(raw_rules) → list[PermissionRule]`：兼容 JSON 和 DSL 两种输入
  - [ ] 1.6 编写 `tests/policy/test_rule_parser.py`：覆盖解析/序列化/边界/兼容

- [ ] **Task 2: 替换持久化层为 DSL 格式输出**
  - [ ] 2.1 修改 `permission_rules.py` 的 `save_rule`：输出时调用 `serialize_rule` 生成 DSL 字符串
  - [ ] 2.2 修改 `load_rules`：读取时调用 `parse_rule_list` 兼容 JSON 和 DSL 输入
  - [ ] 2.3 更新 `settings.json` 中 `permissions` 字段的示例文档
  - [ ] 2.4 更新 `docs/user/examples/config.toml` 中的审批相关示例

- [ ] **Task 3: 受信目录门**
  - [ ] 3.1 新建 `packages/agent/harness_agent/policy/trust_gate.py`
  - [ ] 3.2 实现 `is_trusted_directory(path) → bool`：检查目录是否在受信列表中
  - [ ] 3.3 实现 `trust_directory(path, source)` 和 `untrust_directory(path)`：增删受信目录
  - [ ] 3.4 受信列表持久化到 `~/.harness/settings.json` 的 `trusted_directories` 字段
  - [ ] 3.5 在 `approval_policy.py` 的预检中注入受信门：未受信时隐藏 `approve_thread`/`approve_always` 选项
  - [ ] 3.6 在 `approval_mode.py` 模式切换时注入受信门：未受信时拒绝切换到 yolo/auto
  - [ ] 3.7 编写 `tests/policy/test_trust_gate.py`

## 阶段二：Shell/Bash 安全引擎（Python policy 层）

- [ ] **Task 4: 集成 tree-sitter-bash 解析依赖**
  - [ ] 4.1 在 `packages/agent/pyproject.toml` 中添加 `tree-sitter` 和 `tree-sitter-bash` 依赖
  - [ ] 4.2 新建 `packages/agent/harness_agent/policy/bash_parser.py`
  - [ ] 4.3 实现 `parse_bash(command) → BashAST`：使用 tree-sitter 解析 bash 命令
  - [ ] 4.4 实现 `extract_segments(ast) → list[CommandSegment]`：拆分链式命令（`&&`/`||`/`;`/`|`）
  - [ ] 4.5 实现 `strip_wrappers(segment) → CommandSegment`：剥离 timeout/env/nice/nohup/bash -c 等包装器，递归深度 ≤3
  - [ ] 4.6 实现 `get_command_root(segment) → str`：提取命令根（首词），含词边界
  - [ ] 4.7 编写 `tests/policy/test_bash_parser.py`：覆盖拆分/剥离/边界场景

- [ ] **Task 5: 实现命令前缀匹配引擎**
  - [ ] 5.1 新建 `packages/agent/harness_agent/policy/bash_matcher.py`
  - [ ] 5.2 实现 `matches_command_prefix(pattern, command) → bool`：词边界前缀匹配（`git` 不匹配 `gitleaks`）
  - [ ] 5.3 实现 `matches_command_glob(pattern, command) → bool`：Freeform glob 匹配
  - [ ] 5.4 实现 `evaluate_bash(command, rules) → BashDecision`：逐 segment 对所有 Bash/Any 规则评估，汇总 deny/ask/allow
  - [ ] 5.5 合取式 allow：所有 segment 都命中 allow 规则才整体放行
  - [ ] 5.6 CWE-178 防护：匹配前 trim 前导空白
  - [ ] 5.7 无法解析的命令 fail-closed → ask
  - [ ] 5.8 编写 `tests/policy/test_bash_matcher.py`

- [ ] **Task 6: 实现 shell 文件访问门**
  - [ ] 6.1 新建 `packages/agent/harness_agent/policy/shell_access.py`
  - [ ] 6.2 实现 `extract_write_paths(command) → list[Path]`：从命令中提取输出重定向（`>`、`>>`）和写命令（`tee`、`dd of=...`）的目标路径
  - [ ] 6.3 实现 `evaluate_shell_file_access(command, file_rules) → GateResult`：将提取的路径对照 Read/Edit/Any 的 deny/ask 规则
  - [ ] 6.4 编写 `tests/policy/test_shell_access.py`

- [ ] **Task 7: 扩展危险命令检测**
  - [ ] 7.1 扩展 `auto_mode.py` 的 `DESTRUCTIVE_PATTERNS`：添加 `git checkout -- .`、`git stash drop`、`git commit --amend`、`kubectl delete`、`chmod -R 777`、`chown -R` 等
  - [ ] 7.2 实现 `has_destructive_intent(user_messages) → bool`：检测用户最近消息是否含"丢弃/清除/重置"等破坏意图关键词（中日文）
  - [ ] 7.3 在 F3 过滤器中：破坏性命令 + 用户破坏意图 → 降级 ask；无意图 → deny
  - [ ] 7.4 编写 `tests/policy/test_destructive_patterns.py`

- [ ] **Task 8: 实现安全命令白名单**
  - [ ] 8.1 新建 `packages/agent/harness_agent/policy/safe_commands.py`
  - [ ] 8.2 定义 `ALWAYS_SAFE_COMMANDS`：`ls`、`cat`、`pwd`、`whoami`、`head`、`tail`、`wc`、`sort`、`uniq`、`tr`、`cut`、`echo`、`date`、`uname`、`df`、`du`、`free`、`uptime`、`which`、`whereis`
  - [ ] 8.3 定义 `SAFE_GIT_SUBCOMMANDS`：`status`、`log`、`diff`、`show`、`branch`（无 `-D`）、`remote -v`、`stash list`
  - [ ] 8.4 实现 `is_safe_command(segment) → bool`：综合判断 + 排除危险参数（如 `rg --pre`）
  - [ ] 8.5 在 `approval_policy.py` 的只读检查中接入白名单：default 模式下安全命令自动 allow
  - [ ] 8.6 编写 `tests/policy/test_safe_commands.py`

- [ ] **Task 9: 实现 Shell 安全底线**
  - [ ] 9.1 在 `bash_matcher.py` 中实现 `has_write_side_effect(segment) → bool`：检测输出重定向、写命令
  - [ ] 9.2 实现 `has_unsafe_env(segment) → bool`：检测 LD_PRELOAD、LD_LIBRARY_PATH 等危险环境变量
  - [ ] 9.3 实现 `is_opaque_shell(segment) → bool`：检测 eval、bash -c `$VAR` 等动态执行
  - [ ] 9.4 实现 `has_exec_risk(segment) → bool`：检测 git commit/rebase/am 等可能触发 hooks 的命令
  - [ ] 9.5 实现 `evaluate_safety_floors(command) → FloorResult`：即使白名单命中，底线触发仍强制 ask
  - [ ] 9.6 编写 `tests/policy/test_bash_floors.py`

## 阶段三：多工具审批串行化（Python host + TS CLI 层）

- [ ] **Task 10: Python 端审批交互改为逐个串行（仅非并发安全工具）**
  - [ ] 10.1 定义并发安全工具集合：READ/INTERACT/PLAN 类别工具（`is_concurrency_safe(tool_name) → bool`）
  - [ ] 10.2 修改 `run_coordinator.py` 的 `_extract_interaction`：并发安全工具不产生 InteractionRequest，直接放行执行
  - [ ] 10.3 非并发安全工具不再合并为一个审批：分别生成独立的 InteractionRequest，按序入队
  - [ ] 10.4 修改 `run_coordinator.py` 的 `_resume_value`：不再将单个决策广播到所有动作，改为 1:1 映射
  - [ ] 10.5 实现 `_pending_approvals` 队列：维护一个有序的待审批列表，前一个完成才弹出下一个
  - [ ] 10.6 并发安全工具与非并发安全工具混合时：读工具先并行执行完毕，写/执行工具按序串行审批
  - [ ] 10.7 实现 `PolicyDeny` 路径：生成 ToolMessage(error) 返回模型，标记为 Continue
  - [ ] 10.8 实现 `UserReject` 路径：标记为 PermissionReject，跳过同批后续所有非并发安全 tool call
  - [ ] 10.9 编写 `tests/host/test_approval_protocol.py`：覆盖并发安全工具直接执行、逐个审批、PolicyDeny 继续、UserReject 终止、已完成不回滚

- [ ] **Task 11: TypeScript 端适配逐个审批展示**
  - [ ] 11.1 修改 `state.ts`：`PendingApproval` 状态支持单工具详情（不再聚合多条描述）
  - [ ] 11.2 修改 `timeline.tsx`：确保同一时刻只渲染一个审批卡片
  - [ ] 11.3 修改 `controller.ts`：审批回传后不立即处理下一个，等待 Python 推送下一个 interaction
  - [ ] 11.4 受信目录未受信时隐藏 "本线程允许" 和 "永久允许" 按钮
  - [ ] 11.5 编写 `packages/cli/tests/tui/application/controller.test.ts`：逐个审批、终止同批场景

## 阶段四：AUTO 模式规则剥离与 Always Allow 粒度优化

- [ ] **Task 12: AUTO 模式危险规则剥离**
  - [ ] 12.1 新建 `packages/agent/harness_agent/policy/dangerous_rules.py`
  - [ ] 12.2 定义 `DANGEROUS_ALLOW_PATTERNS`：`Bash(*)`、`Bash`、`Bash(python *)`、`Bash(node *)`、`Bash(bash *)`、`Bash(sh *)`、`Bash(sudo *)`、`Bash(ssh *)`、`Bash(curl *)`、`Bash(wget *)` 等
  - [ ] 12.3 实现 `strip_dangerous_rules(rules) → tuple[list, list]`：分离出危险 allow 规则，返回（安全规则，已剥离规则）
  - [ ] 12.4 在 `approval_mode.py` 模式切换中注入：进入 AUTO → 剥离；退出 AUTO → 恢复
  - [ ] 12.5 编写 `tests/policy/test_dangerous_rules.py`

- [ ] **Task 13: Always Allow 规则生成粒度精细化**
  - [ ] 13.1 修改 `run_coordinator.py` 的 `_generate_permission_rule`：
    - Bash execute：调用 `bash_parser.extract_command_rule(command)` 提取 AST 最小范围规则
    - 文件写入：提取目录级路径，生成 `Edit(src/utils/**)` 风格规则
    - WebFetch：提取域名，生成 `WebFetch(domain:host)` 风格规则
    - Delete：保持人工确认优先级
  - [ ] 13.2 在 `bash_parser.py` 中实现 `extract_command_rule(command) → str`：基于 tree-sitter AST 提取根命令+已知子命令，参数替换为 `*`
  - [ ] 13.3 编写 `tests/host/test_approval_protocol.py`：验证各类工具的规则生成粒度

- [ ] **Task 14: AUTO 模式编辑快速放行**
  - [ ] 14.1 修改 `auto_mode.py` 的 F1 `acceptEdits` 过滤器：当前仅工作区内非敏感路径自动放行，改为**所有 Edit 类工具（write_file/edit_file/apply_patch）直接 allow**
  - [ ] 14.2 快速放行前置条件：目标路径不在受保护路径列表中、目标路径在工作区范围内
  - [ ] 14.3 受保护路径命中时：不适用快速放行，继续进入正常审批管线（F4 分类器或回退人工确认）
  - [ ] 14.4 Delete 类工具明确不走编辑快速放行，保持原 AUTO 分类器路径
  - [ ] 14.5 非 AUTO 模式下不改动 F1 行为（auto-edit 保持其原有独立逻辑）
  - [ ] 14.6 编写 `tests/policy/test_auto_mode.py`：覆盖工作区内非保护路径直接放行、受保护路径不快速放行、工作区外不快速放行、Delete 不走快速放行

## 阶段五：路径安全检查强化

- [ ] **Task 15: 工作区路径验证增强**
  - [ ] 15.1 在 `sensitive_paths.py` 中扩展 `SENSITIVE_DIRECTORIES`：添加 `.husky`、`.github/workflows`
  - [ ] 15.2 扩展 `SENSITIVE_FILES`：添加 `package.json`、`Makefile`、`.npmrc`、`.gitignore`、`*.harness/settings.json`
  - [ ] 15.3 实现 `resolve_symlink_safe(path) → Path`：realpath 解析符号链接，检测是否指向工作区外
  - [ ] 15.4 在 `approval_policy.py` 预检中：符号链接指向工作区外 → 强制 ask
  - [ ] 15.5 编写 `tests/policy/test_sensitive_paths.py`

# 任务依赖

- Task 1 和 Task 4 无依赖，可并行
- Task 2 依赖 Task 1
- Task 5、6、8、9 依赖 Task 4
- Task 7 依赖 Task 4 和 Task 5
- Task 10 依赖 Task 1、4、5、9（规则 DSL + Shell 引擎完成后才能改审批管线）
- Task 11 依赖 Task 10
- Task 12 依赖 Task 1
- Task 13 依赖 Task 4、10
- Task 14 依赖 Task 1（规则 DSL 完成后才能判断受保护路径）
- Task 15 无依赖，可并行
- Task 3 无依赖，可并行
