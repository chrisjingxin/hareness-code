---
id: HC-158
title: 对齐Qwen插件运行时
feature_area: Plugin 运行时兼容
parent_task: HC-157
decomposed_by: Codex
priority: P0
status: 待验收
owner: 未认领
branch: -
reviewed_at: 2026-08-25
review_due: 2026-09-08
scope: 在 HC-157 的 Qwen/DevAgent 安装、信任和不可变快照基础上，按独立停点接入 Commands、Skills、MCP、可映射 Hook、LSP 与 Settings；所有组件只进入 Harness canonical runtime，不建立 Qwen 私有执行循环。当前 ZA38 插件未声明 Channels，2026-08-28 用户决定本 Task 不实现 Channel runtime，未来出现真实需求时另立安全架构任务。
acceptance: 首先关闭 unsupported Qwen LSP/Monitor 误入通用 runtime 的门禁缺口；trusted+enabled ZA38 插件的三个 Markdown Commands 以自然短名进入 Slash Command Registry，冲突时稳定命名空间化，命令正文从 Host 不可变快照展开且正确处理 {{args}}；za38-framework 进入 canonical SkillRegistry 并可由命令和模型按权限读取；za38.03_code_index 经 canonical MCP lifecycle 受控启动、命名空间化并可观察失败；Qwen Hook/LSP/Settings 只有在各自 component 报告 adapted/effective 且存在 canonical consumer 时才运行；Adapter report revision 变化时已安装 package 必须由当前 Adapter 重解析，能力指纹变化保留旧 trust 并仅在实际请求 stale Plugin 能力时要求显式 reauthorization，普通 Run/内置能力/其他已授权 Plugin 继续可用；Qwen SubagentStop Hook 的执行级失败只告警并返回已完成 child，正常 blocking 继续同一 child，blocking cap 返回结果和 warning，取消及 PreToolUse/Policy/MCP/非 Qwen 安全边界保持既有语义；Channels 保持明确 unsupported/effective=false，且不阻塞本 Task 收口；每阶段具备离线恶意输入、Protocol/Host/CLI 回归和独立回滚证据。
user_docs: docs/user/插件管理.md
developer_docs: docs/developer/spec/HC-158-对齐Qwen插件运行时.md、docs/developer/plan/HC-158-对齐Qwen插件运行时.md、docs/developer/todo/HC-158-对齐Qwen插件运行时.md、docs/developer/architecture/扩展与插件机制设计方案.md
test_evidence: "Phase 2/3 联合返修：tests/test_qwen_phase3.py 12 passed；tests/test_qwen_mcp_phase2.py 14 passed；两者合计 26 passed；tests/test_qwen_plugins.py + tests/test_plugin_runtime.py 合计 98 passed；canonical MCP 72 passed；HC-157 SubagentStop 16 passed；Run cancellation 39 passed；三个主文件 150 passed；portable/Claude/Hybrid runtime 31 passed、29 deselected；full-demo 1 passed；Host Plugin/Skill/Run 12 passed、44 deselected；CLI/Protocol focused 62 pass；Python Protocol contract 12 passed；protocol:check、typecheck、git diff --check 通过。新增真实 fake stdio MCP 子进程闭环覆盖 initialize/tools-list/tools-call/cancel/close；新增 Host root/Managed child feedback lifecycle、取消不调用模型、Qwen executable+argv 跨平台解析、Claude PATH 保持和 LSP EOF/header/body/JSON 归一化回归。真实 ZA38 checkout 只读临时 home 安装+enable 得到 Commands 3、Skill 1、MCP config 1，MCP conversion diagnostics 为空，static preview 三类均为 0；净化 store 有 122 个 references/origin 条目，runtime snapshot 无 origin。Phase 4B Settings/Host focused 61 passed，Plugin runtime/MCP/LSP focused 35 passed，portable/Claude/Hybrid 与 Plugin 主集合 155 passed，Phase 0-4B Python 集合 322 passed，canonical MCP/Host/Run/SubagentStop 185 passed，CLI/Protocol focused 80 pass；v3.7 protocol:check、typecheck、uv lock --check --offline、diff-check 通过。新增真实 CLI parse→execute→fake Agent Settings/Plugin removal dispatch、registry revision drift fail-closed、Host retained MCP snapshot overlay release、Windows atomic ACL 前后校验、macOS/Linux/Windows API mock 与 CRLF 边界回归。未启动真实 MCP/Hook/LSP/Monitor、模型或网络，未读取 `.env*`/`.npmrc`/凭据；docs/tasks/project 检查与 HC-130 既有复核日期阻塞见 tmp/handoff.md。"
references: HC-157；commit 255be38；Qwen Code snapshot 6a432ad2ebce57b0b48cd3d6a8f4f7fab50c33fe；/Users/beichen/Desktop/大模型/za38-cli-extension（只读参考）
completed_at: -
---

## 背景

HC-157 已让 Harness 能识别、安装、信任和快照化 Qwen/DevAgent Extension，并接入 Context、Agent 与
`SubagentStop`。HC-158 Phase 1 已让 Commands、Skills 进入 canonical runtime，Phase 2 又让 ZA38 stdio MCP
进入同一 canonical lifecycle；本停点再把三类 Qwen Tool Hook、stdio LSP 和 Settings 接入现有
canonical seam。当前 ZA38 清单没有 Channels；本 Task 已决定不接入 Channel runtime。

