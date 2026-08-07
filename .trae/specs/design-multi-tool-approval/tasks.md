# Tasks

- [x] Task 1: 扩展审批模式为 5 种（新增 auto，预留 bubble）
  - [x] SubTask 1.1: 修改 `packages/agent/harness_agent/policy/approval_mode.py`，新增 `AUTO = "auto"` 枚举值，更新解析逻辑和模式切换循环顺序（plan → default → auto-edit → auto → yolo → plan）；在枚举中预留 `BUBBLE = "bubble"` 但标记为未启用（解析到 bubble 时降级为 default 并输出警告日志），为后续子代理冒泡功能保留扩展点
  - [x] SubTask 1.2: 更新 `packages/agent/harness_agent/policy/tool_risk.py` 中的默认行为矩阵，为 auto 模式添加对应列（EDIT→filter, EXECUTE→filter, DELETE→filter, READ→allow）
  - [ ] SubTask 1.3: 补充单元测试验证 5 种模式的解析、切换和未知值降级行为

- [x] Task 2: 重构五层评估管线（L1→L2→L3→L4→L5）
  - [x] SubTask 2.1: 重构 `packages/agent/harness_agent/policy/approval_policy.py` 中的 `evaluate_permission()`，明确分层：L1(工具 ToolKind 声明) → L2(deny 规则硬拦截) → L3(只读放行) → L3.5(敏感路径) → L4(规则引擎 allow/ask) → L5(模式覆盖) → L6(Extension 钩子)
  - [x] SubTask 2.2: 修改 `permission_rules.py` 的匹配策略，从"最后匹配优先"改为两级优先级：第一级 deny > allow > ask（deny 命中立即返回；无 deny 时 allow 优先于 ask）；第二级同动作内按来源 session > project > user > system
  - [x] SubTask 2.3: 确保 L2 deny 在任何模式下（包括 yolo）都不可覆盖；L5 yolo 模式将 ask 转为 allow
  - [ ] SubTask 2.4: 补充管线各层的单元测试（deny 不可覆盖、yolo 放行 ask、plan 阻止写操作、规则优先级）

- [x] Task 3: 实现 AUTO 模式四层过滤器
  - [x] SubTask 3.1: 新建 `packages/agent/harness_agent/policy/auto_mode.py`，实现 `evaluate_auto_mode()` 入口函数，返回 `AutoModeDecision(via, should_block, reason)`
  - [x] SubTask 3.2: 实现 F1 acceptEdits 快速路径：EDIT 类工具 + 工作区内路径 + 非敏感路径 → approved
  - [x] SubTask 3.3: 实现 F2 安全工具白名单：READ/INTERACT/PLAN 类工具 → approved
  - [x] SubTask 3.4: 实现 F3 破坏性命令守卫：正则匹配 `rm -rf /`、`git push --force`、`git reset --hard`、`git clean -f`、`terraform destroy` 等 → blocked
  - [x] SubTask 3.5: 实现 F4 两阶段 LLM 分类器（`policy/classifier.py`：一阶段快速判断 + 二阶段复核、连续拒绝回退、决策缓存）；经 `AutoClassifierMiddleware` 在模型响应阶段分类，预检与执行层守卫读缓存裁决；`[approval] classifier = "<profile>"` 配置，未配置/失败回退 ask（fail-closed）
  - [x] SubTask 3.6: 实现连续拒绝回退机制：连续 3 次 blocked → fallback 到手动审批
  - [x] SubTask 3.7: 在 `approval_policy.py` 的 L5 层集成 auto 模式：当模式为 auto 且结果为 filter 时调用 `evaluate_auto_mode()`
  - [ ] SubTask 3.8: 补充 auto 模式各层的单元测试（快速路径、白名单、破坏性守卫、回退机制）

