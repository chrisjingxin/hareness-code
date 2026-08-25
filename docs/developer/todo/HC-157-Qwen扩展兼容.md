# HC-157 Qwen 扩展兼容 Todo

关联 [Task](../task/HC-157-Qwen扩展兼容.md)、[Spec](../spec/HC-157-Qwen扩展兼容.md) 与 [Plan](../plan/HC-157-Qwen扩展兼容.md)。

## 可演示停点：Qwen/DevAgent 静态校验

- [x] 在 `packages/protocol/schema/v3.json` 增加 `qwen-code`，执行 `bun run protocol:generate`，确认生成产物和 `protocol:check` 一致。
- [x] 在 CLI `--format` 解析和参数测试中支持 `qwen-code`，未知格式仍在启动 sidecar 前失败。
- [x] 先补 Qwen/DevAgent Adapter 的失败测试：双家族清单、跨格式冲突、显式不匹配、坏 JSON、身份字段、越界路径和 unsupported 字段。
- [x] 新增离线清洁 fixture 或从受控临时目录构造 fixture，确认 ZA38 组件报告为 3 Commands、1 Skill、3 Agents、1 MCP、1 Context、1 Hook。
- [x] 实现统一 Qwen/DevAgent 静态 Adapter；阶段一当时所有组件只报告 `unsupported/effective: false`，不接入 Context、Agent、MCP、Hook 或资源运行时。
- [x] 运行 focused Agent/CLI/Protocol 测试及按影响范围选择的 typecheck/project checks，记录精确结果。
- [x] 更新 Task 第一阶段证据和本文件勾选项，写入 `tmp/handoff.md`，停止等待用户验收。

## 第一阶段证据（2026-08-21）

- TDD 红灯已确认：新增 CLI `qwen-code` 参数断言在实现前因旧格式白名单失败；Agent focused 测试在依赖安装前因本地缺少 `.venv` 无法启动。
- `cd packages/agent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_qwen_plugins.py`：9 passed。
- `cd packages/agent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_plugins.py`：35 passed；`tests/test_plugin_fixtures.py tests/test_full_demo_plugin.py`：28 passed。
- `cd packages/agent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/protocol/test_protocol_contract.py`：12 passed。
- `cd packages/cli && bun test tests/args.test.ts tests/index.test.ts tests/ipc/client.test.ts tests/ipc/protocol-contract.test.ts`：39 passed、0 failed、531 expect；`bun run typecheck`：通过。
- `bun run protocol:generate`、`bun run protocol:check`、`git diff --check`：通过；协议生成产物已同步。
- `bun run tasks:sync`、`bun run docs:check`、`bun run tasks:check`、`bun run project:check` 共同被仓库既有活动/归档重复任务 `HC-156` 阻塞，未修改旧任务或手写看板绕过检查。
- 实际 `bun run dev -- plugins validate packages/agent/tests/fixtures/qwen_extensions/za38-devagent --format qwen-code` 受当前沙箱禁止写入 `~/.harness/plugins` 阻塞；静态路径通过 `PluginManager(home=临时目录)` 的离线测试验证，未将该环境错误当作 Adapter 失败。

## 第一阶段返修（2026-08-21）

- [x] 对 `qwen-extension.json` 增加缺省 `commands/`、`skills/`、`agents/` 目录语义；`devagent-extension.json` 保持显式路径语义，并验证无专属清单时不做目录猜测。
- [x] 拒绝 `mcpServers.command` 非字符串、空 Hook handler、缺失合法 `type`/`command` 的 Hook，并让畸形组件为 `invalid`、不计入有效数量或能力。
- [x] 让 manifest 直指单个 `SKILL.md` 时复用目录扫描相同的 frontmatter、description 和正文校验。
- [x] 恢复 ZA38 三个 Agent 的 `color`、`approvalMode: auto-edit`，并用测试锁定 Hook matcher、command 与 MCP `${extensionPath}${/}` placeholder 形状。
- [x] 返修后重跑 Qwen、既有 Plugin/fixture/full-demo/Protocol focused 测试；项目级检查仍单独记录 HC-156 重号阻塞。

返修验证：`packages/agent` 的 `tests/test_qwen_plugins.py` 19 passed；`tests/test_plugins.py` 35 passed；
`tests/test_plugin_fixtures.py tests/test_full_demo_plugin.py` 28 passed；
`tests/protocol/test_protocol_contract.py` 12 passed。CLI 四文件 focused 为 39 passed、0 failed、531 expect；
`protocol:generate`、`protocol:check`、`typecheck`、`git diff --check` 通过。

本次 Context 语义返修后的完整证据以本文“Context 语义返修”节和 `tmp/handoff.md` 为准。

## Context 语义返修（2026-08-21）