## 用户结果

- 安装并启用 ZA38 后，输入 `/za38` 能看到 `/za38-init`、`/za38-sdd`、`/za38-index`，而不是“没有匹配的命令”。
- 选择命令后，Timeline 保留用户精确原始调用（包括命令大小写、内部空格和尾随空格），模型投影使用已安装快照中的 Markdown 正文和规范化调用参数。
- `za38-framework` 作为同一插件的有效 Skill 可被 Command 请求和模型按需读取；停用、失信或篡改后立即不可用。
- `za38.03_code_index` 只在 enabled+trusted catalog 中通过 canonical MCP Manager 启动，失败可观察且不阻塞其他 Server。
- Hook、LSP、Settings 按组件逐步接入；Channels 明确延期并保持 unsupported/effective=false；“识别到”永远不等于“可执行”。

## 实施边界

- 每一阶段独立 review、验收和停点；Commands+Skills 未通过前不得进入 MCP。
- 不把插件正文、宿主 store 路径或敏感设置值发送给 CLI；CLI 只消费 Host 校验后的摘要和稳定 ID。
- 不直接执行真实 ZA38 MCP、Hook、网络服务或读取 `.env*` 完成自动化测试。
- 不为追求表面兼容放宽 portable/Claude/Hybrid 的 schema、权限或冲突规则。
- Marketplace、Git/npm 安装、`link`、auto-update 和热重载属于 Plugin 管理面后续任务，不阻塞核心运行时。

## 可观察验收

- [x] Phase 0：Qwen 未声明为 `adapted/effective` 的 LSP、Monitor、Hook、MCP 永远不进入 runtime catalog。
- [x] Phase 1：三个 ZA38 Command 和 `za38-framework` Skill 从 static preview 移入 effective catalog，并完成 fake Host→CLI→Run 离线闭环。
- [x] Phase 2：ZA38 stdio MCP 经 fake server 完成启动、工具发现、调用、错误隔离、关闭和 generation 替换闭环；取消复用既有 Run/async cancellation seam。
- [x] Phase 3：Qwen `PreToolUse`、`PostToolUse`、`PostToolUseFailure` 与 LSP 复用既有 canonical runtime；无 Host seam 的 Hook 保持 unsupported。
- [x] Phase 4A：Settings 架构设计、威胁模型、v3.7 wire shape、最小环境注入和离线验收矩阵已设计并通过主任务评审。
- [x] Phase 4B：Settings 已实现用户/工作区作用域、敏感值保护和最小环境注入；旧 fingerprint、声明漂移或缺值按契约失败关闭；主任务代码验收已通过，待用户手工验证。
- [x] Phase 5 决策：当前 ZA38 未声明 Channels，本 Task 不实现 Channel runtime；组件继续报告 unsupported/effective=false。未来若有真实需求，必须另立任务完成 canonical Channel Host、安全评审和 threat model，禁止用动态 import 绕过。
- [x] 全阶段：Agent Plugins 1.0、Claude、Hybrid、HC-157 Context/Agent/SubagentStop 回归不变；用户已完成真实 ZA38 CLI/TUI 与 Managed 子代理调用验收。

## Phase 0 实施证据（已验收）

本停点只完成统一 component effective runtime gate，未进入 Phase 1 Commands+Skills。

- 代码：`plugins.model.runtime_component_eligibility` 统一检查 format、kind、enabled/trusted、安装身份、status、count、sources、capabilities、effective；Hook、LSP、Monitor、MCP、Command、Skill，以及 HC-157 Agent/Context/Team consumer 均通过同一语义或其对应 canonical gate。
- Qwen：`lspServers` 与 `monitors` 只生成明确的 `kind=lsp/monitors`、`status=unsupported`、`effective=false` 报告；Qwen LSP/Monitor/MCP 在 Phase 0 没有可运行格式入口，不能借 Claude loader 读取或构造 runtime。
- 失败关闭：disabled、untrusted、invalid、unsupported、effective=false、组件报告缺失/歧义和安装校验失败均不产生可执行 runtime；阻断原因使用稳定 `PLUGIN_RUNTIME_COMPONENT_BLOCKED` diagnostics。Qwen `SubagentStop` 仅对命中 matcher 后的 Hook 执行级失败按 warning + allow 处理；runtime 构造、身份、报告和取消仍失败关闭。
- 测试：离线 fixture/mock 覆盖恶意 Qwen LSP/Monitor、混合 effective Agent/Context/Hook、各 status/effective 组合、disabled/untrusted、安装 package digest 漂移、execution-relevant component report binding 漂移/缺失、diagnostics 文案变化、Claude Hook/LSP/Monitor 与 portable Command/Skill/MCP。三个主文件集合 `tests/test_plugins.py tests/test_plugin_runtime.py tests/test_qwen_plugins.py` 实际为 `129 passed`；Phase 0 显式集合为 `12 passed`；HC-157 回归为 `13 passed, 59 deselected`；portable/Claude/Hybrid 为 `31 passed, 27 deselected`；跨格式 Agent/Context/SubagentStop 为 `7 passed`；Host Plugin 为 `6 passed, 47 deselected`。Qwen fake spawn counter 为 `0`；未启动真实 ZA38 Hook/MCP/LSP/Monitor，未联网，未读取 `.env` 或凭据。
- 停点：Phase 0 已经由主任务验收；本工作树不提交、不推送，Phase 1 与 Phase 2 证据见下节。

