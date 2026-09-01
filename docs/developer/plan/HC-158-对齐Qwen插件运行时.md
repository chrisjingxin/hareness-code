# HC-158 Qwen 插件运行时对齐实施计划

关联 [Task](../task/HC-158-对齐Qwen插件运行时.md) 与 [Spec](../spec/HC-158-对齐Qwen插件运行时.md)。

## 总体顺序

严格按 Phase 0 → 1 → 2 → 3 → 4 → 5 推进。每一阶段先补失败测试、再实现、review 后停下等待验收；不能因为
前一阶段已经识别组件就跳过下一阶段的安全和运行时验收。

## Phase 0：统一 effective gate

### 目标文件

- `packages/agent/harness_agent/plugins/manager.py`
- `packages/agent/harness_agent/plugins/runtime.py`
- `packages/agent/harness_agent/plugins/mcp_adapter.py`
- `packages/agent/tests/test_qwen_plugins.py`
- `packages/agent/tests/test_plugins.py`

### 改动

1. 抽取按 `kind + status + effective + format` 判断运行资格的纯函数，Hook、LSP、Monitor、MCP、Command、Skill 复用。
2. 修复 Qwen manifest 中 unsupported `lspServers`/`monitors` 仍可能被通用 runtime loader 读取的问题；当前 Qwen
   Monitor 永远不加载，LSP 直到 Phase 3 才可 effective。
3. 通过篡改 installed report、unknown field、混合有效 Agent 与恶意 LSP/Monitor 的 fixture 证明不会启动进程。
4. 保持 HC-157 `SubagentStop` adapted/effective 路径可用，portable/Claude/Hybrid 不回归。

### 停点

所有 runtime catalog 只包含 component report 明确允许的条目；恶意 Qwen LSP/Monitor fixture 的 fake spawn 计数为 0。

### Phase 0 实施结果（当前停点）

已完成统一 gate 与 canonical consumer 接入：`manager.py` 的 Command/Skill/Agent/Policy/Team/Context 来源、
`mcp_adapter.py`、`runtime.py` 的 Hook/LSP/Monitor 均不再仅按字段或 status 运行；Qwen Adapter 明确输出 LSP/Monitor
unsupported report。gate 还验证包含 `kind/status/count/sources/capabilities/effective` 的
`capability_fingerprint(plugin.components)` 与已安装 fingerprint 精确相等，diagnostics 文案不参与 trust；声明了组件但缺少
report 时返回 `COMPONENT_REPORT_MISSING`，未声明组件的干净插件不产生噪声。使用 85a7 venv + dc33 `PYTHONPATH`，三个主测试文件
`tests/test_plugins.py tests/test_plugin_runtime.py tests/test_qwen_plugins.py` 实际 `129 passed`；Phase 0 显式集合 `12 passed`；
HC-157 回归 `13 passed, 59 deselected`；portable/Claude/Hybrid `31 passed, 27 deselected`；跨格式 Agent/Context/SubagentStop
`7 passed`；Phase 0 当时停在主任务验收，验收通过后才进入本次 Phase 1。

## Phase 1：Commands + Skills

### 目标文件

- `packages/agent/harness_agent/plugins/qwen.py`
- `packages/agent/harness_agent/plugins/manager.py`
- `packages/agent/harness_agent/extensions/plugin_skills.py`
- `packages/agent/harness_agent/host/run_execution.py`
- `packages/agent/harness_agent/host/agent_host.py`
- `packages/agent/harness_agent/host/run_coordinator.py`
- `packages/agent/harness_agent/plugins/store.py`
- `packages/agent/harness_agent/plugins/resources.py`
- `packages/protocol/schema/v3.json`（仅当 Command invocation contract 需要扩展）
- `packages/cli/src/interactive/commands.ts`
- `packages/cli/src/interactive/command-dispatcher.ts`
- `packages/agent/tests/test_qwen_plugins.py`
- `packages/agent/tests/host/test_server.py`
- `packages/cli/tests/interactive/commands.test.ts`
- TUI/Web command menu focused tests

### 改动

