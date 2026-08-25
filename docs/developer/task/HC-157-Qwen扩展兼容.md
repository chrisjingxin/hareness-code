---
id: HC-157
title: Qwen扩展兼容
feature_area: Plugin 格式兼容
parent_task: -
decomposed_by: Codex
priority: P0
status: 待验收
owner: 未认领
branch: -
reviewed_at: 2026-08-25
review_due: 2026-09-08
scope: 在保留 Agent Plugins 1.0、Claude 和 Hybrid 行为的前提下，完成 Qwen/DevAgent Extension 的格式识别、静态适配、只读资源快照、Context、Agent 权限、SubagentStop 交互以及目录/ZIP 验收和文档收口；Commands、Skills、MCP 保持静态预览边界。
acceptance: qwen-extension.json 与 devagent-extension.json 能被 auto 或显式 qwen-code 稳定识别；标准默认目录、Context string/string[]、冲突、身份、JSON、路径和 unsupported 字段均有稳定结果；ZA38 清洁 fixture 的 3 Commands、1 Skill、3 Agents、1 MCP、1 Context、1 Hook、真实 Agent frontmatter、Hook matcher、MCP placeholder 和 capability fingerprint 正确；安装快照保持根 references/scripts/mcp 资源闭包和虚拟只读路径；信任并启用后三个 Qwen executor 进入 canonical AgentCatalog/agents.list，DEVAGENT.md 在主 Agent 和对应 Plugin Agent 快照中各注入一次，作为不可覆盖 Core Policy 的 REFERENCE/STABLE Context；approvalMode/permissionMode 只形成能力请求，和父 Agent、Host Policy、workspace 边界取交集，收紧时只能完成离线只读任务；已接入 Agents 从 static preview 去重，Commands/Skills/MCP preview 保留，portable/Claude/Hybrid preview 为空；enabled+trusted、已安装 hooks 报告为 adapted/effective 且 matcher 命中的 Qwen SubagentStop 在同一 Managed child execution 返回父 Agent 前运行，allow/block/reason/additionalContext、submit/continue/skip 语义稳定，invalid/unsupported/async/空同步结果、取消/超时/无客户端、共用校验边界 mismatch 和 runtime 构造漂移均 fail-closed，连续阻断最多八次；清洁目录与 ZIP 经相同 trust+enable 链得到等价资源、Agent、Context、Hook 和静态预览结果；兼容矩阵、用户文档、架构说明和最终验证证据完整；不连接 Qwen MCP、不执行真实 ZA38 Hook。
user_docs: docs/user/插件管理.md
developer_docs: docs/developer/spec/HC-157-Qwen扩展兼容.md、docs/developer/plan/HC-157-Qwen扩展兼容.md、docs/developer/todo/HC-157-Qwen扩展兼容.md、docs/developer/architecture/扩展与插件机制设计方案.md
test_evidence: 第五阶段清洁目录/ZIP 独立临时 home 验收等价：resource counts 3/3/1/1/1/1，启用后 Agents=3、Context=1、Hook=1、HookRuntimeFailure=0，Commands/Skills/Agents/MCP preview=3/1/0/1；Host 全量 53 passed，Qwen+Protocol 81 passed，CLI IPC/Protocol 24 passed，项目脚本 11 passed，protocol:generate/check、typecheck、git diff HEAD --check 通过。Agent 全量 2274 passed、6 skipped，9 个失败均为沙箱禁止 127.0.0.1 监听；CLI 全量 847 passed、1 skipped，剩余失败由 loopback 禁止、只读 worktree Tree-sitter 缓存 EPERM 和两个与 HC-157 无文件交集的既有 Compose 展示断言组成。project/docs/tasks check 仅被既有 HC-130 复核日期阻塞。所有 HC-157 测试均离线，不启动真实 MCP/Hook、不联网、不读取 .env 或凭据。
references: /Users/beichen/Desktop/大模型/github projects/harness-code docs/plans/Harness-Qwen-DevAgent-Extension兼容计划.md；/Users/beichen/Desktop/大模型/za38-cli-extension（只读清洁打包形状参考）
completed_at: -
---

## 问题

Harness 当前只把 Agent Plugins 1.0、Claude Code 和两者 Hybrid 视为 Plugin 格式。ZA38 的 DevAgent Extension 使用根目录 `devagent-extension.json`，同一套生态也可能使用 `qwen-extension.json`；现有自动识别会把它当成无法识别的目录，显式格式契约也无法传递 `qwen-code`。

## 用户结果