## Phase 1 实施证据（Commands + Skills 停点，已通过主任务验收）

本节记录已验收的 Commands + Skills 停点；Phase 2 随后按独立边界完成，Phase 3 LSP/其他 Hook 仍未进入。

- Qwen Adapter：`qwen-command` 保持 `kind=command`，`qwen-skill` 转为 canonical `SkillRecord`；合法 Markdown、YAML frontmatter、正文、来源、大小和 UTF-8 逐条校验。坏条目隔离并保留稳定 diagnostic，`!{shell}` 与 `@{file}` 明确拒绝；symlink、路径越界、篡改和重装竞态均 fail closed。
- Catalog/preview：只有 Adapter 和 canonical consumer 都能读取的条目才令 `commands`/`skills` component `adapted/effective=true`；enabled+trusted、package digest 与 report binding 匹配后才进入下一 Host snapshot。有效条目从 Qwen static preview 去重，disabled/untrusted/invalid/unsupported 条目继续以 non-runnable preview 可见。
- Registry/CLI：ZA38 的 `/za38-init`、`/za38-sdd`、`/za38-index` 以自然名进入单一 CommandRegistry；嵌套目录用 `:`，冲突稳定回退为 `extension.command`，不依赖加载顺序。CLI 只接收 Host 摘要、stable ID 和 invocation contract，不接收正文或 store 路径；CLI 将同一 Registry 生成的 `command_id → resolved_name` 与 snapshot ID 通过 `commands.bind` 一次性绑定到 Host，Host 不推断未选中的自然名。
- Protocol 兼容：canonical Protocol v3 从 minor 5 升到 minor 6，`commands.bind` 的最小 minor 为 6。CLI 只在 initialize 协商到至少 6 且存在 Plugin Command 时调用 binding；旧 minor 有 Plugin Command 时返回稳定 `COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED`，没有 Plugin Command 时保持普通 run 兼容。Host 对协商到 v5 或更低的连接在 handler 前返回 `PROTOCOL_MINOR_REQUIRED`，不接受 v6-only binding。
- Run/Skill：Host 从本 Run 不可变 snapshot 读取正文，`{{args}}` 只做一次纯文本替换，无占位符时追加参数；Transcript 保留精确原始调用 `/ZA38-SDD   创建登录功能  `，模型只接收一次展开正文并记录 plugin/package/command/snapshot provenance。`za38-framework` 只通过同一 snapshot 访问顶层 `references/*.md`，开发素材 `references/origin/` 不进入虚拟资源、静态列表或诊断；净化后的安装 store 可保留该源文件，但不扩大运行时权限。
- 离线停点：fake Agent/Host/CLI 验证菜单、补全、raw invocation、single projection、provenance 和虚拟 references；没有启动真实 ZA38 Hook/MCP/LSP/Monitor、模型或网络，也没有读取 `.env`/凭据。
- 返修：本地目录/ZIP 安装在 staging 读取内容前按名称排除 `.git`、常见 VCS/OS 元数据、`.env`/`.env.*`、`.npmrc` 等；净化后的 package 才参与 file count、digest、resource snapshot，排除范围外的 symlink/hardlink/socket/FIFO/special file 仍稳定拒绝。真实 `/Users/beichen/Desktop/大模型/za38-cli-extension` 只读临时 home 安装并 enable 后得到 Commands 3、Skill 1，Commands/Skills static preview 均去重；未读取或保存 `.env*`、`.npmrc` 内容。沙箱禁止创建真实 Unix socket，离线 fixture 用 `stat.S_IFSOCK` fake lstat 验证 `.git` 先剪枝，真实 checkout 验收覆盖实际 `.git/fsmonitor--daemon.ipc`。
 返修：Protocol `requested_skill` 增加可选 `raw_invocation`、`command_name`，并新增 `commands.bind`；CLI 保留 `/ZA38-SDD   创建登录功能  ` 原文、单独规范化 args，并把同一 Registry 的 exact resolved name 绑定到当前 snapshot。Host 在写 Transcript/调用模型前只接受这份不可变 `command_id → resolved_name` binding，校验 raw、stable ID、command name 和 args，不复制 builtin/alias/冲突表，也不重新解析 UI 命令；未选中的 `/help` 伪装、另一插件命令和 args/raw mismatch 稳定返回 `COMMAND_INVOCATION_*`，fake Agent 不增加调用且不写伪造 Transcript。Phase 1 主集合 `150 passed`（52+8+90）、focused `18 passed`；真实 ZA38 diagnostics 为空，store 保留净化源中的 `references/origin/`，但 snapshot/Protocol 未捕获；HC-157 Qwen/SubagentStop/Context、portable/Claude/Hybrid、full-demo、Host/CLI/Protocol 证据与具体命令见 `tmp/handoff.md`。Task/Spec/Plan/Todo 已同步；Phase 2 证据见下节。

## Phase 2 实施证据（ZA38 stdio MCP 停点，待主任务验收）

本停点只完成 Qwen `mcpServers` 的 ZA38 stdio 子集；不进入 Phase 3 Hook/LSP，也不实现 Settings、HTTP/SSE 或真实外部服务。