1. 新增 `qwen-command`/`qwen-skill` dialect 校验；只接受 Markdown、合法 YAML frontmatter、非空正文和安全相对来源。
2. Commands/Skills 报告改为逐条 adapted/effective；一个坏文件不伪装成有效条目，component diagnostics 保留。
3. 复用 SkillRegistry 捕获不可变正文和根资源；Command 记录保持 `kind=command`，不进入普通 `/skills` 列表。
4. Host `agent_commands` 输出 Plugin 摘要；CLI 唯一解决内置/其他来源冲突并使用 `extension.command` 回退，再把 exact `command_id → resolved_name` 与 snapshot ID 通过 `commands.bind` 登记给 Host。
5. CLI 提交原始 invocation、stable ID 和 args；Host 在 Run snapshot 内展开 `{{args}}`，Transcript 保留原始调用，模型投影使用展开正文。
6. 检测 `!{...}`、`@{...}` 并报告 unsupported；首阶段不执行 Shell/File expansion。
7. ZA38 三个 Commands 与 `za38-framework` 从 static preview 去重；停用、失信、fingerprint 变化和篡改均不可执行。

### 停点

在 fake model/Host 上输入 `/za38-sdd 创建登录功能`：菜单能补全，Transcript 保留调用，模型只收到一次展开正文，
同一 snapshot 中 `za38-framework` 可按虚拟路径读取。不得连接 MCP 或执行 ZA38 脚本。

### Phase 1 实施结果（当前停点）

- [x] Qwen Markdown Command/Skill 使用独立 `qwen-command`/`qwen-skill` dialect；解析、大小、UTF-8、frontmatter、正文、来源和不支持 expansion 均按条目隔离，symlink、路径越界、篡改和重装竞态 fail closed。
- [x] Adapter、SkillRegistry 和统一 component gate 共同确认 `adapted/effective`；有效 Commands/Skills 从 static preview 去重，非运行条目仍以 non-runnable preview 可见。
- [x] 三个 ZA38 自然命令进入单一 CommandRegistry；嵌套目录使用 `:`，冲突稳定回退为 `extension.command`；CLI 不接收正文或宿主路径，并通过 Protocol v3 minor 6 的 `commands.bind` 把同一 Registry 的 exact binding 交给 Host，Host 不复制 builtin/alias/冲突解析表。
- [x] Host Run 绑定不可变 snapshot，原始 slash invocation 保留在 Transcript，正文只展开一次；`skill.loaded` 记录 plugin/package/command/snapshot provenance；`za38-framework` 只读取顶层 `references/*.md`，`references/origin/` 可保留在净化安装 store，但不进入运行时快照。
- [x] 用 fake model/Host/CLI 完成 `/ZA38-SDD   创建登录功能  ` 的离线停点；不进入 Phase 2 MCP。
- [x] 返修本地目录/ZIP staging：按名称先排除 VCS/OS 元数据、`.env`/`.env.*`、`.npmrc`，净化后再计算 digest/count/snapshot；其他 special file 仍 fail closed，并用 fake socket fixture 与真实 checkout 只读验收。
- [x] 返修 Skill 资源闭包：Qwen 只捕获顶层 `references/*.md`，带 sentinel 的 `references/origin/` 不可读、不列出、不诊断；真实 ZA38 清洁安装 diagnostics 为空。
- [x] 返修 raw invocation contract：Protocol 增加 `raw_invocation`/`command_name` 可选字段和 v3 minor 6 的 `commands.bind`（最小 minor 6）；CLI 保留大小写、内部空格和尾随空格，并把 exact resolved name 绑定到本 snapshot。Host 只校验 stable ID 对应的不可变 binding，不复制 builtin/alias 表；插件声明 `help` 但 CLI 选中 `/bad.help` 时，`/help` 伪装、另一插件命令和 args/raw mismatch 均稳定失败且不产生 Run 记录；v5 协商时 CLI 不调用未知 RPC，Host 返回 `PROTOCOL_MINOR_REQUIRED`。

实际验证：主集合、Phase 1 focused 和本轮真实 ZA38 store/snapshot/diagnostics 结果以 `tmp/handoff.md` 的本轮命令为准；Phase 0、HC-157、Host、CLI/Protocol、跨格式及协议/typecheck/diff 回归均保持通过。Protocol v3 minor 6 与 `commands.bind` v5/v6 跨 minor contract 也已验证。真实 ZA38 checkout 只读安装+enable 得到 Commands 3、Skill 1，diagnostics 为空，store 保留净化源中的 `references/origin/`，运行时 snapshot 不捕获。详细命令与既有 HC-130 检查阻塞见 `tmp/handoff.md`。

