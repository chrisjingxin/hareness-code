# HC-158 Qwen 插件运行时对齐 Todo

关联 [Task](../task/HC-158-对齐Qwen插件运行时.md)、[Spec](../spec/HC-158-对齐Qwen插件运行时.md) 与
[Plan](../plan/HC-158-对齐Qwen插件运行时.md)。

## Phase 0：effective gate

- [x] 先写恶意 Qwen `lspServers`/`monitors` fixture，证明当前通用 runtime 可能越过 unsupported 报告；测试不得启动真实进程。
- [x] 抽取统一 component runtime eligibility gate，并应用到 Hook、LSP、Monitor、MCP、Command、Skill。
- [x] 验证 unsupported/invalid/effective=false、disabled、untrusted、digest、component source binding fingerprint 漂移全部不进入 runtime；diagnostics 文案变化不扩大 trust。
- [x] 重跑 HC-157 SubagentStop 与 portable/Claude/Hybrid runtime 回归，完成本停点自检并等待主任务验收。

### Phase 0 停点查看方式

在 `packages/agent` 使用项目虚拟环境运行 Phase 0 focused tests；预期看到 Qwen 恶意 LSP/Monitor 的
`runtime_catalog.lsp_servers/monitors` 为空、fake spawn 为 0，同时有效 Hook 仍为 1 条，portable/Claude/Hybrid
runtime fixture 保持可用。返修后的实际证据为：Phase 0 显式集合 `12 passed`；
`tests/test_plugins.py tests/test_plugin_runtime.py tests/test_qwen_plugins.py` 共 `129 passed`；
HC-157 回归 `13 passed, 59 deselected`；portable/Claude/Hybrid `31 passed, 27 deselected`；跨格式 Agent/Context/SubagentStop
`7 passed`；Host Plugin
`6 passed, 47 deselected`。Phase 0 已验收，Phase 1 的执行证据见下节。

## Phase 1：Commands + Skills

- [x] 增加 Qwen Markdown Command/Skill dialect 的正负向测试和 Adapter/runtime 共用校验。
- [x] Commands/Skills 逐组件标记 adapted/effective，并从 static preview 去重。
- [x] 将自然命令名、嵌套 `:` 和 `extension.command` 冲突回退接入单一 CommandRegistry，并以 Protocol v3 minor 6 的 `commands.bind` 把 exact resolved name 绑定到同一 snapshot。
- [x] Host 从不可变 snapshot 展开 `{{args}}`；Transcript 保留原始 invocation，模型投影记录 provenance；raw token 必须匹配本连接 exact `command_id → resolved_name` binding。
- [x] 明确拒绝首阶段不支持的 `!{shell}`、`@{file}`，覆盖超大正文/参数、坏 UTF-8、篡改和重装竞态。
- [x] ZA38 三个 Commands 与 `za38-framework` 完成 Host→Protocol→CLI→Run 离线集成测试。
- [x] 完成 focused、跨格式回归、文档和 review 前自检；停在 Phase 1，不进入 MCP。
- [x] 返修本地目录/ZIP 安全 staging：`.git`/常见 VCS、OS metadata、`.env`/`.env.*`、`.npmrc` 按名称先剪枝；净化 package 才计算 digest/count/resource snapshot，排除范围外 special file 仍拒绝。
- [x] 收紧 Qwen Skill 资源闭包：只捕获顶层 `references/*.md`；`references/origin/` sentinel 可保留在净化 store，但不可读、不出现在静态列表/诊断/运行时快照；真实 ZA38 清洁安装 diagnostics 为空。
- [x] 返修 Command invocation contract：CLI 传递精确 raw invocation，并通过最小 minor 6 的 `commands.bind` 登记同一 Registry 的 exact resolved name；Protocol 携带 `raw_invocation`/`command_name`，Host 在受理前校验 stable ID、raw、command name 和 args 与不可变 binding 一致。插件 `help` 被 CLI 选为 `/bad.help` 时，未选中的 `/help`、另一插件命令和 args/raw mismatch 稳定失败。v5 协商下 CLI 不调用 binding、Host 返回 `PROTOCOL_MINOR_REQUIRED`；Host 不复制 builtin/alias/冲突表，fake Agent/Transcript 不产生副作用。