- Adapter：`qwen.py` 使用显式 `_load_qwen`，与 `mcp_adapter.py` 共用逐 server 字段校验；只有 `type=stdio`、安全 command/args/cwd/env/timeout 和四类路径 token 均能构造 `McpServerConfig` 的条目才 `adapted/effective`。未知 token、`!{shell}`/`@{file}`、越界、宿主绝对路径、缺失目标、symlink、错误类型和超限均 fail closed；坏 server 隔离，不影响同包有效 server。
- Canonical lifecycle：有效 Qwen server 进入现有 `McpConnectionManager`，复用 namespaced tool、连接超时、Run cancellation、Host close 和 generation replacement；`mcp.status` 新增可选 `diagnostics` 结果字段，稳定呈现逐项转换失败。有效 MCP 从 static preview 去重，disabled/untrusted/invalid 条目保持 non-runnable preview 且不构造 client。
- Protocol：只在 canonical `packages/protocol/schema/v3.json` 为 `mcpStatusResult` 增加可选 `diagnostics: string[]`，随后运行 `protocol:generate` 与 `protocol:check`，未手改生成文件。
- 离线证据：`tests/test_qwen_mcp_phase2.py` 当前 `14 passed`，覆盖有效 server、四类 token、字段拒绝、坏项隔离、真实 fake stdio initialize/tools/list/call/cancel/close、mcp.status diagnostics、disabled 不构造 client 和 generation drain；canonical `tests/extensions/test_mcp.py` `72 passed` 覆盖 partial failure、取消/关闭相关生命周期与 snapshot replacement，Host `tests/host/test_server.py` 既有完整集合 `56 passed`。未启动真实 ZA38 MCP、模型或网络。
- 真实 ZA38 只读临时 home：安装+enable 得到 Commands `3`、Skill `1`、MCP config `1`；MCP conversion diagnostics `[]`，Commands/Skills/MCP static preview 均为 `0`；content-addressed store 保留 `122` 个 `references/origin` 条目，runtime snapshot 捕获 `0` 个 origin 条目。仅做安装、静态快照和配置转换，没有调用 `MultiServerMCPClient`，未读取或打印 `.env*`、`.npmrc`、origin 内容或凭据。

## Phase 3 实施证据（Hook + LSP 停点，待主任务验收）

本停点只接入 Qwen 已有 Host seam，不进入 Phase 4 Settings；Phase 2 的 MCP 增量继续保持 unstaged。

- Hook：`PreToolUse`、`PostToolUse`、`PostToolUseFailure` 和既有 `SubagentStop` 按事件逐条转换到 `HookRunner`。Pre 只能阻断，不能修改工具参数或绕过 policy/approval；Post 只保存有界、低可信 `additionalContext`，不改写真实工具结果；PostToolUseFailure 的 Hook 异常不覆盖原始工具异常。Prompt、Session、Compact、Permission、Todo、Message 等无 canonical seam 的事件保留 unsupported/non-runnable；同一清单中有效事件与坏/unsupported 事件逐项隔离。
- LSP：新增 `qwen_lsp.py` 作为 Adapter/runtime 共用校验 seam；inline 或包内 JSON 仅接受 stdio。command、args、cwd、env、timeout、workspaceFolder、extensionToLanguage 逐字段有界校验，四类路径 token 只解析到已安装 store/当前 workspace；未知 token、socket/HTTP、宿主绝对路径、越界、缺失/symlink、扩展冲突和错误类型产生稳定诊断并不 spawn。runtime 只构造既有 `PluginLspManager`，查询只走 Harness `lsp` 工具。
- 生命周期：真实 fake stdio LSP 覆盖 initialize/definition、server EOF、畸形 header/body/JSON、timeout、cancel、close 和 generation replacement；disabled、untrusted、invalid、unsupported 组件均不构造 client。Qwen Hook/LSP 冻结 executable 后不继承 `PATH`、`NODE_OPTIONS` 或动态加载器变量；Qwen Hook command 另冻结为 executable+argv 并使用 exec，带空格/反斜杠路径不依赖 POSIX quoting；Claude/portable 保持既有 `inherit-path` 语义；不读取 Settings。
- 离线证据：`tests/test_qwen_phase3.py` 当前 `12 passed`；与 `tests/test_qwen_plugins.py tests/test_plugin_runtime.py` 合计 `98 passed`。覆盖 fake Tool 的 allow/block/additionalContext/original error、Run A/B feedback 隔离、root+两个 child 的 Managed/Host release、取消时模型调用为 0、Qwen executable+argv freeze、Claude PATH 保持、真实 fake LSP 的 EOF/header/body/JSON 归一化、canonical lsp tool、transport/path/env/placeholder/extension conflict、timeout/cancel/close/replacement。未启动真实 ZA38 Hook/LSP/MCP、模型或网络，也未读取 `.env*`、`.npmrc`、Settings 或凭据；最终跨包命令与 staged SHA-256 见 `tmp/handoff.md`。
- 联合返修证据：Hook feedback 以真实 `RunContext(thread_id, run_id, execution_id)` 作为 key，Run A/B 交错时只向所属模型请求投影；Managed child release 只清精确 execution，Host root release/close 清理同一 Run 的全部 execution，异常和取消不留残留，缺少有效上下文稳定返回 `PLUGIN_HOOK_RUN_CONTEXT_REQUIRED`。已取消 model call 返回 `PLUGIN_HOOK_RUN_CANCELLED` 且不调用模型。Qwen Hook/LSP 在 catalog 阶段冻结 executable，Hook command 使用 `create_subprocess_exec` 传入 argv；运行环境不继承 `PATH`、`NODE_OPTIONS` 或动态加载器变量；Claude/portable 的既有 `inherit-path` 语义保持，另有 fake node 位于非 `os.defpath` 目录和 Windows 风格 argv 的回归。
- LSP 坏 stdio 证据：真实 fake subprocess 在 initialize 阶段覆盖 server EOF、非 ASCII header，在 query 阶段覆盖截断 body 与非法 JSON；均返回稳定 `PLUGIN_LSP_*`，并清理 client、进程和 stderr task。当前 `tests/test_qwen_phase3.py` 为 `12 passed`。
- MCP 真实闭环证据：`test_qwen_mcp_real_fake_stdio_round_trip_cancel_and_close` 通过 Qwen Adapter → canonical `McpConnectionManager` 启动仓库内 fake newline-delimited stdio server，观察 initialize、tools/list、tools/call、取消进程和 `close_all` 清空 generation；此前 patch `MultiServerMCPClient` 的测试仍只覆盖 manager seam/generation，不再作为唯一 stdio 证据。当前 `tests/test_qwen_mcp_phase2.py` 为 `14 passed`，联合 focused 为 `26 passed`。