- [x] `qwen-extension.json` 的 `contextFileName` 支持 string/string[]；缺省或空数组只在根 `QWEN.md` 实际存在时报告一个 Context。
- [x] 保持 DevAgent 的非空字符串显式路径契约；不凭未确认的 DevAgent 语义推断默认文件或数组语义。
- [x] 显式 Context 路径继续拒绝绝对路径、`..`、symlink、目录和缺失文件；默认 `QWEN.md` 缺失则不报告组件。
- [x] 增加离线默认 Context、空数组、string[] 多文件、显式缺失/越界和 DevAgent 字符串契约测试。
- [x] Context 返修后重跑 Qwen、既有 Plugin/fixture/full-demo/Protocol、CLI、Protocol、typecheck 和 diff focused 检查。

查看方式：使用清洁 ZA38 打包目录执行 `harness plugins validate <path>`；确认 JSON 结果的 `plugin.format` 与组件数量、诊断和 `capability_fingerprint`，不要执行 install 后启动任何外部服务。

## 第二阶段：静态组件到只读资源快照（2026-08-21）

- [x] 复用 Qwen Adapter 的 canonical 相对 `sources`，从已安装内容寻址 store 建立 `PluginResourceSnapshot`。
- [x] 快照按真实 ZA38 包关系保留根 `references/`、`scripts/`、`mcp/`，捕获 Commands、Skills、Agents、Context 普通文件，以及 MCP/Hook 静态逻辑条目；不纳入伪造的 Skill 私有 `references/`。
- [x] 通过 `/.harness/plugins/<plugin-id>/...` 提供虚拟来源；摘要不包含 store/source 宿主绝对路径，读取拒绝绝对路径、`.`、`..`、未知资源和越界。
- [x] 在 Command 内容、MCP 已知字段和 Hook handler command 中覆盖 `<extensionPath>`、`${extensionPath}`、`${extensionPath}${/}`、`${CLAUDE_PLUGIN_ROOT}`；嵌入式越界、未知 token、宿主路径和缺失目标 fail closed；不启动 MCP、不执行 Hook。
- [x] 安装、`plugins.list/inspect` 和现有 Host 列表链路暴露静态 preview；阶段二当时只筛选 Qwen/DevAgent 且全部组件
  `effective=false` 的记录，`commands=3`、`skills=1`、`agents=3`、`mcp=1` 均标记
  disabled/static/non-runnable/read-only；portable、Claude、Hybrid preview 为空，未 trust 的 Qwen 记录不进入 enabled catalog。
- [x] 用清洁 ZA38 目录与 ZIP 覆盖根依赖闭包、四种 placeholder、相对引用、源目录删除/篡改、隐藏/凭据命名文件排除和只读边界。
- [x] 更新 HC-157 Task/Spec/Plan/架构/用户文档与 `tmp/handoff.md`，明确对应列表可见而非仅 resource snapshot。

阶段二验证证据在下方“第二阶段返修证据”节维护；本阶段未接入 Context 注入、AgentCatalog/权限、
MCP 连接或 Hook runtime。

## 第二阶段返修证据（2026-08-21）

- [x] 删除 `skills/za38-framework/references/` 伪造目录，使用根 `references/root-guide.md`、
  `scripts/za38-index.mjs`、`scripts/za38-git-commit-gate.mjs` 和 `mcp/context-server.mjs` 清洁 fixture；
  快照能读取四个目标并保留 Hook matcher/command 形状。
- [x] 静态 placeholder 只在明确 Command/MCP/Hook 字段解析；逐个规范化并验证包内目标已入快照，覆盖
  `--file=...`、引号命令、`..`、未知根 token、宿主绝对路径、缺失目标和四种已知 token。
- [x] 通过 `PluginManager.static_preview` 与 Host 的 `initialize`、`skills.list`、`agents.list`、
  `mcp.status` 结果分别证明 ZA38 的 3 Commands、1 Skill、3 Agents、1 MCP 可见且全部 disabled/static/
  non-runnable/read-only；effective 列表保持为空。
- [x] 离线回归证明 disabled/enabled portable、Claude、Hybrid 不进入四类 static preview；未来 Qwen 组件若
  出现 `effective=true` 也不重复投影，Host 四个 preview 仍只保留 Qwen 未接入运行时资源。
- [x] 覆盖目录/ZIP 同一 staging/store 链、Skill `../../references/...` 读取、源目录删除/篡改后快照保留、
  隐藏/凭据命名文件排除；所有测试使用离线 fixture/mock。

最终验证：Qwen focused 34 passed；Plugin focused 37 passed（含新增跨格式 preview 回归）；fixture/full-demo
28 passed；Protocol 12 passed；
Host 列表 focused 4 passed、48 deselected；CLI focused 39 passed、0 failed、531 expect；
`bun run protocol:generate`、`bun run protocol:check`、`bun run typecheck`、`git diff --check` 通过。
`bun run project:check` 仍因既有 HC-156 活动/归档任务重号失败。