- [x] Task 4: 完善审批选项处理（allowAlwaysSession + allowAlwaysProject + deny+feedback）
  - [x] SubTask 4.1: 在 `packages/agent/harness_agent/host/run_coordinator.py` 中完善 `interaction.approval` 响应处理：decisions 扩展为 5 种，`_resume_value` 区分 approve/reject/feedback
  - [ ] SubTask 4.2: 实现规则生成逻辑：根据当前工具名 + 参数特征自动生成 allow 规则
  - [ ] SubTask 4.3: 实现 allowAlwaysProject 的持久化写入：调用 `permission_rules.save_rule()` 写入 `.harness/settings.json`
  - [x] SubTask 4.4: 实现 deny+feedback 的重确认流程：`_resume_value` 将 feedback 附加到 resume 值中
  - [ ] SubTask 4.5: 实现 allowAlways 后自动批准排队中匹配新规则的其他待审批工具调用
  - [ ] SubTask 4.6: 补充审批选项处理的集成测试（覆盖 5 种选项的完整行为）

- [x] Task 5: 规则持久化四层作用域
  - [x] SubTask 5.1: 扩展 `permission_rules.py`，支持 session/project/user/system 四层作用域；`RuleScope` 新增 `"system"`，`PermissionRule` 新增 `scope` 字段
  - [x] SubTask 5.2: 实现规则加载合并逻辑：`load_rules()` 加载四层，`merge_rules()` 合并为 flat list；system 层只读（`save_rule` 对 system 直接 return）
  - [ ] SubTask 5.3: 实现规则 DSL 解析：`Bash(git *)` → tool=execute, pattern="git *"；`Edit(src/**)` → tool=edit, pattern="src/**"；`Read(.env)` → tool=read, pattern=".env"
  - [ ] SubTask 5.4: 补充规则加载、合并、匹配、持久化的单元测试

- [x] Task 6: Extension 权限钩子接口预留（不实现功能，仅预留扩展点）
  - [x] SubTask 6.1: 在 `evaluate_permission()` 返回最终决策前，预留 `_extension_permission_hooks` 调用点（当前为空列表，直接跳过）；定义 `ExtensionPermissionHook` Protocol 接口
  - [x] SubTask 6.2: 确保调用点逻辑：主流程 deny → 不调用钩子直接返回；主流程 allow/ask → 遍历钩子，任一返回 deny 则最终为 deny
  - [ ] SubTask 6.3: 在 `runtime/agent.py` 中间件注册顺序中预留 Extension Middleware 的位置注释（位于主策略中间件之后）
  - [ ] SubTask 6.4: 补充单元测试验证钩子接口的收紧/不可放宽语义

- [x] Task 7: 协议与 CLI 联调验证
  - [x] SubTask 7.1: 确认 `packages/protocol/schema/v3.json` 中 `interaction.approval` 的选项枚举与实现一致（approve_once/approve_thread/approve_always/reject/reject_with_feedback）
  - [x] SubTask 7.2: CLI 端审批弹窗已渲染五种选项（允许一次/本线程允许/永久允许/拒绝/拒绝并反馈），值与协议一致
  - [ ] SubTask 7.3: 端到端验证：default 模式下 Bash 命令弹窗 → 用户选择各选项 → 验证行为正确（特别是 allowAlwaysProject 写入文件、allowAlwaysSession 仅内存）

- [x] Task 8: 修复审批响应规则生成与反馈注入（验证 FAIL 项修复）
  - [x] SubTask 8.1: 在 `run_coordinator.py` 中实现审批响应后的规则生成：`approve_thread` 写入 session 内存；`approve_always` 调用 `save_rule()` 持久化到 project 层
  - [x] SubTask 8.2: 修复 `reject_with_feedback` 的反馈注入：将 feedback 放入 decisions 的 `args.message` 字段
  - [x] SubTask 8.3: 在 `evaluate_rules` 中实现第二级作用域优先级排序
  - [x] SubTask 8.4: 补充修复后的单元测试（9 个新测试通过）

# Task Dependencies

- Task 2 依赖 Task 1（需要 auto 模式枚举存在）
- Task 3 依赖 Task 2（需要五层管线框架）
- Task 4 依赖 Task 2（需要规则引擎和管线）
- Task 5 依赖 Task 2（需要规则引擎框架）
- Task 6 依赖 Task 2（需要 evaluate_permission 管线存在）
- Task 7 依赖 Task 3、Task 4、Task 5（需要所有功能就绪）
- Task 3、Task 4、Task 5、Task 6 之间无依赖，可并行