## Phase 4A 设计实施证据（已通过主任务评审）

Phase 4A 只完成 Settings 架构设计、威胁模型和 Phase 4B 可验收设计，没有在该阶段
修改产品代码、Protocol schema/generated、CLI 或测试实现；该设计已通过主任务评审。
Phase 4B 的实现证据见下文，唯一架构依据是
docs/developer/architecture/扩展与插件机制设计方案.md 的 HC-158 Settings 章节。

- 存储：user/workspace metadata 固定在用户私有 ~/.harness/settings/v1，所有值
  固定进入当前用户 credential manager；无可验证 backend 时
  SETTINGS_STORAGE_UNAVAILABLE，禁止明文 fallback、PluginStore、Git
  workspace、registry、catalog、fingerprint、Transcript、日志和 fixture。
- 持久化分层：v1 `index.json` 顶层固定为 schema_version、scope、scope_binding_digest、
  index-level revision、records、tombstones、journal_refs 和 workspace_registry；单个
  durable record 不保存 store_revision、store_state、runtime_state、pending_operation
  或 diagnostic。store_state 由 live index/tombstone/journal 与 exact credential backend
  校验派生，runtime_state 只由当前 Host/generation snapshot 派生；settings.list 现场
  组合三者，绝不把 runtime_state 写回 metadata。user index 以 digest-only、受限
  `workspaces/<digest>/index.json` registry 登记全部 workspace scope，不枚举 backend。
- 事务文件顺序固定：scope lock 内先写完整 journal temp→fsync→atomic rename，再原子提交
  `index.journal_refs` 并锁内重读；ref 未 durable 前不得写 credential 或改 record/tombstone。
  ref 前崩溃只清理安全 orphan journal；ref 缺失/损坏/权限不合法使 scope
  SETTINGS_STORAGE_UNAVAILABLE。完成后先移除 ref 再删 journal。index 与 journal phase
  不一致时只按 active generation/tombstone 和 closed union 决定回滚或前滚。
- 空 store 的 list 不建文件并返回逻辑 revision=0；set/remove 在锁后重读，只有
  expected=0 且 index 仍不存在时 set 才 bootstrap。workspace 首次 set 按 user lock →
  workspace lock 把 registry registering、workspace index 和 record 作为可恢复事务推进
  到 registered；bootstrap 的 index 首次 ref 提交 revision=1，record 成功后再递增。
  并发首次 set 只有一个成功，另一方返回 revision conflict。absent/no-record remove
  返回 SETTINGS_RECORD_NOT_FOUND 且不创建 index。
- 绑定：plugin_id、package_digest、declaration_digest、setting_key、env_var
  与 user/workspace scope binding digest 逐项匹配；Qwen 必须有 name、description、
  合法 envVar，sensitive? 可选，required 固定 false，setting_key 等于 exact
  canonical envVar。name/description 只展示，不进入 record identity 或
  declaration_digest；缺少/空/非法 envVar 是 declaration invalid，未来 required/
  value_type/consumer 只属于 Harness-native schema。声明、package、root/symlink/
  额外 root 漂移均 stale/fail closed。declaration_digest 忽略 display 字段，但当前
  package_digest 包含 manifest bytes；只要完整 package_digest 变化，旧 credential 仍
  按外层 binding stale、必须重新 set，HC-158 不跨 package digest 迁移。
- 运行：workspace 覆盖 user；Qwen 缺少 optional value 只是不创建对应 env entry。
  同一插件每个有效 MCP/Hook/LSP 子项收到该插件已配置且通过 denylist 的全部
  declared settings，作为 child-only env overlay；Commands/Skills/Agents 不 spawn、
  不接收 Settings env。权限/schema/backend 失败不 spawn，拒绝 PATH、NODE_OPTIONS、
  PYTHONPATH、LD_/DYLD_ 等 process-control 变量；Host/generation snapshot 固定
  next-host 生效，不热更新当前 Run。list 同时报告 live store_state 与旧 Host 的
  runtime_state，可表达 configured + pending_restart。Host/generation 的 immutable
  snapshot 只在 generation replacement/Host close 释放，Run/child 只清理自身临时
  env/value 引用。