### Phase 1 停点查看方式与证据

在 `packages/agent` 使用 85a7 Python 3.13 venv 和 dc33 `PYTHONPATH`，fake Host/Agent 输入
`/ZA38-SDD   创建登录功能  `。可观察到三个自然命令和 `/za38` 前缀补全、Transcript 中的精确原始调用、模型单次收到展开正文、完整 provenance，以及同一 snapshot 的
`za38-framework` `references/root-guide.md` 虚拟只读读取。

- 主集合：`tests/test_plugins.py` → `52 passed`；`tests/test_plugin_runtime.py` → `8 passed`；`tests/test_qwen_plugins.py` → `90 passed`；合计 `150 passed`。
- Phase 1 Qwen focused（含真实 checkout 只读验收、exact binding 和 stable record identity 负向）→ `18 passed`；HC-157 Qwen/SubagentStop/Context、portable/Claude/Hybrid、full-demo、Host 及具体 selectors 的命令与结果见 `tmp/handoff.md`。
- Phase 0 显式集合 → `20 passed`；HC-157 回归 → `13 passed, 77 deselected`。
- portable/Claude/Hybrid runtime → `31 passed, 30 deselected`；Host Plugin/Skill/Run → `12 passed, 44 deselected`；CLI/Protocol focused（含 CLI 启动层版本门禁、Command Registry、Host/Protocol contract）→ `62 pass`；full-demo → `1 passed`。
- `protocol:generate`、`protocol:check`、`typecheck`、`git diff --check` 通过；未启动真实 ZA38 Hook/MCP/LSP/Monitor、模型或网络，未读取真实 `.env*`/`.npmrc`/凭据。
- 真实 `/Users/beichen/Desktop/大模型/za38-cli-extension` 使用临时 home 只读安装并 enable：Commands 3、Skill 1，Commands/Skills preview 均去重；store 不含 `.git`、`.env*` 或 `.npmrc`，但保留净化源中的 `references/origin/`，runtime snapshot 不含 origin。沙箱禁止创建真实 Unix socket，离线 fixture 使用 `stat.S_IFSOCK` fake lstat 验证剪枝。
- `git diff --check` 与四份已修改的 HC-158 文档、`tmp/handoff.md` 的独立 trailing-whitespace 扫描另行记录；docs/tasks/project 检查如被 HC-130 既有复核日期阻塞，不修改 HC-130。

## Phase 2：MCP

- [x] 为 Qwen `mcpServers` 增加显式 `_load_qwen` canonical 转换和 component gate。
- [x] 首先支持 ZA38 stdio shape，逐字段验证 command/args/cwd/env/timeout 与 `${extensionPath}`、`${workspacePath}`、`${/}`、`${pathSeparator}`。
- [x] 使用仓库内真实 fake newline-delimited stdio MCP 经 Qwen Adapter → canonical manager 覆盖 initialize/tools/list/call、取消和 close；patch fake 仅用于 manager seam、Host status 与 generation lease。
- [x] MCP effective 后从 static preview 去重；未 trust/disabled/invalid 不构造 client，坏项诊断进入 `mcp.status.diagnostics`。
- [x] 完成 focused、MCP/Plugin/Host/CLI/Protocol 回归、文档同步和真实插件只读安装验收；停在 Phase 2 等待主任务验收。

### Phase 2 停点查看方式与证据

使用仓库 Qwen fixture 和真实 fake stdio subprocess；patch `MultiServerMCPClient` 只用于 manager seam/generation 辅助，不启动真实 ZA38 MCP：

