# 多工具审批机制设计 Spec

## Why

当前项目已有基础审批策略（`packages/agent/harness_agent/policy/`），支持 plan/default/auto-edit/yolo 四种模式和简单的规则引擎。但存在以下不足：

1. **缺少 auto 模式**：没有 LLM 分类器自动判断安全性的能力，用户必须在"全部手动审批"和"全部放行"之间二选一
2. **审批选项不完整**：协议层已定义 `approve_once/approve_thread/approve_always/reject/reject_with_feedback`，但 Python 端未完整实现持久化规则和 deny+feedback 重确认流程
3. **工具风险声明分散**：当前 `tool_risk.py` 用硬编码矩阵管理风险分级，新增工具需要手动维护矩阵，容易遗漏
4. **缺少 AUTO 模式安全守卫**：没有破坏性命令正则硬拦截、连续拒绝回退、分类器 fail-closed 等安全机制

本设计参考 QwenCode（分层评估 L3→L4→L5）、Grok（集中式 Actor 管线 + 4 层防线）、Claude Code（规则引擎 + 模式系统 + 竞赛机制）三个竞品的优劣，取长补短。

## 竞品设计对比与设计理由

### 竞品架构对比

| 维度 | QwenCode | Grok | Claude Code | 本方案 |
|------|----------|------|-------------|--------|
| 工具是否参与审批 | 每个工具重写 `getDefaultPermission()` | 工具不参与，Manager 集中裁决 | 工具实现 `checkPermissions()` | **混合：工具声明 ToolKind + 集中引擎裁决** |
| 审批模式数量 | 5 种（plan/default/auto-edit/auto/yolo） | 6 种（含 dontAsk） | 5+2 种（含 auto/bubble 内部模式） | **5 种（plan/default/auto-edit/auto/yolo）** |
| 规则优先级 | deny > ask > allow > default | deny > ask > allow（与来源无关） | deny > ask > allow > passthrough | **deny > ask > allow（与来源无关）** |
| AUTO 模式实现 | 三层过滤器（快速路径 + 白名单 + LLM 分类器） | 快速路径 + LLM 分类器 + 拒绝预算 | acceptEdits 模拟 + 白名单 + AI 分类器 | **四层过滤器（快速路径 + 白名单 + 破坏性守卫 + LLM 分类器）** |
| 审批 UI 选项 | ProceedOnce/ProceedAlwaysProject/ProceedAlwaysUser/ModifyWithEditor/Cancel | AllowOnce/AllowAlways/RejectOnce/RejectAlways/切换YOLO | Allow/AlwaysAllow/Reject/RejectWithFeedback | **allowOnce/allowAlwaysSession/allowAlwaysProject/deny+feedback/deny** |
| 规则持久化 | 项目级 + 用户级 settings.json | ~/.grok/sessions/ 按项目隔离 | settings.json 多来源合并 | **session(内存) + project + user + system(企业管控) 四层** |

### 为什么这么设计

**1. 混合模型（工具声明 + 集中裁决）**

- 取 QwenCode/Claude Code 之长：每个工具通过 `ToolKind` 枚举声明自身风险类别（READ/EDIT/DELETE/EXECUTE/FETCH/AGENT/INTERACT/PLAN），提供参数语义（路径、命令等）
- 取 Grok 之长：最终裁决由集中式 `evaluate_permission()` 统一完成，保证一致性
- 避免 QwenCode 的问题：不需要每个工具写复杂的 `getDefaultPermission()` 逻辑
- 避免 Grok 的问题：不是所有 Bash 走同一逻辑，工具可以基于参数提供差异化信息

**2. 五种审批模式**

与 QwenCode 完全一致（plan/default/auto-edit/auto/yolo），因为：
- 这 5 种覆盖了从最严格到最宽松的完整梯度
- 不采用 Grok 的 `dontAsk`（CI 场景可用 yolo + deny 规则替代）
- 预留 Claude Code 的 `bubble` 模式：当前版本不实现，但 `ApprovalMode` 枚举和模式切换逻辑需为其预留扩展点。后续子代理功能上线时，bubble 模式允许子代理的审批请求冒泡到父代理/用户决策，而非在子代理层独立处理