## Phase 2：ZA38 stdio MCP

### 目标文件

- `packages/agent/harness_agent/plugins/qwen.py`
- `packages/agent/harness_agent/plugins/mcp_adapter.py`
- `packages/agent/harness_agent/plugins/manager.py`
- `packages/agent/harness_agent/host/agent_host.py`
- `packages/agent/tests/test_qwen_plugins.py`
- MCP manager/Host focused tests

### 改动

1. Adapter 与 `_load_qwen` 共用 server 字段校验，只有 runtime 可构造的 stdio 子项标记 adapted/effective。
2. 将 `${extensionPath}${/}mcp${/}context-server.mjs` 逐字段解析到已安装 store；拒绝未知 token、路径越界和缺失目标。
3. 复用 canonical MCP generation、tool namespace、连接超时、取消和关闭；一个 Server 失败只记录该项。
4. 使用仓库 fake MCP 验证 initialize/list/call/cancel/close，不启动真实 ZA38 Server、不联网、不读取 `.env*`。
5. MCP effective 后从 static preview 去重；disabled/untrusted 状态仍显示 non-runnable preview。

### 停点

trusted+enabled fake ZA38 包在 `mcp.status` 显示一个 effective server，并能调用 fake `search_za38`；停用或关闭 Host 后
进程和工具 generation 全部清理。

### Phase 2 实施结果（当前停点）

- [x] `qwen.py` 增加显式 `_load_qwen`，与 `mcp_schema.py` 共用逐 server 的 stdio、字段、大小、路径和四类 token 校验；只有能构造 canonical `McpServerConfig` 的条目才 `adapted/effective`。
- [x] `mcp_adapter.py` 复用 `McpConnectionManager` 的 namespace、连接超时、工具发现/调用、Run 取消、Host close 和 generation replacement；坏条目进入 `mcp.status.diagnostics`，不阻塞同包有效条目。
- [x] effective Qwen MCP 从 static preview 去重；disabled、untrusted、invalid 条目保持 non-runnable preview 且 fake client 构造次数为 0。
- [x] 只在 canonical `packages/protocol/schema/v3.json` 为 `mcpStatusResult` 增加可选 `diagnostics`，运行 `protocol:generate` 和 `protocol:check`，未手改生成产物。
- [x] 离线 fixture 覆盖有效 ZA38 server、四类路径 token、错误隔离、Host status 诊断和 generation drain；新增真实 fake newline-delimited stdio 子进程经 Qwen Adapter → canonical manager 覆盖 initialize/tools/list/call、取消和 close，patch fake 仅保留给 manager seam；不启动真实 ZA38 MCP、模型、网络或读取凭据。

Phase 2 实际结果：`tests/test_qwen_mcp_phase2.py` `14 passed`（含真实 fake stdio 子进程 initialize/tools/list/call/cancel/close）；canonical MCP manager `72 passed`；Run cancellation `39 passed`；Host server `56 passed`；
Phase 1 三个主文件保持 `150 passed`，CLI/Protocol focused `62 pass`。真实 ZA38 只读临时 home 安装+enable 得到 Commands 3、
Skill 1、MCP config 1，MCP conversion diagnostics 为空，Commands/Skills/MCP static preview 均为 0；store 保留 122 个
`references/origin` 条目，runtime snapshot 捕获 0 个。Phase 3 随后在同一 unstaged 增量上实现，未进入 Phase 4。

## Phase 3：现有 Hook seam 与 Qwen LSP

### 改动

1. Qwen `PreToolUse`、`PostToolUse`、`PostToolUseFailure` 分别转换到现有 HookRunner；按事件子项报告 effective。
2. 定义输入/输出、阻断、additionalContext、原工具错误保持和异步限制；异常按事件语义失败关闭或保留原失败。
3. 为 Qwen `lspServers` 增加 Adapter 报告和 component gate，复用现有 stdio LSP parser/manager。
4. socket/HTTP LSP、无 canonical seam 的 Hook 事件继续 unsupported，不因同一 hooks component 中有一个有效事件而执行其他事件。

### 停点

fake Tool 和 fake LSP 证明三类 Hook 与 stdio LSP 可用；Session/Prompt/Compact/Permission 等事件仍明确 non-runnable。

### Phase 3 实施结果（当前停点）