- `tests/test_qwen_mcp_phase2.py` → `14 passed`：有效 stdio、四类 token、字段 fail closed、逐 server diagnostics、static preview 去重、disabled 不 spawn、真实 fake subprocess 的 initialize/tools/list/call/cancel/close，以及 patch seam 的 generation drain。
- `tests/extensions/test_mcp.py` → `72 passed`：canonical server status、partial failure、tool namespace、close、snapshot replacement 和资源 lease；`tests/host/test_run_coordinator.py` → `39 passed`，验证既有 Host/Run cancellation seam 的取消后资源释放。
- `tests/host/test_server.py` → `56 passed`；三个 Phase 1 主文件仍为 `150 passed`；CLI/Protocol focused 为 `62 pass`；Python Protocol contract 为 `12 passed`。
- `bun run protocol:generate`、`bun run protocol:check`、`bun run typecheck` 和 `git diff --check` 通过；`mcpStatusResult.diagnostics` 只由 canonical schema 生成。
- 真实 `/Users/beichen/Desktop/大模型/za38-cli-extension` 只读临时 home：Commands 3、Skill 1、MCP config 1，conversion diagnostics 为空，Commands/Skills/MCP preview 均为 0；store 有 122 个 origin 条目，runtime snapshot 无 origin。只做安装、快照和配置转换，不调用 client、不读 `.env*`/`.npmrc`/凭据。

## Phase 3：Hook + LSP

- [x] 映射 Qwen `PreToolUse`、`PostToolUse`、`PostToolUseFailure`，逐事件定义输入输出和失败语义。
- [x] 保持无 canonical seam 的 Qwen Hook 事件 unsupported，混合事件不扩大 effective 范围。
- [x] Qwen `lspServers` 仅适配现有 stdio LSP，建立 Adapter/runtime 共用校验和精确 component gate。
- [x] 覆盖 transport、workspace、extension 冲突、超时、取消与关闭；review 后停点。
- [x] 联合返修：Hook feedback 按真实 `RunContext(thread_id, run_id, execution_id)` 隔离；Managed child release 只清自身 execution，Host root release 清同一 Run 的全部 execution，取消/异常/close 不留残留，缺上下文 fail closed。
- [x] 联合返修：Qwen command 冻结为 executable + argv 并使用 exec 启动，带空格和 Windows 反斜杠路径保持原 argv；Claude/portable 的 PATH 与 shell/exec 语义回归保持。已取消 model call fail closed 且不调用模型。
- [x] 联合返修：真实 fake LSP 覆盖 server EOF、畸形 header、截断 body、非法 JSON；均归一化 `PLUGIN_LSP_*` 并清理 client/process/stderr task。

### Phase 3 停点查看方式与证据

在 `packages/agent` 使用项目虚拟环境运行 `tests/test_qwen_phase3.py`。fake Tool 可观察 Pre deny 不调用底层 Tool、Post
additionalContext 只进入一次低可信模型投影、原始 Tool 异常保持不变；真实 fake stdio LSP 只通过既有 `lsp` 工具查询，超时/取消/Host close/
generation replacement、EOF 和畸形消息后 client 清理。Prompt、Session、Compact、Permission、Todo、Message 等事件继续 non-runnable。

- `tests/test_qwen_phase3.py` → `12 passed`；`tests/test_qwen_mcp_phase2.py` → `14 passed`；联合 Phase 2+3 → `26 passed`。
- `tests/test_qwen_plugins.py tests/test_plugin_runtime.py` → `98 passed`；其中 HC-157 SubagentStop、Claude/Hybrid canonical Hook/LSP/Monitor 与 Phase 0 gate 保持通过。
- Phase 3 未修改 Protocol，不运行生成器；最终 Phase 2+3 focused、Host/Run cancellation、CLI/Protocol、typecheck、diff 与 staged 基线证据写入 `tmp/handoff.md`。

## Phase 4：Settings

### Phase 4A：架构设计/威胁模型停点（已通过主任务评审）

- [x] 读取现有 ConfigChangeService、ModelSettings、PluginStore、AgentHost、
  ThreadPersistence 和 Protocol v3.6 版本协商实现。
- [x] 在既有插件架构文档合并唯一主方案：user/workspace metadata、schema v1、
  credential manager、权限、锁/CAS、atomic write、journal/recovery、迁移、删除、
  卸载和重装语义；不建立新的决策文件。
- [x] 固定 durable `index.json` 顶层 schema_version/scope/scope_binding_digest/
  revision/records/tombstones/journal_refs/workspace_registry；单个 record 不含
  store_revision/store_state/runtime_state/pending_operation/diagnostic；store_state
  和 runtime_state 只在 list 现场分别从 live backend 与 Host/generation snapshot 派生。
  user registry 以 digest-only locator 登记 workspace scope，并定义登记、移除、权限、
  crash recovery 和 user lock → scope digest lock 顺序。