**3. deny 绝对优先 + 与来源无关**

取 Grok/Claude Code 的设计：所有来源的规则合并为简单列表，评估时按动作类型（deny > ask > allow）排优先级，不按来源层级。这最简单且安全。

**4. AUTO 模式四层过滤器**

取 QwenCode 的三层过滤器架构，增加 Claude Code 的 acceptEdits 模拟快速路径：
- L5.1 acceptEdits 快速路径（工作区内编辑自动通过）
- L5.2 安全工具白名单（只读工具自动通过）
- L5.3 破坏性命令守卫（正则硬拦截，取 QwenCode）
- L5.4 LLM 分类器（两阶段判断，取 QwenCode + Claude Code）
- 连续拒绝回退（取三家共有设计）
- fail-closed（取 QwenCode/Grok）

**5. 审批选项设计**

五种选项：
- `allowOnce`：执行一次（取三家共有）
- `allowAlwaysSession`：会话级持久化规则 + 执行（取 QwenCode 的 ProceedAlwaysProject 思路，限定为会话生命周期）
- `allowAlwaysProject`：项目级持久化规则 + 执行（取 QwenCode 的 ProceedAlwaysProject + Claude Code 的 AlwaysAllow 写入项目 settings，跨会话持久）
- `deny+feedback`：拒绝并附反馈，Agent 修改后重新确认（取 Claude Code 的 RejectWithFeedback + Grok 的 "tell what to do differently"）
- `deny`：拒绝执行（取三家共有）

不采用 QwenCode 的 `ModifyWithEditor`（TUI 场景下外部编辑器交互复杂度高），不采用 Grok 的 `RejectAlways`（deny 规则应通过配置文件管理）。

规则四层作用域：
- **session**（内存）：会话结束即消失，适合临时信任
- **project**（`.harness/settings.json`）：跨会话持久，跟随项目，适合对某类操作的长期信任（如"允许本项目中所有 git 命令"）
- **user**（`~/.harness/settings.json`）：跨会话持久，用户全局生效
- **system**（企业管控，预留）：`/etc/harness/settings.json`（Linux）/ `C:\ProgramData\harness\settings.json`（Windows）/ `/Library/Application Support/Harness/settings.json`（macOS），由企业 IT 部门预置，**最高优先级，不可被用户/项目覆盖**。当前版本仅在 `load_rules()` 中预留 system 层的读取逻辑（文件不存在则返回空列表），不实现管理界面

合并与命中策略（两级优先级）：

**第一级：动作类型优先级** — deny > allow > ask
- 任何来源的 deny 命中 → 绝对拒绝，不可被任何 allow 覆盖（包括 session allow）
- 无 deny 命中时，allow 优先于 ask（用户明确批准的操作不再弹窗）

**第二级：同动作内来源优先级** — session > project > user > system
- 同为 allow 时：session allow（用户本次会话明确批准）> project allow > user allow
- 同为 ask 时：project ask > user ask（项目级配置更具体）
- system 层只产生 deny（企业管控），不产生 allow/ask

**实际效果**：
- 用户点 allowAlwaysSession 批准 `git commit` → session 层写入 allow → 即使 project 配置有 `ask Bash(git *)`，本会话不再弹窗
- 企业 system 层配置 `deny Bash(rm -rf *)` → 任何 allow 都不可覆盖，绝对拒绝
- 用户点 allowAlwaysProject 批准 `npm test` → project 层写入 allow → 所有后续会话不再弹窗（除非有 deny）

**与竞品对比**：
- 取 Grok 的"CLI 参数/用户明确决策优先级最高"思路（session allow 类比 Grok 的 `--allow` 参数）
- 取 QwenCode 的"会话规则覆盖持久化规则"行为
- 保持 deny 绝对优先（三家共识）

**6. Extension 权限钩子预留（参考 QwenCode /extension + Claude Code /plugin）**