- 恢复：set 先 durable prepared intent，再写 credential，最后 metadata commit；
  prepared/credential_written 在 metadata 未 commit 前崩溃时都按 journal 精确 new account
  回滚删除，失败进入 cleanup_pending/blocked，旧 active 保持；metadata committed 后
  只前滚清理 old account。remove 使用 prepared → tombstone_committed →
  credential_cleanup_pending → done；
  user registry uninstall 固定 user 及其已记录的全部 workspace scope，先 user lock
  冻结 identity/scope 清单，再按 workspace digest 排序加 scope lock；部分失败保留
  可重试 journal 并返回 SETTINGS_UNINSTALL_PARTIAL，不枚举 backend 或按 display
  name 删除，未记录 workspace 不触碰。
- tombstone 在 cleanup 和 journal 完成后仍持久保留；v1 不按时间自动删除，只有满足
  无 journal_ref、精确 account 已清理、revision 下限和无旧 Host/generation/Run lease
  证明的显式有界 GC 才可处理。最小保留 30 天、触发上限 180 天，无法证明时继续保留
  并报告 cleanup pending。
- 并发：set/remove 的 `expected_store_revision` 是必填非负 index revision，首次 set
  也必须先 list；Host 锁后重读并 CAS，冲突返回 SETTINGS_STORE_REVISION_CONFLICT，
  HC-158 不提供无条件写入。journal 的 PendingOperation 是按 operation 判别的
  Set/Remove/Uninstall/Migration closed union；wire 只返回不含 operation_id、account、
  record 或路径的脱敏 pending summary，非法 phase/operation 组合 fail closed。
- 平台：Phase 4B 只在 macOS Security.framework、Linux secretstorage/Secret Service、
  Windows CredWriteW/CredReadW/CredDeleteW 的 capability probe 可证明时启用；权限、
  锁、atomic replace、fsync 或 backend 不可验证时稳定 disabled，不降级明文。
- 契约：Phase 4B 已把 canonical Protocol v3.6 升到 v3.7，增加 settings.list/set/remove；
  三个方法都要求 minor >= 7，list 需要 settings.read，set/remove 需要
  settings.manage；old minor 不发送/接受未知 RPC，响应只含完整摘要和稳定错误。
  所有 setting（sensitive 和 non-sensitive）交互输入都只允许 TTY no-echo，非交互
  统一使用 --secret-stdin；任何 setting 都不接受 argv value。值 validator 统一拒绝
  先检查类型/输入形状和 byte 上限：超出 65536 bytes 为 SETTINGS_VALUE_TOO_LARGE，
  上限内的非法/不完整 UTF-8、NUL 或非法 stdin 形状为 SETTINGS_VALUE_INVALID；CLI
  有界读取与 Host 重校验一致。
- CLI grammar 固定为 `harness plugins settings <list|set|remove>`：`set/remove` 的
  `<plugin-id> <setting-key>` 是两个位置参数，`--package-digest`、
  `--declaration-digest`、`--expected-store-revision` 是必填具名 option；只有 `set`
  允许 `--secret-stdin`。`settings.list` 的 wire summary 字段是 `env_var`；当前 Qwen
  映射中把该返回值作为后续 set/remove 的 `<setting-key>`，不存在名为 `setting_key` 的
  summary 字段。每个 action 都有独立 option 白名单，未知、缺值、重复或冲突身份输入在
  解析期失败；`--workspace` 与 `--cwd` 是互斥的同一 workspace 选择别名。
- 验收：架构文档已列出 Agent+CLI 双端目标文件、red→green 离线 fake credential/
  process 矩阵、权限/迁移/删除/卸载重装/声明漂移/precedence/redaction/
  cancellation/close 证据、protocol:generate/check、typecheck、diff-check 和
  回滚。Channels 已从本 Task 实现范围移出并继续禁止运行。

## Phase 4B 实施证据（主任务代码验收已通过，待用户手工验证）

本停点已按通过评审的 Phase 4A 设计实现 Settings 管理、解析和最小环境注入。Channels
已经按用户决定延期，不属于本次实现。实现文件、red→green 过程、实际测试命令与计数、
平台未验证边界统一记录在 `tmp/handoff.md`；本 Task 在用户手工验证前保持“待验收”，不归档。

- `config/settings.py` 覆盖 Qwen exact `envVar` declaration、值校验、credential backend
  seam、metadata/journal/ref、空 store revision=0、CAS、set/remove recovery、workspace
  registry 和可重试 uninstall；无可证明 backend 时 fail closed。
- `AgentHost` 以 Host/generation 创建 immutable SettingsSnapshot，按 workspace > user
  解析，只向有效 Qwen MCP/Hook/LSP child 注入声明且通过 denylist 的 env；Commands、
  Skills、Agents 不接收 Settings env，set/remove 只对 next-host 生效。
- 每次成功的 `plugins.remove` 都按当前已安装 Plugin record 的 plugin/package identity
  解析并清理 Settings；即使记录已停用或 trust 失效，也会在同一 registry lock/revision
  事务内清理已有 user/workspace credential，不能依赖 enabled+trusted runtime catalog。
  `--purge-data` 只额外删除 Plugin data。