- `harness plugins validate <clean-za38-fixture>` 自动识别为 `qwen-code`，并列出实际的组件数量、来源和静态状态。
- `harness plugins validate/install <source> --format qwen-code` 使用同一套 Agent 端 Adapter；显式格式优先，但不会绕过清单冲突或安全校验。
- 同时存在两个 Qwen 家族清单，或 Qwen/DevAgent 清单与 portable/Claude 清单共存时，得到稳定的 `PLUGIN_FORMAT_CONFLICT`。
- Qwen/DevAgent 不会因为普通 `commands/`、`skills/` 或 `agents/` 目录而被 auto 猜测；没有专属清单时仍保持格式歧义/不匹配。
- 标准 `qwen-extension.json` 缺少组件路径时使用存在的根目录 `commands/`、`skills/`、`agents/`；`devagent-extension.json` 仍只使用显式路径。
- Qwen `contextFileName` 缺省或为空数组时只报告实际存在的根 `QWEN.md`；显式 string/string[] 路径继续执行包内普通文件安全校验，DevAgent 保持非空字符串显式路径契约。
- 第一、二阶段已完成格式识别、只读资源快照和静态列表 preview；未接入运行时的 Commands/Skills/MCP 仍是 disabled/static/non-runnable，portable、Claude、Hybrid 不重复投影。
- 第三阶段把已信任启用的 Qwen Context 和 Agent 接入现有 ContextLifecycle、AgentCatalog、ResolvedAgentSpec 与 Policy 交集；第四阶段在同一 Managed child execution 上接入 SubagentStop 与父 Run Interaction，不建立平行 runtime，不连接 MCP。

## 完整范围

1. 阶段一：增加 `qwen-code` Protocol/CLI/Agent 契约，统一解析两个家族清单和静态组件报告。
2. 阶段二：把 Commands、Skills、Agents、MCP、Context 和 Hook 资源转换为只读插件资源快照。
3. 阶段三：接入 Context、Qwen Agent 权限上限和父 Agent/Host Policy 取交集。
4. 阶段四：接入 `SubagentStop`、子 Agent 的 Question/Approval 以及失败关闭/阻断上限。
5. 阶段五：补齐清洁打包验收、用户文档、兼容矩阵、架构说明和全量验证。

## 当前执行边界

- 五个阶段均已完成实现与离线收口，当前状态为待用户验收；前三阶段保持 staged，第四、五阶段保持 unstaged。
- 不修改现有 Claude/portable/Hybrid 运行语义；最终验收只使用清洁目录/ZIP、离线 fake Hook/模型/Interaction，不启动真实 MCP 或用户 ZA38 Hook，不联网，不读取真实 `.env*` 或凭据。
- 用户验收前不归档 Task，不执行 `git add`、`commit`、`push`、`reset` 或 `checkout`。

## 验收

- [x] Protocol schema、生成的 TS/Python 契约和 CLI `--format qwen-code` 通过 focused 检查。
- [x] auto/显式格式优先级、Qwen 家族冲突和 portable/Claude Hybrid 回归通过。
- [x] 坏 JSON、身份字段、路径越界、未知/非空 settings 均稳定失败或明确 `unsupported`。
- [x] ZA38 清洁 fixture 报告 `commands=3`、`skills=1`、`agents=3`、`mcp=1`、`contexts=1`、`hooks=1`，指纹包含这些能力。
- [x] 现有 portable、Claude、Hybrid focused tests 保持通过；第一阶段停点记录到 handoff。
- [x] 第二阶段安装后的 `resource_snapshot(s)` 和对应静态列表 preview 可显示上述组件，使用虚拟 Plugin 路径读取真实根依赖；不启动 MCP/Hook。
- [x] 第三阶段 Context 通过既有 `ContextLifecycle` 生成不可变 `REFERENCE/STABLE` 块；Context-only 包可在 trust+enable 后注入，主 Agent 和对应 Qwen Agent 各只注入一次，未 trust/disabled 不注入。
- [x] 第三阶段三个 ZA38 executor 通过专用 Qwen Markdown frontmatter Adapter 进入实际 `AgentCatalog/agents.list`；`name/description/color/approvalMode` 和 Qwen `permissionMode` 独立映射，未知/类型错误/bypass/冲突在启用前 invalid。
- [x] 第三阶段 Agent 权限与父 Agent、Host Policy/workspace 边界取交集；auto-edit 不是 bypass，收紧时写、Shell、网络、delegation 均不能越权。
- [x] 第三阶段 static preview 按组件去重：Agents 消失，Commands/Skills/MCP 保留，portable/Claude/Hybrid 为空；不接入第四阶段 Hook/交互。
- [x] 第四阶段仅对已安装报告为 `hooks: adapted/effective=true`、enabled+trusted 且 matcher 命中的 Qwen `SubagentStop` 复用既有 `HookRunner` 与父 Run Interaction；submit/continue 在同一 child 再次过 gate，skip 只放行当前 gate，invalid/unsupported/async/空同步结果、取消/超时/无客户端/畸形 Hook 均稳定失败关闭，连续第九次阻断不放行，整个过程保留同一 child provenance。
- [x] 前阶段 control-plane 契约闭合：canonical `agentSummary`、Python/TypeScript generated/validators、Host/CLI 消费和 contract fixtures 同时接受 `color`、`approval_mode`、`permission_mode`；真实 Qwen `agents.list` dispatch 通过；`mcp.status` 的正式结果保留 `static_preview`，畸形 handler 后 dispatch loop 仍可继续。
- [x] 第五阶段清洁目录与 ZIP 经独立临时 home 完成 auto/显式校验、安装、trust+enable 和资源/Agent/Context/Hook/preview 等价验收；用户兼容矩阵、架构说明、review 与全量验证证据已收口。