- [x] 固定 journal file 先 temp→fsync→atomic rename、再提交 index.journal_refs 并锁内
  重读；ref 前 orphan 只从受限 journal 目录清理，ref 缺失/损坏使 scope fail closed，
  完成时先移除 ref 再删文件；固定 index active generation/tombstone 与 closed union
  的跨文件恢复裁决。
- [x] 固定 bootstrap CAS：不存在 index 的 list 返回 revision=0 且不建文件；只有
  expected=0 的 set 在 scope lock 下创建 v1 index，workspace 首次 set 推进 registry
  registering→registered；并发竞争返回 revision conflict；absent/no-record remove
  返回 SETTINGS_RECORD_NOT_FOUND 且不建 index。
- [x] 对齐真实 Qwen ExtensionSetting（name、description、envVar、sensitive?）；
  name/description/envVar 是必需 string，sensitive 可选，Qwen required 固定 false；
  setting_key 等于 exact canonical envVar。缺少/空/非法 envVar 是 declaration invalid，
  name/description 不进入 record identity 或 declaration_digest。
- [x] 记录 display/package 边界：declaration_digest 忽略 display 字段，但完整
  package_digest 含 manifest bytes；package_digest 变化即使只改 name/description 也
  使旧 credential binding stale，必须重新 set，不跨 digest 迁移；tombstone cleanup
  后持续保留，仅显式有界 GC 在所有安全门槛满足时执行。
- [x] 固定 immutable plugin/package/declaration/user/workspace binding、workspace
  覆盖 user、Qwen 可选 value 与 extension-wide MCP/Hook/LSP child-only env overlay；
  Commands/Skills/Agents 不接收 env，非 Qwen required fail-closed、process-control
  envVar denylist、startup snapshot、next-host 生命周期与 Host/generation/close 所有权；
  snapshot 只在 generation replacement/Host close 释放，Run/child 只释放自身临时引用。
- [x] 固定 set prepared-intent-first、remove tombstone-first、uninstall 跨 scope
  partial-retry 状态机；set 的 prepared/credential_written 未 commit 恢复固定精确删除
  new account，失败为 cleanup_pending/blocked，metadata_committed 后只前滚 old cleanup；
  user registry 清理 user 及 index 已记录的全部 workspace scope，先 user lock 冻结清单
  再按 binding digest 加 scope lock；恢复只按 journal 精确 account，禁止 backend 枚举或
  display name 删除；list 同时返回派生的 store_state/runtime_state。
- [x] 固定 macOS/Linux/Windows 具体 credential API 与 capability probe；不可证明
  权限、backend 或内存清理安全性质时稳定 fail closed，并记录 best-effort 引用释放
  与 Python/TypeScript 残余内存风险。
- [x] 写明 v3.7 settings.list/set/remove 完整 wire shape、字段类型/可选性/闭集 enum、
  required non-negative expected_store_revision/CAS、按 operation 判别的
  SetPending/RemovePending/UninstallPending/MigrationPending closed union、minor >= 7
  与 settings.read/settings.manage capability gate、stable errors、TTY no-echo 与
  --secret-stdin 规则（sensitive/non-sensitive 相同）、离线 fake 验收矩阵、
  Phase 4B 目标文件和回滚方案；值 validator 先判输入形状/byte 上限，超出 65536 bytes
  归为 SETTINGS_VALUE_TOO_LARGE，上限内的类型/编码/NUL/非法 stdin 形状归为
  SETTINGS_VALUE_INVALID。
- [x] 固定 CLI Settings grammar：`set/remove` 使用 `<plugin-id> <setting-key>` 位置参数，
  package/declaration digest 与 expected revision 使用必填具名 option；list/set/remove
  各自严格白名单，未知、缺值、重复/冲突身份和 list/remove 的 `--secret-stdin` 均在
  解析期拒绝。`settings.list` 返回 `env_var`，客户端将该 exact Qwen `envVar` 作为
  后续 `<setting-key>`，不读取不存在的 `setting_key` summary 字段；`--workspace` 与
  `--cwd` 同时出现时按互斥别名稳定拒绝。
