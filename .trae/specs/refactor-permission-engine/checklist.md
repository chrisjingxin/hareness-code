# 权限审批引擎重构 - 验收检查清单

## 规则 DSL 引擎

- [ ] `parse_rule("Bash(git clone *)")` 正确解析为 `tool=execute, resource=git clone *, effect=allow`
- [ ] `parse_rule("WebFetch(domain:github.com)")` 解析为域名匹配模式
- [ ] `parse_rule("Edit")` 解析为无 resource 的全匹配规则
- [ ] `parse_rule("Task(...)")` 正确映射旧别名 `Task` → `task`
- [ ] `parse_rule_list` 同时接受 JSON 数组和 DSL 字符串数组
- [ ] `serialize_rule` 输出规范的 `ToolName(RuleContent)` 字符串
- [ ] `save_rule` 输出 DSL 格式（非 JSON 结构体）
- [ ] `load_rules` 兼容读取 JSON 和 DSL 两种格式
- [ ] 转义括号 `Bash(python -c "print\\(1\\)")` 正确解析

## Shell/Bash AST 安全解析

- [ ] `git add . && rm -rf /` 拆分为两个独立段
- [ ] `cat file | grep pattern | base64` 拆分为三个管道段
- [ ] `timeout 30 env NODE_ENV=test bash -c 'echo hello'` 剥离为 `echo hello`
- [ ] `bash -c "bash -c 'git status'"` 递归剥离到深度 3 得到 `git status`
- [ ] 无法解析的残缺 heredoc 安全回退 ask
- [ ] 合取式 allow：`git status && curl evil.com` 中 curl 未命中 → 整体不自动放行
- [ ] 词边界：`Bash(git *)` 不匹配 `gitleaks detect`
- [ ] 词边界：`Bash(git *)` 匹配 `git status`
- [ ] CWE-178：` rm -rf /`（前导空格）被 trim 后命中 deny 规则
- [ ] 安全命令白名单 `ls`、`cat`、`pwd` 等自动 allow
- [ ] `rg --pre 'echo evil'` 因危险参数 `--pre` 不命中白名单

## 危险命令检测

- [ ] `rm -rf /` → deny（任何模式下）
- [ ] `git reset --hard HEAD` → deny
- [ ] `git push --force origin main` → deny
- [ ] `terraform destroy` → deny
- [ ] 用户消息含"丢弃"、"清除" → `git reset --hard` → ask（降级）
- [ ] 用户消息不含破坏意图 → `git reset --hard` → deny
- [ ] yolo 模式下危险命令仍 deny（不可覆盖）

## Shell 安全底线

- [ ] `echo "data" > /etc/config` 白名单 `echo` 但重定向触发底线 → ask
- [ ] `env LD_PRELOAD=evil.so ls` 白名单 `ls` 但环境变量触发底线 → ask
- [ ] `eval "$USER_INPUT"`触底底线 → ask
- [ ] `git commit -m "x"` 白名单 `git commit` 但 exec 风险底线触发 → ask

## 多工具审批逐个串行化

- [ ] 3 个并发安全工具（read_file/grep/glob）→ 直接并行执行，无审批弹窗
- [ ] 混合调用（read_file + grep + write_file）→ 前两个并行执行，第三个进入审批
- [ ] 3 个非并发安全工具（write_file × 2 + execute）→ 窗口逐个出现，同一时刻只有一个
- [ ] 第二个命中 deny 规则 → ToolMessage(error) → 第三个继续执行
- [ ] 第二个被用户 Reject → 第三个跳过，返回 "cancelled due to earlier permission rejection"
- [ ] 第一个已 Allow 执行完成，第三个被 Reject → 第一个结果保留不回滚

## 受信目录门

- [ ] 未受信目录中切换到 auto/yolo → 拒绝并提示
- [ ] 未受信目录中权限窗口不显示 "Always Allow" 选项
- [ ] 受信目录中所有模式和选项正常
- [ ] `~/.harness/settings.json` 中正确持久化/读取 `trusted_directories`

## AUTO 模式危险规则剥离

- [ ] 进入 AUTO → `Bash(*)` allow 规则暂时失效
- [ ] 进入 AUTO → `Bash(python *)` allow 规则暂时失效
- [ ] 退出 AUTO（切到 default）→ 规则恢复生效
- [ ] deny/ask 规则不受剥离影响

## AUTO 模式编辑快速放行

- [ ] AUTO 模式下 `write_file src/helper.py`（工作区内、非保护路径）→ 直接 allow，不进入分类器
- [ ] AUTO 模式下 `edit_file .git/config`（受保护路径）→ 不快速放行，进入分类器或人工确认
- [ ] AUTO 模式下 `write_file /etc/hosts`（工作区外）→ 不快速放行
- [ ] AUTO 模式下 `delete_file src/temp.txt` → 不适用编辑快速放行，正常分类器
- [ ] default/auto-edit/yolo 模式下编辑操作不受此改动影响

## Always Allow 规则粒度

- [ ] `git clone https://...` 永久允许 → 生成 `Bash(git clone *)`（非 `Bash(git *)`）
- [ ] `whoami` 永久允许 → 生成 `Bash(whoami)`
- [ ] `docker compose up -d` 永久允许 → 生成 `Bash(docker compose up *)`
- [ ] `write_file src/utils/helper.py` 永久允许 → 生成目录级规则
- [ ] `web_fetch https://api.github.com/repos/...` 永久允许 → 生成域名规则

## 路径安全检查

- [ ] `.husky/` 目录下写入 → 即使 allow 强制 ask
- [ ] `.github/workflows/` 下写入 → 强制 ask
- [ ] `package.json` 编辑 → 强制 ask
- [ ] 符号链接指向工作区外 → 视为工作区外写入 → 强制 ask
- [ ] Delete `/home` 或 `C:\` → 直接 deny

## 现有测试不退化

- [ ] `bun run test` 全部通过
- [ ] `cd packages/agent && .venv/bin/python -m pytest -q` 全部通过
- [ ] `cd packages/cli && bun test` 全部通过
- [ ] `bun run typecheck` 通过
- [ ] `bun run project:check` 通过