- canonical Protocol v3.7 已生成 `settings.list/set/remove` 与 capability/minor gate；
  CLI 统一使用 `--secret-stdin`，TTY no-echo，所有值不从 argv 接收、不回显，管理写入
  使用必填 `expected_store_revision`。
- 测试仅使用运行时生成的 fake credential/process/Host/CLI，不使用真实凭据、真实 ZA38
  进程、网络或模型；具体可观察结果和未验证项见 handoff。

### Phase 4B 用户实测返修（2026-08-28，待主任务验收）

- CLI Controller、菜单、手输和选择后 Enter 现在共享启动握手生成的同一个不可变
  `CommandRegistry`；Plugin Command 的原始 invocation、规范化 args、stable command ID 和
  `requestedSkill` contract 仍由同一 Dispatcher 结果提交。此前“菜单可见但 Enter 报未知命令”
  的真实路径已由 `input.submit` 回归覆盖。
- Qwen `qwen-code` command Hook 的原生 `timeout` 单位是毫秒：缺省 `60000`、允许范围
  `1..600000`，在 canonical `HookDefinition` 边界转换为 `60..600` 秒；`0`、负数、布尔值、
  字符串和超上限值稳定 invalid。Claude/portable 仍按既有秒单位和 `1..600` 秒边界处理。
- 本轮真实 ZA38 只读临时 home 安装/inspect 得到 Commands `3`、Skill `1`、Hooks
  `adapted/effective=true`、`compatibility=recognized`，静态诊断为空；没有执行真实 Hook、模型、
  MCP、网络，也没有读取或打印 `.env*`、`.npmrc` 或凭据。
- 本轮 focused 结果：Qwen Hook/真实安装选择集 `13 passed`，完整 Qwen Plugin 文件 `102 passed`，
  Plugin 主集合（`test_plugins.py` + `test_qwen_plugins.py`）`155 passed`；完整 Phase 0-4B
  Python 集合 `322 passed`，CLI/Protocol 六文件集合 `80 pass`。

### Phase 4B 用户真实运行跨端 Protocol 返修（2026-08-28，待主任务验收）

真实 ZA38 `/za38-sdd` Run 曾触发 `run.started`/`skill.loaded` 的 schema additional-properties
错误，随后才出现 sequence gap。根因是 BuildRunAdapter 已按 Phase 1 发送
`command_provenance`/`provenance`，但 v3.7 canonical schema 漏声明，而不是应该删除来源信息或放宽
validator。本轮只在 `packages/protocol/schema/v3.json` 增加严格闭集的四字段 provenance 定义，并通过
`protocol:generate` 同步 TS/Python 类型、schema 副本、digest 和 validator；没有升 minor，因为 v3.7
仍是当前未发布 HC-158 minor，字段是既有实现意图的 schema 修复，不是新的 RPC 或 capability。

- `run.started.payload.command_provenance` 与 `skill.loaded.payload.provenance` 只允许
  `plugin_id`、`package_digest`、`command_id`、`snapshot_id`；Plugin Command 的 command ID 非空，普通
  Skill 的 `skill.loaded` provenance 可用 `command_id: null`。测试同时拒绝额外字段，并确认不包含正文、
  store 路径或宿主绝对路径；内置/项目/用户 Skill 不附加空 digest provenance，保持既有 payload。
- `StartRun` 冻结 initialize 协商的 minor。v3.6 或更低连接请求 Plugin Command 时，Host 在准备阶段返回
  `PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED`，不写 Transcript、不调用 fake Agent、不发事件；普通旧 minor
  Run 和 v3.6 `commands.bind` 保持兼容。v3.7 连接走完整 provenance 事件，CLI fake transport 收到的
  run.started、skill.loaded、content.delta、run.completed 序列为连续 `1,2,3,4`。
- 红测先在真实 `AgentHost`/`BuildRunAdapter` 事件上调用 Python `EventEnvelope.model_validate`，并在 CLI
  `AgentClient` fake transport 上复现协议错误与序列缺口；绿测锁定生产 shape、TS validator、旧 minor 门禁和
  raw invocation/args 不变。验证命令与实际计数写入 `tmp/handoff.md`；未启动真实模型、Hook、MCP、网络或读取凭据。
- 本轮实际通过：`test_qwen_plugins.py` `103 passed`；Plugin/Qwen 主集合
  `test_plugins.py + test_plugin_runtime.py + test_qwen_plugins.py` `165 passed`；扩展 Skill/Host
  `test_skills.py + test_server.py` `79 passed`；MCP/Phase3/Settings/Protocol 合集 `100 passed`；
  CLI/Protocol 六文件集合 `82 pass`（`762 expect()`）。

### 用户真实 Subagent 返修（2026-08-31，待主任务验收）

用户用真实 ZA38 Agent 调用 `za38-frontend-executor` 时，Agent 已正确注册、`task` 审批也已解决，
但 child Engine 在构建阶段因空 MCP 过滤视图尝试 acquire 不存在的 MCP snapshot，最终界面只显示
笼统的 `RuntimeError`。本轮修复将空 `spec.tools` 定义为无 MCP 资源需求；有 MCP 权限的 child
只借用当前 Host 完整 MCP resource，并按 immutable `spec.tools` 精确投影，选定 server/tool
缺失时保持 fail closed，不向 child 暴露其他 server 的工具。未知异常仍只显示类型，受控
`MCP_`/`RUNTIME_`/`PLUGIN_` 等稳定码可安全透传，禁止把路径或秘密放进 Tool 输出。