- QwenCode 的 `/extension`：用户通过 TypeScript 扩展定义自定义工具和权限逻辑，扩展可以注册 `onToolCall` 钩子参与审批决策
- Claude Code 的 `/plugin`（Hooks 系统）：用户在 settings 中配置 `PreToolUse`/`PostToolUse` 钩子脚本，外部进程参与审批
- 本方案取两者之长：Extension 以 Middleware 形式注册（取 QwenCode 的进程内钩子，性能好、可访问上下文），但权限只能收紧不能放宽（取 Claude Code 的安全边界设计）
- 核心原则：**主审批流程是安全边界，Extension 是增强层**。主流程 deny 不可被 Extension 覆盖；Extension deny 可以覆盖主流程 allow。这保证了即使用户安装了恶意 Extension，也不能绕过核心安全策略
- 当前版本仅预留接口（`evaluate_permission()` 返回值增加 `extension_hooks` 调用点），不实现 Extension 加载/注册/管理功能

## What Changes

- 扩展 `approval_mode.py`：新增 `auto` 模式，完善 5 种模式的解析和切换
- 重构 `approval_policy.py`：实现完整的 5 层评估管线（L1 工具风险声明 → L2 deny 硬拦截 → L3 只读放行 → L4 规则引擎 → L5 模式覆盖）
- 新增 `auto_mode.py`：AUTO 模式四层过滤器 + LLM 分类器 + 拒绝追踪
- 完善 `permission_rules.py`：支持 session/project/user 三层作用域持久化
- 完善协议层 `interaction.approval` 的处理：实现 allowAlwaysSession 的规则持久化 + deny+feedback 的重确认流程
- 更新 `tool_risk.py`：工具通过 `ToolKind` + 参数语义声明风险，集中引擎裁决

## Impact

- Affected specs: 审批策略、工具执行管线、协议交互
- Affected code:
  - `packages/agent/harness_agent/policy/`（核心改动）
  - `packages/agent/harness_agent/host/`（审批请求处理）
  - `packages/protocol/schema/v3.json`（interaction.approval 选项确认）
  - `packages/cli/src/tui/`（审批 UI 渲染）

## ADDED Requirements

### Requirement: AUTO 审批模式

系统 SHALL 提供 `auto` 审批模式，通过四层过滤器自动判断工具调用安全性，减少用户交互。

#### Scenario: 工作区内编辑自动通过
- **WHEN** 当前为 auto 模式，工具类型为 EDIT，目标路径在工作区内且非敏感路径
- **THEN** 自动批准，不弹窗

#### Scenario: 只读工具自动通过
- **WHEN** 当前为 auto 模式，工具 ToolKind 为 READ
- **THEN** 自动批准，不弹窗

#### Scenario: 破坏性命令硬拦截
- **WHEN** 当前为 auto 模式，Shell 命令匹配破坏性正则（`rm -rf /`、`git push --force`、`git reset --hard` 等）
- **THEN** 直接拒绝，不经过分类器

#### Scenario: LLM 分类器判断
- **WHEN** 当前为 auto 模式，前三层均未命中
- **THEN** 调用 LLM 分类器判断安全性；分类器允许则执行，拒绝则阻止

#### Scenario: 分类器不可用时 fail-closed
- **WHEN** LLM 分类器 API 超时或不可用
- **THEN** 拒绝执行（宁可误拒不可漏放）

#### Scenario: 连续拒绝回退
- **WHEN** 分类器连续拒绝 3 次
- **THEN** 回退到手动审批（弹窗让用户决定），用户批准后重置计数

### Requirement: 审批选项完整实现

系统 SHALL 在审批弹窗中提供五种选项：allowOnce、allowAlwaysSession、allowAlwaysProject、deny+feedback、deny。

#### Scenario: allowOnce 执行一次
- **WHEN** 用户选择 allowOnce
- **THEN** 本次工具调用执行，不影响后续同类调用的审批

#### Scenario: allowAlwaysSession 会话级持久化规则
- **WHEN** 用户选择 allowAlwaysSession
- **THEN** 系统生成匹配当前工具的 allow 规则，写入会话级规则集（内存），本次及当前会话内后续匹配调用自动通过，会话结束后规则消失

#### Scenario: allowAlwaysProject 项目级持久化规则
- **WHEN** 用户选择 allowAlwaysProject
- **THEN** 系统生成匹配当前工具的 allow 规则，写入项目级配置文件（`.harness/settings.json`），本次及所有后续会话中匹配调用自动通过，规则跨会话持久存在