- [x] 同步架构索引、HC-158 Task/Spec/Plan 和 tmp/handoff；本阶段不改代码、
  Protocol schema/generated、CLI 或测试实现。

### Phase 4B：实现清单（主任务代码验收已通过，待用户手工验证）

- [x] 实现 settings 摘要、set/remove 管理链路、credential backend seam、v1 metadata/
  journal/recovery、空 store revision=0、required CAS、workspace registry 与可重试
  uninstall；敏感值不回显、不入 registry/log/fingerprint/Transcript。`plugins.remove
  --purge-data` 在 registry lock 内复核 revision/package identity，partial/conflict 不先
  宣称移除；每次成功 remove 都按已安装 record 解析并清理 Settings，停用或 trust 失效
  的 Plugin 仍清理其既有 credential，不能依赖 enabled+trusted runtime catalog；
  `--purge-data` 只额外删除 Plugin data。
- [x] 为每个有效 Qwen MCP/Hook/LSP child 生成同一插件 extension-wide 的最小 env overlay，
  禁止进程控制变量和 shell 环境静默回退；Commands/Skills/Agents 不接收 env；set/remove
  只对 next-host 生效。
- [x] 以 fake credential/process/Host/CLI 先 red 后 green，覆盖 user/workspace precedence、
  声明变化、缺值、删除、恢复、卸载、日志脱敏、取消/关闭、跨格式回归及 Protocol v3.7
  settings.list/set/remove；补充 CLI parse→execute→fake Agent dispatch、registry drift、
  retained MCP snapshot overlay release、Windows atomic ACL 和 macOS/Linux/Windows API
  error/redaction mock。未知旧 metadata/旧明文迁移保持 fail closed。
- [x] 用户实测返修：Controller 菜单、手输、选择后 Enter 和 `command.execute` 复用握手生成的
  同一不可变 `CommandRegistry`，保持 Plugin Command 的 raw invocation、规范化 args、stable ID
  和 `requestedSkill` contract；Qwen command Hook 按 `60000..600000ms`（含边界）转换为
  canonical 秒，Claude/portable 保持秒语义；真实 ZA38 只读 inspect 显示 Hook
  `adapted/effective`、`compatibility=recognized`。
- [x] 上一轮用户实测返修结果已复核：Qwen Plugin `102 passed`、Plugin 主集合 `155 passed`、Phase 0-4B
  Python 集合 `322 passed`、CLI/Protocol 六文件集合 `80 pass`；当时 Protocol schema 未改变，
  `protocol:check` 通过。随后本轮用户真实 Run 返修已改 canonical v3.7 schema 并重新生成。
- [x] 用户真实 ZA38 Run 暴露的跨端契约已返修：BuildRunAdapter 的
  `run.started.command_provenance` 与 `skill.loaded.provenance` 进入 canonical v3.7 严格四字段
  schema，并以 `protocol:generate` 同步双端类型、schema 副本、digest 和 validator；不升 minor，因
  v3.7 尚未发布且这是既有实现意图的 schema 漏项修复。Host 冻结 negotiated minor，v3.6 及更低
  Plugin Command 在事件/Transcript/Agent 前返回 `PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED`，普通旧
  minor Run 与 v3.6 `commands.bind` 保持兼容；fake transport 验证 raw invocation、一次投影和
  `1..4` 连续事件序列。
- [x] `skill.loaded.provenance` 仅由带 immutable package digest 的 `plugin:` Skill 发送；内置、项目、
  用户 Skill 经过生成的 Python validator 回归，保持无 provenance 的既有事件形状。
- [x] 本轮实际证据：Python 新增生产事件/旧 minor 选择集 `2 passed`；`test_qwen_plugins.py` `103 passed`；
  Plugin/Qwen 主集合 `165 passed`；扩展 Skill/Host `79 passed`；MCP/Phase3/Settings/Protocol 合集
  `100 passed`；CLI/Protocol 六文件集合 `82 pass`、`762 expect()`；生成、协议、类型和
  staged/unstaged diff 检查均通过。