## 流程说明

仓库要求的 `mattpocock:grill-me`、`agent-skills:spec-driven-development`、`mattpocock:codebase-design`、`agent-skills:planning-and-task-breakdown`、`agent-skills:test-driven-development` 和 `agent-skills:code-review-and-quality` 未出现在本次可用 Skill 列表中。本轮按仓库现有模板和源码证据完成同等的需求固化、规格、计划、TDD 与验证记录；正式 review 和后续阶段留给用户验收后的接力。

## 第一阶段停点证据

- 已完成 `qwen-code` Protocol/CLI/Agent 契约、生成产物、Qwen/DevAgent 静态 Adapter 及清洁 ZA38 fixture。
- Adapter 自动识别或显式 `qwen-code` 均识别根目录唯一家族清单；双家族、跨 portable/Claude 清单、坏 JSON、身份字段和越界路径均有稳定结果；非空 `settings` 与首版非范围字段明确报告 `unsupported`。
- fixture 静态报告为 3 Commands、1 Skill、3 Agents、1 MCP、1 Context、1 Hook；阶段一当时所有报告均为 `unsupported/effective: false`，因此没有接入任何运行时。
- 任务看板同步不是本轮代码失败：仓库已有活动 `HC-156-修复Web刷新回退.md` 与归档 `HC-156-TUI选中复制.md` 重复，项目检查因此未通过；没有修改旧任务以规避该问题。
- 已写入 `tmp/handoff.md`；以上是历史阶段一停点，本轮不提交、不推送。

## 第一阶段返修证据

- 默认目录测试证明：只有 `qwen-extension.json` 的 `name/version` 加上根目录默认 `commands/`、`skills/`、`agents/` 时，Adapter 正确报告对应库存；DevAgent 显式路径回归仍通过。
- 负向测试证明：`command: 123`、空/缺失合法 Hook handler、单文件坏 frontmatter 均不会成为有效 `unsupported` 组件；结果为 `invalid`、count 为 0 且不产生对应有效能力。
- ZA38 fixture 测试锁定三个 Agent 的 `color` 与 `approvalMode: auto-edit`，以及 Hook matcher、command 和 MCP `${extensionPath}${/}` placeholder；第一阶段 descriptor 仍只做静态计数，运行时字段留给后续阶段。

## Context 语义返修证据

- Qwen `contextFileName` 缺省或 `[]` 时，根 `QWEN.md` 存在则报告 `contexts=1`，不存在则不生成 Context；非空 string[] 报告实际的多个包内文件。
- 显式缺失路径返回 `PLUGIN_COMPONENT_MISSING`，绝对路径/`..` 返回既有路径错误；DevAgent 的 ZA38 字符串路径仍通过，数组按已记录契约拒绝。
- 本次只扩展静态 Context 报告，没有读取或注入 Context，也没有进入第二阶段运行时。

## 第二阶段证据

- 安装结果、`plugins.list` 和 `plugins.inspect` 携带脱敏 `resource_snapshot(s)`；ZA38 清洁 fixture 的静态计数为 Commands 3、Skills 1、Agents 3、MCP 1、Context 1、Hook 1。
- 快照复用 Adapter 的相对 `sources`，根 `references/root-guide.md`、`scripts/za38-index.mjs`、`scripts/za38-git-commit-gate.mjs`、`mcp/context-server.mjs` 可通过 `/.harness/plugins/<plugin-id>/...` 读取；Skill `../../references/...` 可解析，未创建 Skill 私有 references。
- MCP/Hook/Command 的已知 placeholder 逐字段解析；所有文件型目标均存在于快照，`..`、未知 token、宿主路径、嵌入式越界、缺失目标稳定拒绝；所有 MCP/Hook 资源 `runnable=false`，未 trust 记录不进 enabled catalog。
- `plugins.list.static_preview` 与 Host 的 `initialize.static_command_preview`、`skills.list.static_preview`、`agents.list.static_preview`、`mcp.status.static_preview` 只对 `qwen-code` 且所有组件 `effective=false` 的记录显示 3/1/3/1 条 disabled/static/non-runnable/read-only 预览，effective 列表不含 Qwen 资产；disabled/enabled portable、Claude、Hybrid 的对应 preview 均为空，已有 runtime catalog 不重复。
- 目录与 ZIP 通过同一 staging/store 链生成等价资源库存；修改或删除源目录不影响已安装快照；隐藏开发文件和凭据命名文件不进入闭包。未接入 Context 注入、Agent 权限/执行、MCP 连接或 Hook 执行。