#### Scenario: deny+feedback 重确认
- **WHEN** 用户选择 deny+feedback 并输入反馈文本
- **THEN** 本次调用被拒绝，反馈文本注入 Agent 上下文，Agent 修改方案后重新发起工具调用，再次触发审批

#### Scenario: deny 拒绝执行
- **WHEN** 用户选择 deny
- **THEN** 本次调用被拒绝，Agent 收到拒绝消息

### Requirement: 五层评估管线

系统 SHALL 按 L1→L2→L3→L4→L5 顺序评估每次工具调用的权限。

#### Scenario: deny 规则不可覆盖
- **WHEN** L4 规则引擎匹配到 deny 规则
- **THEN** 无论当前审批模式是什么（包括 yolo），直接拒绝

#### Scenario: yolo 模式放行 ask
- **WHEN** 当前为 yolo 模式，L4 无 deny 规则命中，结果为 ask
- **THEN** 自动批准执行（deny 规则除外）

#### Scenario: plan 模式阻止写操作
- **WHEN** 当前为 plan 模式，工具 ToolKind 不是 READ/INTERACT/PLAN
- **THEN** 直接拒绝

### 完整决策树（所有分支）

以下是从工具调用进入到最终决策的完整分支逻辑，对标竞品中 `shouldConfirmExecute()`（QwenCode）、`PermissionManager.canUseTool()`（Grok）、`hasPermissionsToUseToolInner()`（Claude Code）的复杂度。