- [x] Qwen `PreToolUse`、`PostToolUse`、`PostToolUseFailure` 和 HC-157 `SubagentStop` 按事件逐项进入 `HookRunner`；有效子项可 effective，unsupported seam 不借道执行，坏子项生成事件级 fail-closed failure。
- [x] Pre Hook 只阻断且不改变 Tool 参数；Post Hook 只提供有界低可信 `additionalContext`；PostToolUseFailure 保留原始 Tool 错误；Hook payload 使用虚拟 workspace。Qwen Hook/LSP 冻结 bare executable 后不继承 `PATH`、`NODE_OPTIONS` 或动态加载器变量，Claude/portable 继续使用既有 `inherit-path` 语义。
- [x] 联合返修将 Hook command 冻结为 executable + argv 并走 `create_subprocess_exec`，保留带空格/Windows 反斜杠的 argv；Claude/portable 的 shell/exec 语义不变。Managed child release 只清 `(thread_id, run_id, execution_id)`，Host root release 清同 Run 全部 execution；已取消 model call 返回 `PLUGIN_HOOK_RUN_CANCELLED` 且不调用模型。
- [x] 新增 `qwen_lsp.py`，Adapter/runtime 共用 stdio、字段、大小、路径 token、workspace、env 和 extension conflict 校验；有效项复用 `PluginLspManager` 与既有 `lsp` 工具，坏项逐项诊断，不新增 Qwen LSP manager。
- [x] 真实 fake stdio fixture 覆盖 initialize/query、server EOF、畸形 header/body/JSON、socket/HTTP、unknown field/token、path/env/extension conflict、timeout、cancel、close 和 generation replacement；disabled/untrusted/invalid/unsupported 不 spawn。

本阶段未修改 Protocol schema；实现只修改 agent runtime/adapter、测试和文档。focused `tests/test_qwen_phase3.py` 为 `12 passed`，
`tests/test_qwen_mcp_phase2.py` 为 `14 passed`，联合 Phase 2+3 为 `26 passed`；与 `tests/test_qwen_plugins.py tests/test_plugin_runtime.py`
合计 `98 passed`。最终跨包回归、`typecheck`、`protocol:check`、diff 检查和 staged 基线 hash 见 `tmp/handoff.md`。

## Phase 4：Settings

### Phase 4A：Settings 架构设计、威胁模型与实现门禁（已通过主任务评审）

- [x] 读取现有 ConfigChangeService、ModelSettings、PluginStore、AgentHost、
  ThreadPersistence 和 v3.6 minor 协商 seam，确认 Settings 不复制通用 TOML
  配置系统，不进入 registry、catalog、fingerprint 或 Transcript。
- [x] 在既有 docs/developer/architecture/扩展与插件机制设计方案.md 合并
  Plugin Settings 秘密存储与最小环境注入的唯一主方案；不建立新的决策文件。
- [x] 固定唯一主方案：用户私有 ~/.harness/settings/v1 metadata + 当前用户
  credential manager；schema v1、scope lock、revision/CAS、generation journal、
  fsync/atomic replace、崩溃恢复、权限、迁移、删除、卸载和重装语义全部 fail
  closed；不可用时禁止明文 fallback。
- [x] 固定 v1 durable index 顶层 schema_version/scope/scope_binding_digest/revision/
  records/tombstones/journal_refs/workspace_registry；record 不持久化
  store_revision/store_state/runtime_state/pending_operation/diagnostic，store_state
  由 live backend 派生，runtime_state 由当前 Host/generation snapshot 派生；user
  registry 以 digest-only locator 登记全部 workspace scope，并按 user lock → scope
  digest lock 完成 registering/registered/removal_pending/partial/removed 事务。
- [x] 固定 journal temp→fsync→atomic rename→index.journal_refs→锁内重读的唯一顺序；
  ref 前 orphan 无副作用且只从受限 journal 目录清理，ref 缺失/损坏 fail closed，完成
  时先移除 ref 再删除文件；跨文件 phase 由 index active generation/tombstone 和
  PendingOperation closed union 唯一决定前滚/回滚。
- [x] 固定空 store bootstrap：list 对不存在 index 返回 revision=0 且不建文件；只有
  expected=0 的 set 可在锁后原子创建 v1 index，workspace 首次 set 以
  registering→registered 完成 user registry，竞争者返回 revision conflict；absent
  remove 返回 SETTINGS_RECORD_NOT_FOUND 且不建文件。