## 第三阶段返修证据（2026-08-24）

- Context-only Qwen 包的 `contexts` 报告现为 `adapted/effective: true`，`can_enable` 为 true；安装记录在 disabled 或未 trust catalog 时不注入，trust+enable 后经 `PluginManager.context_blocks_by_source` 形成 `REFERENCE/STABLE` block，停用后再次为空。Commands/Skills/MCP/Hook 仍保持静态边界。
- Qwen `permissionMode` 已与 `approvalMode` 分开解析：`default→default`、`plan→plan`、`acceptEdits→auto-edit`、`auto→auto-edit`、`dontAsk→default`；`bypassPermissions`、未知/类型错误值和两个字段映射冲突均在组件报告中稳定为 `invalid`、`can_enable=false`。
- Host 离线集成测试经 `_prepare_run` 与 `_plugin_delegation_targets` 的真实 ContextLifecycle 调用证明主 Agent 与对应 Managed Plugin Agent 各收到一次 DEVAGENT block；没有直接启动模型、MCP 或 Hook。
- `tests/test_qwen_plugins.py` 返修专项 14 passed；最终完整 Qwen focused 69 passed，覆盖 Hook mismatch、损坏 adapted 报告和 Host fail-closed child；跨包 focused 结果和项目检查阻塞记录写入 Todo 与 `tmp/handoff.md`。

## 第四阶段证据（2026-08-24）

- 合法 Qwen `SubagentStop` Hook 报告为 `adapted/effective: true`，只在 enabled+trusted catalog 中由既有 `HookRunner` 构造；matcher 不命中、未启用或未 trust 均不执行，Commands/Skills/MCP 仍保持静态 preview。
- `ManagedAgentExecutor` 在同一 runtime/checkpoint 上提供最终输出 gate；block 的 reason/additionalContext 通过既有父 Run question 通道返回，submit/continue 都在同一 child 中开始下一回合并再次过 gate，skip 才直接放行当前 gate；三种选择均保留 `execution_id`、`parent_execution_id`、`agent_id`，不会创建第二个 child execution。
- `ChildInteractionRegistry` 按 Run 登记请求并在响应、取消、断连、父 Run 终态时清理；无客户端、超时、取消、无效响应、畸形/非零/异常 Hook 和第九次连续阻断均稳定失败关闭，Shell/Git 仍走 Harness 原有 policy/approval 链。
- 所有新增自动化均使用离线 fake Hook、fake graph 和 fake InteractionPort；没有执行用户 ZA38 脚本、连接 MCP、调用真实模型、联网或读取 `.env`/凭据。第四阶段验证命令与剩余风险记录在 `tmp/handoff.md`。

## 第五阶段收口证据（2026-08-25）

- 使用全新临时目录分别从清洁 fixture 目录和带单层包根的 ZIP 执行 auto/显式 `qwen-code` 校验、安装、fingerprint trust+enable；两种来源均得到相同组件状态、资源计数、3 个 Agent source、1 个 `REFERENCE/STABLE` Context、1 个同步 SubagentStop Hook、0 个 Hook runtime failure，以及 Commands/Skills/Agents/MCP 为 3/1/0/1 的启用后预览。
- 交付 diff review 未发现新的 HC-157 行为、安全、协议或兼容阻塞；Qwen 仍只通过 Adapter 进入 canonical PluginManager、ContextLifecycle、AgentCatalog、HookRunner、ManagedAgentExecutor 和 Protocol 路径，没有第二套运行时。
- 独立回归：`tests/host/test_server.py` 53 passed；Qwen+Python Protocol 81 passed；CLI IPC/Protocol 24 passed；项目脚本 11 passed；`protocol:generate`、`protocol:check`、`typecheck`、`git diff HEAD --check` 通过。
- Agent 全量为 2274 passed、6 skipped、9 failed；失败全部来自沙箱拒绝 `127.0.0.1:0` attachment listener。CLI 全量为 847 passed、1 skipped，loopback Web 用例受沙箱限制，Tree-sitter 用例因只读 worktree 无法创建 `.test-tree-sitter`；另有两个 Compose 展示断言在 HC-157 未触碰的 TUI 文件中独立复现，作为无关既有问题记录，未扩大本任务修复。
- `docs:check`、`tasks:check`、`project:check` 只被活动任务 HC-130 的复核日期 `2026-08-23` 阻塞；未修改 HC-130 规避检查。当前无需 VERSION/CHANGELOG 变更，Task 保持待验收且不归档、不提交、不推送。