```
入口: wrap_tool_call(request) 被触发
│
├─ [前置中间件层] （洋葱模型外层，先于审批管线执行）
│   ├─ WorkspaceBoundaryMiddleware
│   │   ├─ 工具无文件路径参数 → 跳过
│   │   ├─ 路径在工作区内 → 通过
│   │   ├─ 路径在工作区外 → 直接返回错误 ToolMessage（不进入审批）
│   │   └─ 符号链接逃逸检测 → 解析 realpath 后重新判断
│   ├─ ShellAllowListMiddleware（仅 execute 工具）
│   │   ├─ 命令可被 shlex 解析且为单一命令 → 通过
│   │   ├─ 包含管道/重定向/命令替换 → 直接返回错误
│   │   └─ 不在白名单 → 直接返回错误
│   └─ ConcurrencyGuardMiddleware
│       ├─ READ 类工具 → 获取共享读锁 → 通过
│       └─ 写类工具 → 获取独占写锁（等待其他写完成）→ 通过
│
├─ [L1] 工具风险声明提取
│   ├─ 从工具注册表获取 ToolKind（READ/EDIT/DELETE/EXECUTE/FETCH/AGENT/INTERACT/PLAN）
│   ├─ 提取参数语义：
│   │   ├─ execute → 解析 command 字段，提取首个命令词（git/npm/rm/...）
│   │   ├─ edit_file/write_file → 提取 file_path，判断是否工作区内
│   │   ├─ delete_file → 提取 file_path，标记为不可逆
│   │   ├─ web_fetch → 提取 url，判断是否内网地址
│   │   └─ task（子代理）→ 标记为 AGENT 类
│   └─ 输出: ToolContext { tool_name, kind, resource_pattern, params_meta }
│
├─ [L2] deny 规则硬拦截（任何模式不可覆盖）
│   ├─ 合并四层规则: system + user + project + session
│   ├─ 遍历所有 effect="deny" 的规则
│   │   ├─ tool 通配符匹配 AND resource 通配符匹配 → 命中
│   │   └─ 未命中 → 继续
│   ├─ 命中 deny → 最终决策 = DENY（短路，不进入 L3-L5）
│   │   └─ 附加: 记录拒绝原因到审计日志
│   └─ 未命中 → 进入 L3
│
├─ [L3] 只读/安全工具放行
│   ├─ ToolKind ∈ {READ, INTERACT, PLAN} → 最终决策 = ALLOW（短路）
│   ├─ ToolKind = FETCH → 进入 L4（内外网区分暂未实现，统一由 L4 规则和 L5 模式矩阵裁决）
│   └─ 其他 ToolKind → 进入 L4
│
├─ [L3.5] 敏感路径检查（sensitive_paths.py）
│   ├─ 工具参数中的路径命中敏感列表（.git/, .harness/, .bashrc, .ssh/ 等）
│   │   ├─ 当前模式 = yolo → 跳过（yolo 免疫敏感路径检查）
│   │   └─ 其他模式 → 强制标记为需审批（即使 L4 有 allow 规则也弹窗确认）
│   └─ 未命中 → 继续
│
├─ [L4] 规则引擎评估（allow/ask 规则）
│   ├─ 遍历所有 effect="allow" 的规则（按来源优先级 session > project > user）
│   │   ├─ 命中 allow → 记录 base_decision = ALLOW
│   │   └─ 未命中 → 继续
│   ├─ 遍历所有 effect="ask" 的规则
│   │   ├─ 命中 ask → 记录 base_decision = ASK
│   │   └─ 未命中 → 继续
│   ├─ 两级优先级裁决:
│   │   ├─ 有 allow 命中（无论是否有 ask）→ base_decision = ALLOW
│   │   ├─ 仅有 ask 命中 → base_decision = ASK
│   │   └─ 均无命中 → base_decision = None（交由 L5 模式默认）
│   └─ 输出 base_decision 进入 L5
│
├─ [L5] 审批模式覆盖
│   │
│   ├─ [plan 模式]
│   │   ├─ ToolKind ∈ {READ, INTERACT, PLAN, FETCH} → ALLOW（FETCH 不区分内外网，plan 下统一放行）
│   │   └─ 其他 → DENY（plan 模式禁止一切写/执行操作）
│   │
│   ├─ [default 模式]
│   │   ├─ base_decision = ALLOW → ALLOW
│   │   ├─ base_decision = ASK → ASK（弹窗）
│   │   └─ base_decision = None → 按 ToolKind 查默认矩阵:
│   │       ├─ EDIT → ASK
│   │       ├─ EXECUTE → ASK
│   │       ├─ DELETE → ASK
│   │       ├─ AGENT → ASK
│   │       ├─ FETCH(内网) → ASK
│   │       └─ 其他 → ALLOW
│   │
│   ├─ [auto-edit 模式]
│   │   ├─ base_decision = ALLOW → ALLOW
│   │   ├─ base_decision = ASK → ASK（弹窗）
│   │   └─ base_decision = None → 按 ToolKind 查默认矩阵:
│   │       ├─ EDIT → 路径在工作区内且非敏感 → ALLOW（自动批准）
│   │       ├─ EDIT → 路径在工作区外或敏感 → ASK
│   │       ├─ EXECUTE → ASK
│   │       ├─ DELETE → ASK
│   │       ├─ AGENT → ASK
│   │       └─ 其他 → ALLOW
│   │
│   ├─ [auto 模式]（四层过滤器，详见下方）
│   │   ├─ base_decision = ALLOW → ALLOW（规则已允许，不进入过滤器）
│   │   ├─ base_decision = ASK 或 None → 进入 AUTO 过滤器管线
│   │   └─ AUTO 过滤器输出:
│   │       ├─ approved → ALLOW
│   │       ├─ blocked → DENY（或 ASK，取决于回退状态）
│   │       └─ fallback → ASK（回退到手动审批）
│   │
│   └─ [yolo 模式]
│       ├─ base_decision = ALLOW → ALLOW
│       ├─ base_decision = ASK → ALLOW（yolo 将 ask 转为 allow）
│       └─ base_decision = None → ALLOW（yolo 全部放行）
│       （注: L2 deny 已在前面短路，yolo 不可覆盖 deny）
│
├─ [L6] Extension 权限钩子（预留，当前为空）
│   ├─ 主流程决策 = DENY → 不调用钩子，直接返回 DENY
│   ├─ 主流程决策 = ALLOW 或 ASK → 遍历已注册钩子
│   │   ├─ 任一钩子返回 deny → 最终 = DENY
│   │   ├─ 钩子返回 passthrough → 不影响
│   │   └─ 全部 passthrough → 保持主流程决策
│   └─ 无钩子注册 → 保持主流程决策
│
└─ [最终输出]
    ├─ ALLOW → 调用 handler(request)，工具执行
    ├─ DENY → 返回错误 ToolMessage，工具不执行
    └─ ASK → 触发 HITL interrupt，暂停图执行，等待用户审批响应
```