## 阶段三：Context 与 Agent 权限

- [x] 先补 Qwen Agent catalog、Context lifecycle、Host list 的失败测试，再实现最小 canonical seam；仓库专用 TDD Skill 不在当前环境，已按同等红灯→实现→回归流程执行。
- [x] Qwen Agent Markdown 专用解析：三个 ZA38 executor 进入 `AgentCatalog/agents.list`，保留 `name/description/color/approvalMode`，按 Qwen 源枚举独立映射 `permissionMode`（default/plan/acceptEdits/auto/dontAsk），处理受支持工具/模型/turn 子集，未知/类型错误/bypass/冲突模式在 validate/install 前 invalid。
- [x] Qwen Agent 请求通过既有 `ExecutionPolicyDefinition` 与父 Agent/Host/workspace policy 求交集；auto-edit 不等于 bypass，默认未请求的 write/Shell/network/MCP/delegation 关闭。
- [x] Context 从 enabled+trusted `PluginResourceSnapshot` 读取，合法 Context 报告 `adapted/effective=true` 并接入既有 `ContextLifecycle` 为 `REFERENCE/STABLE` block；Context-only 包可启用，disabled/untrusted 不注入，主 Agent 与对应 Plugin Agent 各只注入一次，使用虚拟来源路径并保持 Run snapshot 不可变。
- [x] Host 离线集成测试经 `_prepare_run` 与 Managed Plugin Agent delegation seam 验证两条 canonical ContextLifecycle 调用链各只接收一次同一 Context block。
- [x] 阶段二 preview 改为组件粒度：effective Agents 从 `agents.static_preview` 去重，Commands/Skills/MCP 保留；portable/Claude/Hybrid 不重复投影。
- [x] 保持第三阶段边界：没有真实模型、MCP 连接、Hook/SubagentStop、Question/Approval 交互或网络/凭据读取。

## 阶段四：SubagentStop 与子 Agent 交互

- [x] 先以 `tests/runtime/test_subagent_stop.py` 锁定失败语义：allow、matcher 命中/不命中、disabled/untrusted、畸形/非零/异常 Hook、reason/additionalContext、提交/继续修改/一次性跳过、无客户端/过期/取消/无效响应和第九次上限。
- [x] 复用 `HookRunner` 的受控输入输出边界与 Qwen runtime catalog；合法 `SubagentStop` component 报告 `adapted/effective`，不执行真实 ZA38 Hook，Commands/Skills/MCP 仍保持静态 preview。
- [x] runtime 只消费已安装报告中 `hooks: adapted/effective=true` 的 Qwen 组件；invalid、unsupported、混合坏条目、范围外事件和 `async:true` 均不进 runtime，精确命中的空/关闭 runner 结果 fail-closed，matcher miss 不建立 gate。
- [x] Adapter/runtime 复用共用 Hook 纯校验 seam，覆盖 matcher 513、command 32769、timeout `true/601`、shell `fish`、`args` 类型错误和 Qwen 未支持 `env`；损坏的 adapted 报告、runtime 转换失败或空定义产生可观察 `PLUGIN_HOOK_*` failure，并由 Host fail-closed，不静默放行。
- [x] 在 `ManagedAgentExecutor` 增加最终输出 gate；submit 注入有界提交门禁指令、continue 注入不可信反馈，二者在同一 runtime/checkpoint 开始下一模型回合并再次过 gate；skip 只放行当前 gate，不改变 EffectivePolicy 或 Shell/Git 审批。
- [x] 在 `RunCoordinator` 增加 Run 级 `ChildInteractionRegistry`，question 复用既有 owner channel，保留 child provenance，并覆盖响应、取消、超时、无客户端、父 Run 结束、owner 断连清理。
- [x] 离线集成断言同一 `execution_id`/`parent_execution_id`/`agent_id` 在 block→continue→allow 链路中不变；连续八次可继续，第九次稳定 `SUBAGENT_STOP_BLOCK_LIMIT`，不创建第二个 child execution。
- [x] 闭合前阶段 Host control-plane 契约：`agentSummary` 的 `color`、`approval_mode`、`permission_mode` 已从 canonical schema 生成到 Python/TypeScript 类型、validators、Host/CLI consumer 和 contract fixture；真实 Qwen `agents.list` dispatch 通过，`mcp.status` 保留 `static_preview: []`，畸形 handler 后 dispatch loop 继续工作。

**可演示停点与查看方式**

使用清洁 ZA38 fixture、fake Hook runner 和 fake InteractionPort 运行第四阶段 focused 测试；观察
`SubagentStopController` 产生合法 question 选项 `submit/continue/skip`，以及
`ManagedAgentExecutor` 两次回合使用同一 checkpoint namespace。不会启动 fixture 脚本、MCP、网络或真实模型。