- [x] 固定 immutable identity、user/workspace binding、workspace > user precedence；
  Qwen 必须声明合法 envVar，setting_key 等于 exact envVar，required=false 只表示值
  可缺失；name/description 不进入 identity/digest。每个有效 Qwen MCP/Hook/LSP
  extension-wide 接收已配置 declared settings 的 child-only overlay，Commands/
  Skills/Agents 不接收 env；Host/generation snapshot 仅在 replacement/close 释放，
  Run/child terminal/cancel/close 只释放自身临时 env/value 引用。
- [x] 固定 declaration_digest 忽略 display 字段但 package_digest 包含 manifest bytes；
  package_digest 变化即使只源于 name/description 也使外层 credential binding stale，
  必须重新 set，不跨 package digest 迁移；tombstone cleanup 后持续保留，仅显式有界
  GC 在无 journal、无旧 Host lease、revision/保留期门槛满足时运行。
- [x] 固定 prepared-intent-first 的 set、tombstone-first 的 remove、跨 scope
  uninstall partial-retry 状态机；set 在 metadata 未 commit 的 prepared/credential_written
  崩溃都精确回滚 new account，失败进入 cleanup_pending/blocked，metadata_committed
  后只前滚 old cleanup；user registry uninstall 清理 user 及已记录的全部 workspace
  scopes，先 user lock 冻结清单，再按 binding digest 加 scope lock；恢复只按 durable
  journal 的精确 credential account，不枚举 backend。
- [x] 固定 live store_state 与当前 Host runtime_state 的双状态 summary，以及
  macOS Security.framework、Linux Secret Service、Windows Credential Manager 的
  capability probe 和不可证明时 fail-closed。
- [x] 固定 v3.7 settings.list/set/remove 完整 wire shape、所有 enum/stable errors、
  required non-negative expected_store_revision/CAS、按 operation 判别的
  SetPending/RemovePending/UninstallPending/MigrationPending closed union、minor >= 7
  与 settings.read/settings.manage capability gate、TTY no-echo 与
  --secret-stdin 规则（sensitive/non-sensitive 相同）、旧 minor 不调用，以及 Phase
  4B 的 Agent+CLI TDD 验收矩阵；值 validator 先判输入形状/byte 上限，超出 65536
  bytes 为 SETTINGS_VALUE_TOO_LARGE，上限内的类型、UTF-8、NUL、stdin 形状错误为
  SETTINGS_VALUE_INVALID。

Phase 4A 当时只修改文档并未进入实现；设计已通过主任务评审。Phase 4B 实施结果见下节。

### Phase 4B 实施结果（主任务代码验收已通过，待用户手工验证）

- [x] `packages/agent/harness_agent/config/settings.py` 完成 Qwen declaration 映射、
  shared value validator、credential backend seam、v1 metadata/journal/ref、空 store
  revision=0、CAS、set/remove recovery、workspace registry 和 per-record uninstall
  cleanup；不可证明 backend 时 fail closed。
- [x] `AgentHost` 完成 Host/generation SettingsSnapshot、workspace > user resolver 和
  Qwen MCP/Hook/LSP child-only env overlay；Commands/Skills/Agents 不接收 Settings env，
  不继承 shell process-control 变量，set/remove 只对 next-host 生效；`plugins.remove
  --purge-data` 在 registry exclusive lock 内重读并校验 plugin/package/revision，partial
  或 conflict 不提交 registry mutation；每次成功 remove 都按当前已安装记录解析并清理
  Settings，停用或 trust 失效不使既有 credential 漏清，`--purge-data` 只额外删除
  Plugin data。
- [x] canonical `packages/protocol/schema/v3.json` 升为 v3.7 并生成 settings.list/set/remove
  及 capability/minor gate；CLI 统一 `--secret-stdin`、TTY no-echo、禁止 argv value，
  set/remove 使用 required `expected_store_revision`；语法固定为位置参数
  `<plugin-id> <setting-key>` 加具名 digest/CAS option，各 action 严格白名单，未知或
  重复 option 不静默忽略，只有 set 允许 `--secret-stdin`。