### Phase 4B 可演示停点（待用户手工验证）

- [x] 离线 fake process 只收到通过 declaration、identity、backend 和 denylist gate 的
  declared env；Commands/Skills/Agents 不接收 Settings env，registry、snapshot、wire、
  Transcript 和日志不包含值。
- [x] `settings.list` 同时表达 live `store_state` 与当前 Host `runtime_state`；set/remove
  返回 `pending_restart`，当前 Run/generation 不热更新；Host close/generation replacement
  才释放 immutable snapshot，Run/child 只释放自身临时引用。
- [x] 验证命令、实际计数、平台 backend 未验证边界和 Channels 延期条件记录于
  `tmp/handoff.md`；Channel runtime 未实现。

### 用户真实 Subagent 返修（2026-08-31）

- [x] 为无 MCP 声明的 Plugin Agent 增加真实 AgentEnginePool + Host builder 离线回归，确认空
  视图不依赖 MCP Manager。
- [x] 为有 MCP 声明的 Plugin Agent 增加共享 generation 精确工具视图回归：只注入授权工具，
  server/tool 漂移或缺失按稳定码失败关闭。
- [x] 增加 Managed delegation 稳定错误码透传与未知异常类型级脱敏回归。
- [x] 运行本轮 focused tests、`git diff --check`；完整 Host 集合仍有既有 agents.list schema
  color/approval 字段漏项，已与本轮结果分开记录。

### 用户真实 Hook observation 返修（2026-08-31）

- [x] 修复 observation Hook 的 `diagnostic_log` 关键字从真实 middleware 到 canonical HookRunner
  的传递。
- [x] 新增真实 `PluginRuntimeMiddleware → HookRunner → 本地 fixture Hook` 离线回归，确认成功
  PostToolUse 不再触发 TypeError，日志不包含 payload、路径或秘密。
- [x] 通过 Plugin runtime、Qwen、Delegation、Managed Agent focused tests、typecheck 和
  `git diff --check`；Qwen Phase 3 与 SubagentStop 中的既有失败保持独立记录。

### Phase 4C：Adapter report 重解析与 Qwen SubagentStop 终态对齐

- [x] 给已安装记录写入稳定 `adapter_revision`；refresh 在 registry lock 内复核 package
  digest，并用当前 Adapter 重建 descriptor/component report；未变化不重复递增 revision，
  package/plugin identity/source binding 保持不变。
- [x] 实现 unchanged fingerprint 自动更新并保留 enabled/trusted；changed fingerprint
  原子更新 report 但保留旧 trust，runtime catalog 排除该 Plugin，并建立按 plugin/component
  stable identity 索引的 `reauthorization-required` blocked view；普通 Run 不因全局 stale 状态
  被阻断，只有实际依赖 stale Plugin 能力时才在 dispatch、child、model 或 runtime 前返回包含
  plugin_id/fingerprint/inspect-enable 提示的 `PLUGIN_REAUTHORIZATION_REQUIRED`，显式确认后恢复。
- [x] 覆盖重复启动、并发 refresh、digest 校验、旧 report、trust 保留、fingerprint 变化、
  运行前阻断与 reauthorization 恢复；仅使用临时 home 和离线 Qwen fixture。
- [x] 将 Qwen matched `SubagentStop` 的全部 Hook results 先聚合：failure 与 valid output 分离，
  任一合法 block 按 OR/最严格语义优先，多个 block 的 reason/additionalContext 有界合并；只有无
  block 且有 exception/timeout/nonzero/malformed/closed failure 时才 warning/diagnostic + child
  final result。空 stdout/空 JSON/无 decision 合法 no-decision 不告警；正常 block/continue 继续
  同一 checkpoint，blocking cap 返回最新结果+warning，matcher miss 不执行 Hook。
- [x] 保持 Run/parent/user cancellation 传播；PreToolUse、Policy、权限、MCP、非 Qwen 与
  Hook runtime construction/identity/report 失败继续 fail-closed；Qwen `timeout: 10000`
  仍转换为 10 秒；Channels 不实现。