## 第四阶段证据（2026-08-24）

- `tests/runtime/test_subagent_stop.py`：16 passed；覆盖 gate、合法空 JSON、未知 gate 动作、matched-empty/closed runner、matcher、Managed executor、submit 二次回合、RunCoordinator registry、断连/取消/无效响应和失败关闭路径。
- `tests/test_qwen_plugins.py`：Qwen fixture 现验证 Hook `adapted/effective` 与可信 runtime catalog；参数化覆盖 matcher/command/timeout/shell/args/env mismatch、损坏 adapted 报告和 Host child fail-closed；既有 Context/Agent/preview 回归保持通过。
- `tests/test_full_demo_plugin.py`：Hybrid 既有 PreToolUse Hook 回归通过；Hybrid 合并 manifest 展示摘要不会被误当作 runtime 文件路径。
- Qwen/Plugin/fixture/full-demo 合并 focused 138 passed；Managed Agent/Plugin runtime/Coordinator/Approval/Side-question 合并 focused 181 passed；Host plugin/agent/context/interaction 10 passed，`tests/host/test_server.py` 全量 53 passed；Protocol Python 12 passed，CLI IPC/protocol focused 24 passed；`protocol:generate`、`protocol:check`、`typecheck`、`git diff --check` 通过。前阶段 control-plane schema mismatch 已闭合；`project:check`、`docs:check`、`tasks:check` 若仍失败只记录既有 HC-130 复核日期，不修改旧任务；本阶段不进入第五阶段。

## 第三阶段证据（2026-08-24）

- `tests/test_qwen_plugins.py`：Context-only effective/trust 门禁、Qwen Agent canonical catalog、五种 permissionMode 映射、bypass/未知/类型错误/冲突 invalid、Context `REFERENCE/STABLE`、Host 主/子 Agent 各一次注入、Host 四列表 component-level preview 和受限 policy 交集均有离线断言。
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_qwen_plugins.py`：55 passed；指定 Agent/Plugin/fixture/full-demo/AgentCatalog/Context/Protocol 合并命令：240 passed。
- `tests/host/test_server.py -k 'plugin or agents or context'`：9 passed、43 deselected；`tests/protocol/test_protocol_contract.py`：12 passed。
- CLI focused：39 pass、0 fail、531 expect；`bun run protocol:generate`、`bun run protocol:check`、`bun run typecheck`、`git diff --check` 通过。
- `bun run project:check`、`bun run docs:check`、`bun run tasks:check` 均仅被既有 HC-130 复核日期 `2026-08-23` 阻塞，未修改 HC-130。
- 未连接真实 MCP、未执行 Hook、未调用真实模型、未联网、未读取 `.env` 或凭据；不进入阶段四。

## 阶段五：收口

- [x] 使用清洁目录/ZIP 完成最终验收，更新用户插件文档、兼容矩阵与扩展架构文档。
- [x] 完成完整 diff review、任务证据和全量验证记录；任务转为待验收，在用户明确要求前不提交或推送。
- [ ] 用户实测通过后再同步看板并归档 Task；不得提前把待验收描述成已完成。

## 第五阶段证据（2026-08-25）

- 清洁目录与带单层包根 ZIP 分别使用独立临时 home 执行 auto/显式 `qwen-code` 校验、安装、trust+enable；两者均得到 `agents=3`、`commands=3`、`contexts=1`、`hooks=1`、`mcp=1`、`skills=1` 的资源快照，启用后 Agent/Context/Hook 为 3/1/1、Hook runtime failure 为 0，Commands/Skills/Agents/MCP preview 为 3/1/0/1。
- 完整交付 diff review 未发现新的 HC-157 阻塞；未见第二套 Qwen runtime、秘密或宿主路径写入、权限绕过、静默 Hook 放行或 portable/Claude/Hybrid 路径改写。
- `tests/host/test_server.py`：53 passed；Qwen+Python Protocol：81 passed；CLI IPC/Protocol：24 passed；项目脚本：11 passed；`bun run protocol:generate`、`bun run protocol:check`、`bun run typecheck`、`git diff HEAD --check` 通过。
- Agent 全量：2274 passed、6 skipped、9 failed；9 个失败均为沙箱禁止绑定 `127.0.0.1:0`。CLI 全量：847 passed、1 skipped；其余失败来自 loopback 禁止、只读 worktree Tree-sitter 缓存 EPERM，以及两个在 HC-157 未修改 TUI 文件中独立复现的 Compose 展示断言。
- `docs:check`、`tasks:check`、`project:check` 仅被既有 HC-130 复核日期 `2026-08-23` 阻塞；不修改旧任务规避。Task 保持待验收，不归档、不暂存、不提交、不推送。