- [x] 以 fake credential/process/Host/CLI 先 red 后 green，覆盖 bootstrap/CAS、journal
  recovery、precedence、声明漂移、缺值、删除/卸载、脱敏、取消/关闭和跨格式回归；补充
  registry revision drift 不提前 cleanup、旧 MCP snapshot overlay release、Windows
  atomic ACL 前后校验、macOS/Linux/Windows API error/redaction mock 以及真实 CLI
  parse→execute→fake Agent dispatch；未实现复合 UninstallPending/MigrationPending 的
  伪持久化 journal，未知迁移保持 fail closed。

实际验证：Settings/Host focused `61 passed`；Plugin runtime/MCP/LSP focused `35 passed`；
portable/Claude/Hybrid 与 Plugin 主集合 `155 passed`；Phase 0-4B Python 集合 `322 passed`；
canonical MCP/Host/Run/SubagentStop `185 passed`；CLI/Protocol focused `80 pass`。`protocol:check`、
`typecheck`、`uv lock --check --offline`、`git diff --check` 均通过；准确命令和既有 HC-130
文档检查阻塞见 `tmp/handoff.md`。

本轮用户实测返修（2026-08-28，待主任务验收）：Controller 复用握手生成的同一不可变
`CommandRegistry`，覆盖菜单可见但 Enter 提交未知命令的真实路径；Qwen command Hook 在共同
校验边界把原生 `timeout` 从毫秒转换为 canonical 秒（缺省 `60000ms`、上限 `600000ms`），
Claude/portable 秒语义不变。真实 ZA38 只读安装/inspect 得到 Commands `3`、Skill `1`、Hook
`adapted/effective` 和 `compatibility=recognized`，没有执行外部 Hook。

CLI 管理摘要的实际字段为 `env_var`；在当前 Qwen exact `envVar` 映射下，后续
`set/remove` 将该返回值作为 `<setting-key>` 位置参数，不把不存在的 `setting_key` 字段
写入用户命令或验收描述。`--workspace` 与 `--cwd` 为互斥的 workspace 选择别名，不能
静默按优先级选择。

本轮用户真实 ZA38 Run 的跨端返修（2026-08-28）：BuildRunAdapter 已发送的
`run.started.command_provenance` 与 `skill.loaded.provenance` 原先未在 v3.7 schema 声明，导致
CLI validator 拒帧并连带产生 sequence gap。修复只改 canonical `packages/protocol/schema/v3.json`
后运行 `protocol:generate`，严格生成四字段 closed provenance 类型、双端 validator、Python schema
副本和 digest；不升 minor，因为这是尚未发布 v3.7 的既有实现意图修复，不是新的 RPC/capability。
`StartRun` 冻结 negotiated minor：v3.6 或更低连接的 Plugin Command 在 preparation 阶段稳定返回
`PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED`，不进入 Transcript/Agent/事件；普通旧 minor Run 与
v3.6 `commands.bind` 不受影响。v3.7 fake transport 必须看到无额外字段的连续
`run.started → skill.loaded → content.delta → run.completed`（1..4），并保留 raw invocation、
规范化 args 与 provenance；仅 Plugin-backed Skill 附加 provenance，普通内置/项目/用户 Skill 保持旧 payload。

本轮验证已通过：真实 `BuildRunAdapter` 事件的 Python validator 测试 `2 passed`；CLI/Protocol 两文件
`31 pass`、`569 expect()`；Plugin/Qwen `165 passed`，扩展 Skill/Host `79 passed`，MCP/Phase3/
Settings/Protocol `100 passed`，CLI 六文件 `82 pass`；`protocol:generate`、`protocol:check`、
`typecheck` 与两个 staged/unstaged diff-check 均通过。

### Phase 4B 可演示停点

使用离线 fake credential/process/Host/CLI 可观察到：`settings.list/set/remove` 只返回脱敏
摘要，fake MCP/Hook/LSP child 收到正确 declared env，Commands/Skills/Agents 不收到 env；
set/remove 返回 next-host，当前 Host/Run/generation 不热更新；缺值、stale、权限或
backend 失败均不 spawn。实现证据、`protocol:generate/check`、回滚和未验证边界以
`tmp/handoff.md` 及架构文档 Phase 4B 章节为准。Channels 已决定延期并继续禁止运行。

### 用户真实 Subagent 返修（2026-08-31）

- [x] 在真实 `AgentEnginePool`/Host builder seam 上修复空 MCP 视图：`spec.tools == ()` 时不
  acquire MCP Manager；这覆盖未声明 MCP 的 ZA38 Agent。