### AUTO 模式四层过滤器详细分支

```
AUTO 过滤器入口（仅当 L5 auto 模式且 base_decision ≠ ALLOW 时触发）
│
├─ [F1] acceptEdits 快速路径
│   ├─ ToolKind = EDIT
│   │   ├─ 路径在工作区内 AND 非敏感路径 → approved(via="acceptEdits")
│   │   └─ 路径在工作区外 OR 敏感路径 → 不命中，继续 F2
│   └─ ToolKind ≠ EDIT → 不命中，继续 F2
│
├─ [F2] 安全工具白名单
│   ├─ ToolKind ∈ {READ, INTERACT, PLAN} → approved(via="safeAllowlist")
│   ├─ ToolKind = FETCH 且 url 为公网 → approved(via="safeAllowlist")
│   └─ 其他 → 不命中，继续 F3
│
├─ [F3] 破坏性命令守卫（仅 EXECUTE/DELETE 类）
│   ├─ ToolKind = EXECUTE:
│   │   ├─ 命令匹配破坏性正则列表:
│   │   │   ├─ `rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/`（rm -rf /）
│   │   │   ├─ `git\s+push\s+.*--force`（git push --force）
│   │   │   ├─ `git\s+reset\s+--hard`（git reset --hard）
│   │   │   ├─ `git\s+clean\s+-[a-zA-Z]*f`（git clean -f）
│   │   │   ├─ `terraform\s+destroy`
│   │   │   ├─ `mkfs\.`（格式化）
│   │   │   ├─ `dd\s+.*of=/dev/`（覆写设备）
│   │   │   ├─ `chmod\s+-R\s+777\s+/`
│   │   │   └─ `>\s*/etc/`（覆写系统文件）
│   │   │   → blocked(via="destructiveGuard", reason="匹配破坏性模式: ...")
│   │   └─ 未匹配 → 不命中，继续 F4
│   ├─ ToolKind = DELETE:
│   │   ├─ 目标为目录且路径深度 ≤ 2（如 /, /home, /usr）→ blocked
│   │   └─ 其他 → 不命中，继续 F4
│   └─ 其他 ToolKind → 不命中，继续 F4
│
├─ [F4] LLM 分类器（实现见 policy/classifier.py + AutoClassifierMiddleware）
│   ├─ 接入方式（与设计树的关键实现说明）:
│   │   ├─ HITL 预检（when 回调）是纯同步的，无法在其中调用 LLM。分类在
│   │   │   模型调用链（AutoClassifierMiddleware.awrap_model_call）完成：
│   │   │   模型返回工具调用后、HITL 裁决前分类，结论按 tool_call id 写入
│   │   │   SafetyClassifier 决策缓存；预检与执行层守卫只读缓存，保证每次
│   │   │   调用最多分类一次。
│   │   ├─ 配置入口：`[approval] classifier = "<models profile 名>"`；
│   │   │   分类模型以 ≤10s 超时、禁用重试构建，不共享主模型连接。
│   │   └─ 未配置 classifier 时 F4 保持占位回退 ask（不启用分类器）。
│   ├─ 前置检查:
│   │   ├─ 分类器未配置/不可用 → fallback(ask)（偏离说明：设计树原写
│   │   │   blocked；ask 同样满足"绝不自动放行"的 fail-closed 不变量，
│   │   │   且让用户保留决定权）
│   │   ├─ 连续拒绝计数 ≥ 3 → fallback(ask)，重置计数，弹窗
│   │   └─ 正常 → 调用分类器
│   ├─ 分类器调用（两阶段）:
│   │   ├─ 第一阶段: 快速判断（低 max_tokens，要求立即回答 allow/block）
│   │   │   ├─ 高置信度 allow → approved(via="classifier")，重置拒绝计数
│   │   │   ├─ 高置信度 block → 进入第二阶段复核
│   │   │   └─ 不确定 → 进入第二阶段复核
│   │   └─ 第二阶段: 复核（更高 max_tokens，允许简短分析后给结论）
│   │       ├─ 最终判断 allow → approved(via="classifier")，重置拒绝计数
│   │       └─ 最终判断 block → blocked(via="classifier")，拒绝计数 +1，
│   │           执行层守卫硬拒绝（不弹窗），审计日志 source=classifier
│   ├─ 异常处理:
│   │   ├─ API 超时（>10s）→ fallback(ask)（fail-closed，同上偏离说明）
│   │   ├─ API 错误/网络异常 → fallback(ask)
│   │   └─ 响应格式异常（两阶段均无法解析）→ fallback(ask)
│   └─ 输出: 决策缓存条目 { tool_call_id → (allow|deny|ask, reason) }
│
└─ [后处理]
    ├─ approved → 返回 ALLOW
    ├─ blocked → 返回 DENY（Agent 收到拒绝原因）
    └─ fallback → 返回 ASK（弹窗让用户手动决定）
```