离线回归通过真实 `AgentEnginePool` + Host builder seam 验证空 MCP child 可构建、非空视图只注入
授权工具、缺失工具 fail closed，以及稳定错误码/未知异常脱敏；未调用真实模型、网络 MCP、Hook
或凭据。完整 Host 集合中的既有 `agents.list` schema 漏项（builtin agent 缺少 `color` 等字段）
与本轮变更无关，单独记录在 handoff。

### 用户真实 Hook observation 返修（2026-08-31，待主任务验收）

真实 ZA38 Agent 已越过 MCP/Engine 构建后，成功执行 Tool 时仍因
`PluginRuntimeMiddleware._run_observation_hook()` 未接受 `diagnostic_log` 关键字而失败。
本轮将 RunContext 的诊断日志沿 `PluginRuntimeMiddleware → HookRunner → HookRunner._invoke`
继续传递；Hook 日志只记录稳定的 start/completed/failed 摘要，不复制 payload、stdout、stderr、
宿主路径或秘密。新增真实 middleware + 本地离线 Hook fixture 回归，覆盖 PostToolUse 成功路径和
诊断日志脱敏；不执行真实插件 Hook、MCP、模型、网络或凭据。

### Phase 4C：Qwen Adapter 重解析与 SubagentStop 终态兼容（2026-08-31，已验收）

本停点纠正 HC-158 原先把 Qwen `SubagentStop` Hook 执行失败一律失败关闭的语义，并补齐
“Adapter 升级后不能继续相信安装时报告”的运行前边界。当前 ZA38 插件源码不需要重新复制；只有
package digest 变化才继续沿用显式 update/install 安全边界。

- `PluginManager.refresh_catalog()` 在 Host 启动、控制面刷新和 Run preparation 前读取
  registry 中的已安装记录，复核 package digest，并用当前 Adapter 重建 descriptor/component
  report。`adapter_revision` 与 package/plugin identity 一起绑定；刷新结果在同一 registry lock
  内原子提交，报告和 registry revision 不变时不重复写入。
- 能力指纹不变时自动更新 report/metadata 并保留 `enabled` 与 trust；能力指纹改变时保留旧
  `trusted_capability_fingerprint`，返回 `reauthorization-required`，从 runtime catalog 排除，
  并建立按 plugin/component stable identity 索引的不可执行 blocked view。普通消息、内置能力和
  其他已授权 Plugin 不受影响；只有请求实际解析到 stale Plugin Command/Skill/Agent/Team，或已有
  明确 Plugin provenance 的其他 executable entry 时，才返回包含 plugin_id、authorization_state、
  当前 fingerprint 及 inspect/enable 操作的 `PLUGIN_REAUTHORIZATION_REQUIRED`。用户用新 fingerprint
  显式确认后才恢复 enabled/trusted；重复启动和并发 refresh 不重复扩大授权。
- 对格式为 `qwen-code` 且 matcher 命中的 `SubagentStop`，先聚合全部匹配结果；Hook exception、
  timeout、非零退出、非空畸形输出和 runner 关闭都写入脱敏稳定 warning/diagnostic。任意有效
  block 优先于失败或 allow，只有无有效 block 时才附 warning 并返回已完成 child 的最终结果；
  空 stdout、空 `{}` 和合法无 decision 输出不产生 failure warning。
  合法 blocking 继续同一 child checkpoint；达到 Qwen blocking cap 时释放最新结果并附 warning。
  matcher miss 不执行 Hook；Run/parent/user cancellation 继续传播 Harness canonical cancellation。
  PreToolUse、Policy、权限、MCP、非 Qwen 和 Hook runtime 构造失败仍保持 fail-closed。
- Qwen command Hook `timeout: 10000` 继续按毫秒解释为 10 秒。当前实现不接入 Channels。

本停点离线证据：`tests/runtime/test_subagent_stop.py` 与
`tests/test_plugin_refresh.py` focused 合计 `43 passed`；另有
`tests/test_agent_delegation.py` `13 passed`，合计 `56 passed`。覆盖 Host pre-dispatch
reauthorization、trust 保留、report revision、幂等/并发与 SubagentStop exception/timeout/
nonzero/malformed/type-error/empty/closed runner、blocking cap、matcher miss、cancellation、
warning 传播和多 Hook 聚合。相关集合本轮实际为 `242 passed, 12 failed`：11 项既有 Host
测试使用真实用户级 PluginStore 锁，1 项既有裸 `node` shebang fixture 受带空格 venv 路径影响；
未修改或清理真实用户 home。未使用真实 Hook、MCP、LSP、模型、网络或凭据。

可演示方式：在临时 home 安装并启用 ZA38 fixture，制造旧 `adapter_revision` 后运行
`harness plugins inspect <plugin-id>`，应看到当前 report 和 `authorization_state`；若 report
能力指纹改变，先看到 `reauthorization-required`，直接 Run 在 dispatch 前被拒绝；用 inspect
得到的新 fingerprint 显式确认后再 Run，child 即使 SubagentStop Hook 自身失败也返回最终结果和
warning。2026-08-31 用户已在真实 ZA38 工作区完成插件重新授权、Slash Command 与 Managed
子代理调用验收；期间发现的 CompiledSubAgent `messages` 结果适配缺口已补回归并修复，用户复测通过。