- [x] 对非空视图借用当前完整 MCP generation，并按 resolved tool view 精确过滤；generation
  server identity 或授权工具缺失时返回稳定错误并 fail closed。
- [x] Managed delegation 仅透传白名单前缀的稳定错误码，未知异常保持类型级脱敏。
- [x] 离线回归覆盖成功构建、未授权工具不泄漏、缺失工具失败和错误码透传；未启动真实模型、
  MCP、Hook、网络或凭据。完整 Host 集合的既有 `agents.list` color/approval schema 失败不属于
  本轮改动。

### 用户真实 Hook observation 返修（2026-08-31）

1. [x] 复核 `PluginRuntimeMiddleware.awrap_tool_call`、`_run_observation_hook` 和
   `HookRunner.run/_invoke` 的参数链，确认 `diagnostic_log` 在观察 Hook 路径断裂。
2. [x] 让 `_run_observation_hook` 接收可选诊断日志并继续传给 canonical `HookRunner.run`，保持
   观察 Hook 异常不覆盖 Tool 原始结果。
3. [x] 用真实 middleware、真实 `HookRunner` 和本地离线 fixture Hook 验证 PostToolUse 成功路径、
   日志摘要传递及 payload/路径脱敏；不运行真实插件 Hook、MCP 或模型。
4. [x] 运行 Plugin runtime、Qwen、Delegation、Managed Agent focused tests、typecheck 和
   `git diff --check`；既有无关失败单独记录在 handoff。

## Phase 4C：Adapter report 重解析与 Qwen SubagentStop 终态对齐

### 依赖有序步骤

1. 在 `PluginModel → PluginStore → PluginManager` 建立稳定 `adapter_revision`：刷新时锁内
   读取已安装记录，复核 package digest，用当前 Adapter 重建 descriptor/report；report 与
   fingerprint 同一事务提交，未变化不递增 registry revision。
2. 在刷新结果进入 Host 的 Skill/Agent/Hook/MCP catalog 前计算授权状态：fingerprint 未变保留
   enabled/trusted；fingerprint 改变保留旧 trust 但从 runtime catalog 排除，建立按 plugin/component
   stable identity 索引的 `reauthorization-required` blocked view。普通 Run 不受全局 stale 状态影响；
   只有 requested Plugin Command/Skill、明确 Plugin Agent/Team 或带明确 provenance 的 Plugin
   executable entry 在 child/model/runtime 前返回包含 plugin_id、当前 fingerprint 和 inspect/enable
   提示的 `PLUGIN_REAUTHORIZATION_REQUIRED`；不复制命令冲突规则或扩大协议。
3. 在 `SubagentStopController → ManagedAgentExecutor → AgentDelegation` 先聚合全部 matched Hook
   results，再按 Qwen OR/最严格语义裁决：failure codes 与 valid outputs 分离，任一合法 block 优先，
   block 的 reason/additionalContext 有界合并；只有无 block 且有失败时 warning + allow child。空
   stdout/空 JSON/无 decision 的合法 no-decision 不告警；非空 malformed 仍在无 block 时 warning；
   正常 block/continue 继续同一 checkpoint；blocking cap 返回 result+warning；matcher miss 不调用 Hook；
   Run/parent cancellation 原样传播。
4. 保持非 Qwen、PreToolUse、Policy、权限、MCP、Hook runtime construction/identity/report 的
   fail-closed seam；保留 Qwen command Hook `timeout` 毫秒到 canonical 秒的转换，不接入 Channels。
5. 先运行单元/集成 red→green，再运行 Agent/Host/Plugin 集合；使用临时 home、离线 package
   fixture、fake Hook runner/process，不运行真实 Hook、MCP、LSP、模型、网络或凭据。

### 可演示停点：刷新后运行前授权与 Qwen Hook warning

- [x] 在临时 home 安装并启用 ZA38 fixture，构造旧 `adapter_revision`，重新启动/刷新后可由
  `plugins.inspect` 观察当前 report、fingerprint 和 `authorization_state`。
- [x] 能力指纹未变时观察 enabled/trusted 保留；能力指纹改变时观察旧 trust 不自动扩大、
  `reauthorization-required` 和 stale component 请求的稳定阻断；普通 Run 不被全局 stale 状态
  阻断。