### ToolKind × 审批模式 默认行为矩阵

当 L4 规则引擎无命中（base_decision = None）时，按此矩阵决定默认行为：

| ToolKind | plan | default | auto-edit | auto | yolo |
|----------|------|---------|-----------|------|------|
| READ | allow | allow | allow | allow | allow |
| INTERACT | allow | allow | allow | allow | allow |
| PLAN | allow | allow | allow | allow | allow |
| EDIT | **deny** | ask | allow* | allow** | allow |
| EXECUTE | **deny** | ask | ask | filter*** | allow |
| DELETE | **deny** | ask | ask | filter*** | allow |
| FETCH(公网) | allow | allow | allow | allow | allow |
| FETCH(内网) | **deny** | ask | ask | filter*** | allow |
| AGENT | **deny** | ask | ask | filter*** | allow |

- `*` auto-edit 的 EDIT allow 条件：路径在工作区内且非敏感路径，否则仍为 ask
- `**` auto 的 EDIT allow 条件：F1 acceptEdits 快速路径通过
- `***` filter = 进入 AUTO 四层过滤器管线（F1→F2→F3→F4）
- FETCH 内网/公网区分暂未实现（延后）：当前按单一 FETCH 类别统一处理——plan/yolo allow，default/auto-edit ask，auto 进过滤器。上表两行 FETCH 为目标设计

### 审批响应后的分支处理

```
用户审批响应到达（interaction.approval response）
│
├─ decision = "approve_once"
│   ├─ 本次调用: 恢复图执行，工具运行
│   └─ 后续同类调用: 无影响，仍触发审批
│
├─ decision = "approve_thread"（allowAlwaysSession）
│   ├─ 规则生成:
│   │   ├─ EXECUTE 工具 → 提取命令前缀 → PermissionRule(tool="execute", resource="git *", effect="allow")
│   │   ├─ 文件写/删工具（write_file/edit_file/delete_file/apply_patch）→ 项目级通配
│   │   │   → PermissionRule(tool="delete_file", resource="*", effect="allow")
│   │   │   （用户明确批准后项目路径内的修改/删除不再反复弹窗；工作区边界由
│   │   │     边界预检短路，L3.5 敏感路径仍强制弹窗，通配不放宽硬性保护）
│   │   └─ 其他 → 工具名通配 → PermissionRule(tool="<tool_name>", resource="*", effect="allow")
│   ├─ 写入 session 规则集（内存）
│   ├─ 本次调用: 恢复图执行
│   ├─ 排队中的其他待审批调用:
│   │   ├─ 匹配新规则 → 自动批准，不弹窗
│   │   └─ 不匹配 → 保持待审批
│   └─ 后续同类调用: L4 命中 session allow → 自动通过
│
├─ decision = "approve_always"（allowAlwaysProject）
│   ├─ 规则生成: 同上
│   ├─ 写入 project 层 .harness/settings.json（持久化）
│   ├─ 本次调用: 恢复图执行
│   ├─ 排队中的其他待审批调用: 同上
│   └─ 后续所有会话: 启动时 load_rules() 加载 → L4 命中 → 自动通过
│
├─ decision = "reject"
│   ├─ 本次调用: 返回拒绝 ToolMessage（"用户拒绝了此操作"）
│   ├─ Agent 收到拒绝，可能选择其他方案
│   └─ 拒绝追踪: 连续拒绝计数 +1（≥3 时注入警告防止死循环）
│
└─ decision = "reject_with_feedback"
    ├─ 本次调用: 返回拒绝 ToolMessage（包含用户反馈文本）
    ├─ 反馈注入: feedback 作为 tool_result 错误消息的一部分返回给 Agent
    ├─ Agent 行为: 读取反馈 → 修改方案 → 重新发起工具调用
    ├─ 重新发起的调用: 完整走一遍 L1→L5 管线（可能再次弹窗）
    └─ 拒绝追踪: 同 reject
```