- [x] 以 fake runner/process/Managed executor/Host 做 red→green，覆盖 warning 传递、日志脱敏、
  取消和跨格式回归；不运行真实 Hook/MCP/LSP、模型、网络或凭据。

### Phase 4C 可演示停点

- [x] 临时 home 中旧 Adapter report 启动后自动重解析；`plugins.inspect` 可观察当前
  `adapter_revision`、fingerprint 和 `authorization_state`。
- [x] unchanged 保留 trust；changed 建立不可执行 blocked index，只有实际请求 stale component
  时在 Run/Agent/child 前明确提示 reauthorization，普通 Run 不被全局状态阻断；确认新 fingerprint
  后恢复；重复/并发 refresh 不重复写入或扩大授权。
- [x] Qwen SubagentStop Hook 级失败只显示稳定 warning 并返回 child 结果；正常 blocking 继续
  child，达到 cap 仍返回结果；取消保持 cancellation。
- [x] 验证：focused `test_subagent_stop.py + test_plugin_refresh.py` 为 `43 passed`，加上
  `test_agent_delegation.py` 为 `56 passed`；本轮相关集合为 `242 passed, 12 failed`，其中
  11 项为既有 Host 用户级 PluginStore 锁环境限制，1 项为带空格 venv 路径导致的既有裸
  node shebang fixture 限制；详情写入 `tmp/handoff.md`。
- [x] 用户已手工执行真实 CLI/TUI 的 `plugins.inspect → reauthorize → Run`，并完成 Slash
  Command 与 Managed 子代理调用复测；CompiledSubAgent `messages` 适配返修后运行正常。

## 返修：Managed Plugin child 上下文与 checkpoint 隔离（2026-09-01）

- [x] 以真实 SQLite/ProjectScopedAsyncSqliteSaver 预先写入可识别父历史；红测在旧实现中观察到
  child 模型实际收到父 Human/AI 消息，固定“首次只含 task”的失败边界。
- [x] 为每个 project + Thread + Run + execution 生成稳定内部 checkpoint `thread_id`；不修改公开
  provenance，不新增 Protocol 或 SQLite schema。
- [x] Host Plugin 与 Compose Stage 将内部 ID 同时传入 `RunContext` 和 LangGraph 根图；同 child
  continue/submit/resume 保持相同 ID，sibling、不同 Run 与 terminal 后重用不共享历史。
- [x] 终态、失败、取消和 acquire 失败清理 checkpoints/writes；virtual history、文件 Snapshot、
  deferred reveal 与 Hook feedback 不继承父/sibling scope。
- [x] `test_managed_plugin_checkpoint.py` red→green `7 passed`；Managed/Compose/Delegation/
  SubagentStop/virtual history/Snapshot focused 合计 `108 passed`。
- [x] 已运行 `typecheck`、`protocol:generate`、`protocol:check` 和 `git diff --check HEAD`；较大
  回归中 sandbox loopback、用户级 PluginStore lock 与裸 node/带空格 venv fixture 的既有环境失败
  已单独记录于 `tmp/handoff.md`。
- [ ] **用户验收停点**：以主仓库 cwd 重新启动真实 ZA38 CLI/TUI，确认 child 首次模型 context 只有
  本次 task + 自身 system/Context/Skill，并确认 SubagentStop continue 保留 child 自身历史；完成
  前不得归档 HC-158 或进入下一阶段。

## Phase 5：Channels 延期决策（已完成）

- [x] 核对当前 ZA38 插件无 `channels` 声明，并按 2026-08-28 用户决定从 HC-158 实现范围移出。
- [x] 保持 Channels `unsupported/effective=false`；禁止 Host 直接 import 未信任模块或建立外部连接。
- [x] 记录未来独立任务的前置条件：Channel Host 安全评审、threat model、进程隔离、认证、所有权、速率限制、关闭协议和 fake E2E。

## 最终收口

- [x] 更新用户兼容矩阵、架构文档、Task 证据和目录/ZIP 验收。
- [x] 完成 review、全量验证与用户真实 ZA38 CLI/TUI 复测；既有基线失败单独记录。
- [x] 将 Channels，以及安装源、link/update/hot reload/workspace scope，在出现真实需求时拆为独立后续 Task。