- [x] 使用新 fingerprint 显式确认后再次 Run，观察 child final result 正常返回；Hook exception、
  timeout、nonzero、malformed/type-error/closed 和 blocking cap 只附 warning/diagnostic，不丢弃结果；
  matcher miss 不执行 Hook，取消仍为 cancellation。
- [x] 本停点 focused `test_subagent_stop.py + test_plugin_refresh.py` 为 `43 passed`，加上
  `test_agent_delegation.py` 为 `56 passed`；本轮相关集合实际为 `242 passed, 12 failed`。
  失败项是既有 Host 用户级 PluginStore 锁环境限制（11 项）和带空格 venv 路径导致的既有
  裸 node shebang fixture 限制（1 项），详见 `tmp/handoff.md`，不得据此宣称相关全集通过。

## 返修：Managed Plugin child execution-scoped checkpoint（2026-09-01）

本返修处理真实 CLI/TUI 证据暴露的根图身份错误。LangGraph 顶层图会丢弃非空
`checkpoint_ns`，因此 child 必须拥有与公开 provenance 分离的内部 checkpoint `thread_id`。

- [x] 用真实 `ThreadPersistence` / `ProjectScopedAsyncSqliteSaver` 预先写入父 Thread checkpoint，
  新增 Managed child 集成红测，确认旧实现把父历史带入第一次模型调用。
- [x] 在 `ExecutionRef` 生成稳定 execution-scoped 内部 ID；Host Plugin 与 Compose Stage 将其绑定
  到 graph config 和 `RunContext`，保持公开 Thread/Run/execution provenance 不变。
- [x] 使同一 child 的 continue/submit/resume 复用同一根图身份；terminal、失败、取消和 acquire
  失败均清理该 execution 的 checkpoint/writes，Plugin/Stage 进程内 Snapshot/feedback 也按 execution
  scope 清理。
- [x] 禁止 child virtual history、文件 Snapshot 和 deferred reveal 继承父 Thread；保留父 Transcript
  只接收最终 CompiledSubAgent message 的契约。
- [x] 完成 focused red→green、Managed/Delegation/SubagentStop/Host/Qwen 相关回归、typecheck、
  diagnostic protocol generate/check 和 diff-check；sandbox/用户级 PluginStore 锁造成的既有失败
  保留为环境限制。
- [ ] 用户在主仓库重新启动真实 ZA38 CLI/TUI，确认修复后的 Managed Plugin Agent 首次 context 与
  SubagentStop continue 行为；确认前不得把本返修视为 Task 完成。
- [ ] 用户手工验证真实 CLI/TUI 的 `plugins.inspect → reauthorize → Run` 提示和 warning 展示；
  完成前不归档 Task、不提交、不推送。

## Phase 5：Channels 延期决策（已完成，不实现）

1. 已核对 `/Users/beichen/Desktop/大模型/za38-cli-extension/devagent-extension.json`：当前 ZA38 插件没有 `channels` 声明。
2. 2026-08-28 用户决定本次不开发 Channels；HC-158 以 Phase 0-4B 的核心运行时能力进入手工验证和收口。
3. Adapter 继续把 Channels 报告为 `unsupported/effective=false`，不得动态 import、启动额外进程或建立外部连接。
4. 未来出现真实 Channel 需求时另立任务；新任务必须先完成 Channel Host 安全评审和 threat model，定义独立子进程/受控 IPC、认证、入站身份、Thread/Run 所有权、速率限制、重放防护、断连/取消/关闭和审计，再做 fake E2E。

该延期决策是 HC-158 的完成边界，不把 Channels 伪装成已实现，也不阻塞 ZA38 核心 Commands、Skills、Agents、MCP、Hook、LSP 与 Settings 的交付。

## 管理面后续任务

核心运行时稳定后，再单独设计 Git/npm/Marketplace/URL 安装、`link`、update、workspace install scope 与热重载。
当前 Task 不改变内容寻址复制安装和 next-host 生效语义。

## 每阶段验证

```bash
cd packages/agent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q <focused files>
cd packages/cli && bun test <focused files>
bun run protocol:generate   # 仅 Protocol 变化阶段
bun run protocol:check
bun run typecheck
git diff --check
```

最终再扩大到 Agent/CLI 全量与 `project:check`；环境或既有失败必须与范围内结果分开记录。