### 边界情况与异常分支

| 场景 | 处理方式 |
|------|----------|
| 审批超时（timeout_ms 到期） | 视为 reject，返回超时错误 ToolMessage |
| 用户取消 Run（run.cancel） | 中断所有待审批交互，Run 进入 cancelled 状态 |
| 同一 Run 多个工具并发请求审批 | 按请求顺序排队，前一个审批完成后才弹下一个 |
| allowAlways 后排队中有匹配调用 | 自动批准，不再弹窗（见上方 approve_thread 分支） |
| 分类器与规则冲突（规则 allow，分类器 block） | 规则 allow 优先（L4 在 L5 之前，base_decision=ALLOW 不进入过滤器） |
| 敏感路径 + session allow | 敏感路径检查（L3.5）在 L4 之前，即使有 allow 规则仍弹窗（yolo 除外） |
| 工作区外路径 | WorkspaceBoundaryMiddleware 在前置层直接拒绝，不进入审批管线 |
| 畸形工具参数（缺少必要字段） | L1 提取失败时标记为 EXECUTE 类（最严格），进入完整审批流程 |

### Requirement: Extension 权限钩子预留（不实现，仅预留接口）

系统 SHALL 在五层评估管线中预留 Extension/Plugin 权限钩子的扩展点，使后续用户自定义的 Agent 和 Tool 权限逻辑能嵌入主审批流程，但不能越权。

#### Scenario: Extension 只能收紧不能放宽
- **WHEN** 主审批管线（L1→L5）已做出 allow 决策，Extension 钩子返回 deny
- **THEN** 最终结果为 deny（Extension 可以收紧）

#### Scenario: Extension 不能覆盖主流程 deny
- **WHEN** 主审批管线已做出 deny 决策（如 L2 deny 规则命中）
- **THEN** Extension 钩子不被调用，或即使返回 allow 也不生效（不可越权）

#### Scenario: Extension 钩子执行位置
- **WHEN** 主审批管线完成评估后、最终决策返回前
- **THEN** 按注册顺序调用已启用的 Extension 权限钩子，任一返回 deny 则最终为 deny

## MODIFIED Requirements

### Requirement: 审批模式扩展

现有 4 种模式（plan/default/auto-edit/yolo）扩展为 5 种，新增 `auto`。模式切换循环顺序：`plan → default → auto-edit → auto → yolo → plan`。

### Requirement: 规则引擎增强

现有 `permission_rules.py` 的"最后匹配优先"策略改为**两级优先级**策略：第一级按动作类型（deny > allow > ask），第二级同动作内按来源（session > project > user > system）。用户明确批准的操作（allow）不再被配置文件 ask 规则覆盖。

### Requirement: PreToolUseHook 通过 Middleware 实现

审批拦截通过 deepagents/langchain 的 `AgentMiddleware.wrap_tool_call` 实现，等价于 Claude Code 的 PreToolUseHook：
- 中间件在工具实际执行**之前**被调用
- 验证失败时直接返回错误 `ToolMessage`，不调用 `handler`，工具不执行
- 中间件按洋葱模型组合（先注册 = 最外层 = 先执行检查）
- 现有实现：WorkspaceBoundaryMiddleware、ShellAllowListMiddleware、ConcurrencyGuardMiddleware、PlanModeMiddleware
- Extension 权限钩子后续以新增 Middleware 的形式注册，位于主策略中间件**之后**（内层），确保主流程 deny 先生效

## REMOVED Requirements

无。
