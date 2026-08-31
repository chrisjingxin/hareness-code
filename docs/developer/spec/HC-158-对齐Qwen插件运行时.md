# HC-158 Qwen 插件运行时对齐规格

关联 [Task](../task/HC-158-对齐Qwen插件运行时.md)、[Plan](../plan/HC-158-对齐Qwen插件运行时.md) 与
[HC-157 基线](../task/HC-157-Qwen扩展兼容.md)。

## 1. 当前事实

HC-157 commit `255be38` 已提供：

- Qwen/DevAgent manifest 识别、目录/ZIP staging、内容寻址 store、capability fingerprint 与 trust gate；
- `PluginResourceSnapshot` 和 `/.harness/plugins/<plugin-id>/...` 虚拟只读路径；
- effective Context、AgentCatalog、Policy 交集以及 Qwen `SubagentStop` final gate；
- Commands、Skills 的 canonical runtime 与 MCP 的静态库存/preview；Phase 2 已把 ZA38 stdio MCP 接入同一 canonical lifecycle，其余 Qwen MCP 形态仍为静态 preview。

Harness 已经有可复用的 canonical consumer：

- CLI `CommandRegistry`、`agent_commands` 初始化摘要和 requested Skill 执行链；
- Agent `SkillRegistry`、不可变 manifest/resource snapshot 与 Run 级 requested Skill；
- `PluginMcpLoadResult`、`McpServerConfig`、Host MCP generation；
- `HookRunner` 的 `PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`SubagentStop`；
- Claude stdio LSP 的 `PluginLspManager`。

因此本任务不得新增 Qwen 专属 Command loop、Skill loader、MCP Manager 或 LSP 进程管理器；Adapter 只把
外部格式转换到这些 canonical consumer。

## 2. 必须先修复的门禁缺口

`load_plugin_runtime_catalog` 当前会为 `qwen-code` 读取通用 LSP/Monitor 字段，而 Qwen Adapter 并未把
这些字段报告为 effective 组件。即使 ZA38 当前 manifest 没有这些字段，这仍违反“unsupported 不执行”。

Phase 0 必须建立统一不变量：

```text
runtime component exists
  iff installed component report.status in {supported, adapted}
  and report.effective == true
  and plugin is enabled + trusted
  and installed digest/fingerprint still matches
  and canonical runtime conversion succeeds
```

Hook、LSP、Monitor、MCP、Command、Skill 都必须使用同一组件门禁；转换漂移不得退化为“没有组件”，而要产生
稳定诊断并在会影响权限或终态时失败关闭。报告完整性还必须满足
`capability_fingerprint(plugin.components) == plugin.capability_fingerprint`，且 fingerprint payload 至少包含
`kind/status/count/sources/capabilities/effective`；仅替换 installed `components`（包括 `sources`）而不更新授权
fingerprint 时返回 `PLUGIN_RUNTIME_COMPONENT_BLOCKED: kind=<kind>; reason=COMPONENT_FINGERPRINT_DRIFT`，六类
consumer 均不可执行。diagnostics 文案不进入 fingerprint。若包内入口声明了组件但 installed report 缺失，返回对应的
`reason=COMPONENT_REPORT_MISSING`；未声明该组件的干净插件不产生缺失噪声。Qwen Monitor 在官方 extension schema 中
不是已确认能力，继续 unsupported，不能因 Claude runtime 的通用 loader 而启动。

### Phase 0 已落地边界

`runtime_component_eligibility` 是唯一组件运行资格判断：它按 Plugin format 开放的 component kind、
enabled/trusted 状态、安装身份、单一 component report、`status ∈ {supported, adapted}` 和
`effective=true` 依次判断；runtime consumer 继续调用 store 的安装 package digest 校验，trust fingerprint
不一致也 fail closed；installed component report 的执行来源绑定漂移也必须 fail closed。canonical consumer 转换失败仍追加
稳定 diagnostics，不能把失败吞成空 catalog；报告缺失只有在包内入口确实声明该组件时才诊断。
Qwen `lspServers`/`monitors` 在 Adapter 中明确报告为 `unsupported/effective=false`，Phase 0 的 format gate
拒绝 Qwen LSP、Monitor、MCP 进入 runtime；Qwen `SubagentStop`、Agent、Context 的既有 effective 路径保持不变。

这会改变 HC-157 已安装记录的 trust 计算语义：旧记录的 fingerprint 不含 `sources`，不会自动迁移或自动扩大信任；
运行 gate 对这类记录 fail closed，需重新安装/生成带来源绑定的报告并显式 enable。新安装记录和 HC-157 的有效
Qwen Agent/Context/SubagentStop 路径保持原行为。

### Phase 0 验证证据

在 `packages/agent` 以 85a7 的 Python 3.13 venv 加载 dc33 的 `PYTHONPATH`，
`tests/test_plugins.py tests/test_plugin_runtime.py tests/test_qwen_plugins.py` 的 Phase 0 基线为 `129 passed`；显式门禁、source binding、
缺报告与恶意 Qwen fake-spawn 集合为 `12 passed`，spawn counter 为 `0`；HC-157 Qwen/SubagentStop/Context 为
`13 passed, 59 deselected`；portable/Claude/Hybrid 为 `31 passed, 27 deselected`；跨格式 Agent/Context/SubagentStop 为
`7 passed`。未启动真实 ZA38 进程、模型或网络。

## 3. Command 与 Skill

### 3.1 注册模型

Qwen Markdown Command 继续复用 SkillRegistry 的不可变内容捕获，但记录必须有独立 `kind=command` 与
`dialect=qwen-command`，不能伪装成普通 Skill。Adapter 校验成功后：

- Commands component 为 `adapted/effective: true`；
- Skills component 为 `adapted/effective: true`；
- 对应条目从 static preview 去重；
- `PluginManager.skill_sources` 只从 enabled+trusted catalog 提供来源；
- Host initialize 的 `agent_commands` 只包含元数据、canonical ID 和 requested record ID，不包含正文或路径。

### 3.2 名称与冲突

- 文件 `commands/za38-init.md` 的自然名称为 `/za38-init`；子目录使用 `:`，与 Qwen 规则一致。
- 内置、项目或其他插件无冲突时使用自然名称。
- 冲突时自然名称保留给优先级更高来源，Qwen 命令稳定回退为 `/<extension-name>.<command-name>`。
- 名称、别名、大小写归一化和冲突结果由单一 Registry 快照计算；不得因加载顺序随机变化。
- `/za38` 只作为前缀补全，不自动合成不存在的命令；要执行 `/za38` 必须由插件提供 `za38.md`。

### 3.3 执行与投影

CLI 的单一 `CommandRegistry` 解析原始 slash invocation，并提交原始调用、stable command ID、已解析的
`command_name` 和规范化参数；CLI 随同当前 Skill snapshot ID 通过 `commands.bind` 登记同一 Registry 的
不可变 `command_id → resolved_name` 映射。Host 只从本次 Run 绑定的 Skill/Plugin snapshot 按 stable ID 读取正文，
不复制 CLI 的 builtin、alias 或冲突优先级表，也不重新解析 UI 命令：

`commands.bind` 是 Protocol v3 minor 6 的 operation，最小 minor 固定为 6。CLI 只有在 initialize 协商结果
`minor >= 6` 且确有 Plugin Command 时才发送它；v5 或更低的旧 Agent 不会收到未知 RPC，有 Plugin Command
返回稳定 `COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED`，没有 Plugin Command 则保留普通非插件 Run 的既有兼容路径。
新 Host 对协商到低于 6 的连接在业务 handler 前返回 `PROTOCOL_MINOR_REQUIRED`，因此不会悄悄接受 v6-only binding。

```text
用户 Transcript: `/ZA38-SDD   创建登录功能  `
模型投影: <command body 中 {{args}} 替换为“创建登录功能”后的内容>
provenance: plugin id + package digest + command id + snapshot id
```

- `{{args}}` 只做文本占位替换；无占位符时在正文后追加原始参数。
- 替换不得重新解释 placeholder、路径、YAML 或 shell；正文和参数均有独立字节上限。
- Qwen `!{shell}` 与 `@{file}` 在首个 Command 阶段明确 unsupported；不得把它们作为普通文本悄悄放行。
- Timeline/持久化保留用户原始调用（例如 `/ZA38-SDD   创建登录功能  `），避免把插件大段正文伪装成用户输入；模型投影保存 command provenance。
- `run.start.requested_skill` 的 Command 形状可带 `raw_invocation` 与 `command_name`；Host 必须验证它们分别等于
  原始 `message`、CLI 解析出的命令 token，并验证 stable ID 在当前 snapshot 中解析为 effective command record；
  还必须在本连接登记的 immutable binding 中查到同一个 stable ID，并让 raw/name 都与该 binding 的 exact resolved name
  匹配。不能只因 raw/name/args 三者彼此自洽就绑定到另一条 Command，也不能把 record 自身的自然名当成未被 Registry
  选中的候选。同时验证 `args` 等于 raw invocation 按规范化规则解析后的参数。
  Host 不知道当前 UI builtin/alias 清单，因此不复制出第二套冲突解析结果；缺少或过期 binding 失败关闭。
  缺字段、原文、身份或参数不一致均在受理前返回稳定 `COMMAND_INVOCATION_*`，普通 Skill 不受这两个可选字段影响。
- requested command 在 Run preparation 绑定 snapshot，运行中重装或停用不改变当前 Run，下一 Run 使用新 catalog。

### 3.4 Skill 语义

- Qwen `skills/<name>/SKILL.md` 转为 canonical SkillRecord，保留 user/model invocable、argument hint 和工具请求的安全子集。
- Skill 根引用只通过已捕获的顶层 `references/*.md` 运行时资源解析；`references/origin/` 等开发原始素材不进入
  Skill 虚拟资源、静态列表或诊断，不得复制或伪造私有 references 目录。
- Command 可声明或在正文中要求 Skill，但最终请求仍需 Skill 存在于同一 trusted snapshot，并通过 Host Policy。
- ZA38 `za38-framework` 的根 `references/` 只读可访问；模型不能通过 Skill 获得额外 Shell、网络、MCP 或写权限。

### 3.5 Phase 1 实施证据与停点

Phase 1 已把上述 Commands + Skills 子集接入现有 canonical consumer，未进入 MCP：

- `packages/agent/harness_agent/plugins/common.py` 提供有界的 Qwen Markdown parser；坏 UTF-8、非法 frontmatter、空正文、大小/参数超限、symlink、路径越界以及 `!{shell}`/`@{file}` 均按条目产生稳定诊断，不会把同一 component 的其他合法条目降级或伪装为有效。
- `qwen.py` 的 `commands`/`skills` 报告只把实际可由 `SkillRegistry` 读取的条目标为 `adapted/effective`；`manager.py` 用统一 gate 生成来源，`static_preview` 只移除 effective 条目，disabled、untrusted、invalid、unsupported 仍保留 non-runnable preview。
- `agent_commands` 只下发摘要、stable command ID、requested record ID 和 invocation contract。Qwen 自然命令名、嵌套 `:` 与稳定 `extension.command` 冲突回退由单一 CLI Registry 解析；CLI 通过 v3 minor 6 的 `commands.bind` 把 exact resolved name 和 snapshot ID 绑定到 Host，Host 只验证该 binding，不复制 builtin/alias/冲突表。协议新增 canonical `commands.bind` schema、最小 minor 6；生成器只增加 `min_minor` metadata 的双端常量输出，随后运行 `protocol:generate` 与 `protocol:check`，未手改生成文件；v5 协商下 Host 返回 `PROTOCOL_MINOR_REQUIRED`，CLI 不发送该 RPC。
- `run_execution.py` 从本 Run 的不可变 Skill snapshot 展开 `{{args}}` 一次；原始 slash invocation 进入 Transcript，模型只收到一次展开正文，`skill.loaded` 带 plugin id、package digest、command id 和 snapshot id。重装后的新 catalog 不改变旧 Registry；同一 snapshot 只捕获顶层 `references/*.md`，`references/origin/` 不可读且不产生诊断。

返修补充：本地目录与 ZIP 统一在 staging 的名称剪枝阶段排除 `.git`、常见 VCS/OS 元数据、`.env`/`.env.*`、`.npmrc` 等，不读取被排除文件正文；净化后的完整安装源进入 content-addressed store，才参与 digest 与 file count。运行时 snapshot 再按 Qwen Skill 的明确闭包只捕获顶层 `references/*.md`；因此 `references/origin/` 可以保留在 store，但不进入 Skill 虚拟资源、static preview、diagnostics 或 Protocol。其他位置的 symlink、hardlink、socket、FIFO 和 special file 仍 fail closed，归档中同名元数据采用同样安全忽略策略。真实 ZA38 checkout 的只读临时 home 验收得到 Commands 3、Skill 1，Commands/Skills preview 均为空，diagnostics 为空，store 含 origin，snapshot/Protocol 不含 origin。

返修后的离线链路覆盖 `/ZA38-SDD   创建登录功能  `：CLI 保留 raw invocation，并将同一 Registry 生成的
`za38-sdd` exact binding 通过 `commands.bind` 交给 Host；Host 再以该 binding 和 `创建登录功能` normalized args
绑定 requested command。自然名、嵌套名、fallback、builtin/alias 和插件冲突都由同一 CLI Registry 决定，Host 不维护
builtin/alias 表；identity/args/raw mismatch 或未选中的 `/help` 伪装在 Transcript 和 Agent 之前失败关闭。实际主集合和 Phase 1 focused 计数以 handoff 的本轮命令为准；协议只改
`packages/protocol/schema/v3.json` 后运行 `protocol:generate/check`，未手改生成文件。Phase 2 已在下节记录，Phase 3 已在后段记录。

## 4. MCP

Qwen `mcpServers` 由 `mcp_adapter.py` 新增显式 `_load_qwen` 分支，转换到现有 `McpServerConfig`：

- 首个停点只支持 ZA38 所需的 stdio：非空可执行文件、字符串 args、可选受限 cwd/env、连接超时上限；
- `${extensionPath}`、`${workspacePath}`、`${/}`、`${pathSeparator}` 只在允许字段逐项展开；未知 token、越界路径、
  宿主绝对目标和缺失包内文件失败关闭；
- Server 名称进入 plugin namespace，配置级同名 server 的优先级按现有 Harness 规则处理；
- Plugin 环境使用最小 allowlist，保留变量最后写入，Settings 值只能由 Settings resolver 定向注入；
- 一个 Server 失败只隔离该 Server，不能阻止其他配置与 Plugin Host 启动；转换诊断通过 `mcp.status.diagnostics` 返回，连接失败保留逐 server status；
- 自动化测试只使用仓库 fake stdio MCP，不连接真实 `za38.03_code_index`、Embedding 或内网服务。

### 4.1 Phase 2 实施边界与证据

Qwen `_load_qwen` 只读取 Qwen manifest 的 `mcpServers`，不借用 Claude/portable loader，也不新增 Qwen MCP Manager。
Adapter 与安装报告共用逐 server 校验：只允许 `stdio`、字符串 command/args/env、包内或当前 workspace 的安全 cwd、有限 timeout，
并只展开 `${extensionPath}`、`${workspacePath}`、`${/}`、`${pathSeparator}`。未知 placeholder、路径越界、宿主绝对路径、
缺失目标、symlink、special file、错误类型和超限返回稳定 diagnostic；同包有效 server 仍可进入 canonical `McpServerConfig`。

有效 server 进入既有 `McpConnectionManager` 的 snapshot、namespace、工具发现/调用、Run 取消、Host close 和 generation
replacement 路径。`mcpStatusResult` 仅增加可选 `diagnostics: string[]`，由 canonical schema 生成双端类型和 validators；
effective 条目从 static preview 去重，disabled/untrusted/invalid 条目不构造 client 且继续显示 non-runnable preview。

Phase 2 离线证据为 `tests/test_qwen_mcp_phase2.py` `14 passed`（含真实 fake stdio 子进程 initialize/tools/list/call/cancel/close）、canonical MCP manager `72 passed`、Run cancellation `39 passed`、Host `56 passed`，
并保持 Phase 1 主集合 `150 passed`、CLI/Protocol focused `62 pass`。真实 ZA38 只读安装+enable 得到 Commands 3、Skill 1、
MCP config 1，conversion diagnostics 为空；store 中有 122 个 origin 条目，但 runtime snapshot 捕获 0 个，未启动真实
MCP/Hook/LSP/Monitor、模型或网络，也未读取 `.env*`/`.npmrc`/凭据。

HTTP/SSE、认证 header 和远程 URL 在 stdio 停点通过后另行验收，不能随 stdio 一并默认开启。

## 5. Hook 与 LSP

### 5.1 Hook

Qwen Hook 只在 Harness 已有同语义生命周期 seam 时适配：

| Qwen 事件 | 本任务初始目标 |
| --- | --- |
| `PreToolUse` | 复用 HookRunner，可阻断；保持 tool policy/approval 优先 |
| `PostToolUse` | 复用 HookRunner，输出作为低可信数据 |
| `PostToolUseFailure` | 复用 HookRunner，不能掩盖原始工具失败 |
| `SubagentStop` | 保持 HC-157 行为 |
| Prompt、Session、Compact、Permission、Todo、Message 等 | 没有等价 Host seam 前继续 unsupported |

每个新事件都要先定义输入字段、输出决策、超时、取消和异常语义，再把 component 子项标记 effective；不能把
整个 `hooks` component 一次性视为全部事件已支持。

### 5.2 LSP

Qwen `lspServers` 支持内联 object 或包内 JSON 文件，但只转换为已有 stdio `PluginLspManager`：

- Adapter 和 runtime 共用字段校验；component 未 effective 时绝不读取或启动；
- command/args/env/workspaceFolder/extensionToLanguage 使用已有有界规则；
- socket/HTTP transport、宿主 workspace 越界、扩展名冲突保持 unsupported 或诊断隔离；
- LSP 查询仍只通过 Harness 工具/能力边界，不把任意 JSON-RPC 方法暴露给 Plugin。

### 5.3 Phase 3 已实现语义

Hook payload 只包含事件名、稳定 Run/Tool 标识、虚拟 workspace、脱敏 permission mode、映射后的 Tool 名称、Tool 参数和有界结果/错误类型。
`PreToolUse` 的 exit code 2 或明确 deny 决策返回 error `ToolMessage`，底层 Tool 不执行；Hook 不能改写参数，也不能替代既有审批和 policy。
`PostToolUse` 的 `additionalContext` 只进入下一次模型请求的低可信标记块，一次性投影，不改变 Tool 返回值；`PostToolUseFailure` 的 Hook
失败、超时或取消不会掩盖原始 Tool 异常。Pre 不能异步，Post 允许已有异步语义，SubagentStop 保持同步 HC-157 gate；每项 Hook 构造失败只绑定到
对应事件/matcher 并 fail closed。

Qwen LSP 的 adapter/runtime 共享 `qwen_lsp.py` 校验。默认/inline/包内 JSON server 只允许 stdio，`transport`/`type` 冲突、未知字段、未知
placeholder、路径越界、宿主绝对路径、缺失或 symlink 目标、reserved env、错误类型、超限和扩展冲突逐项产生稳定诊断。有效项才进入已有
`PluginLspManager`；仅有 `definition`、`references`、`hover`、`diagnostics` 四类 Harness 工具查询，取消/超时/Host close/generation
replacement 都关闭旧 client。Qwen Hook/LSP 在 runtime catalog 阶段把 bare executable 冻结为受控绝对路径；Hook command 同时冻结为
`executable + argv`，不再用 shell quoting 重组，因此带空格或 Windows 反斜杠的 argv 不依赖 POSIX shell 语义。子进程不继承
`PATH`、`NODE_OPTIONS` 或动态加载器变量；Claude/portable 的既有 `inherit-path` 定义不改变。Settings 留到 Phase 4。

### 5.3.1 Phase 2/3 联合返修不变量

- Hook 的一次性 `additionalContext` 必须以真实 `RunContext` 的 `(thread_id, run_id, execution_id)` 为 key；并发 Run 不能互取。Managed child release 只清精确 execution，顶层 Host Run release/close 清理同一 `(thread_id, run_id)` 下全部 execution；取消和异常路径同样不得残留，缺少有效上下文直接返回稳定 `PLUGIN_HOOK_RUN_CONTEXT_REQUIRED`。
- 已取消的 `RunContext` 在 model middleware 边界 fail closed，返回 `PLUGIN_HOOK_RUN_CANCELLED` 且不调用模型；上层 task cancellation 仍按既有 Run contract 收敛。
- Phase 2 的 stdio 证据必须包含真实仓库 fake 子进程，而不只 patch `MultiServerMCPClient`：从 Qwen Adapter 进入 canonical manager，观察 initialize、tools/list、tools/call、取消和 close；patch 测试仅用于 manager seam 与 generation lease。
- LSP 的 EOF、非 ASCII/畸形 header、截断 body、非法 JSON 和 server early exit 都归一化为 `PLUGIN_LSP_*`，并在错误返回前关闭 process、stderr task 和 client；不能把底层 `UnicodeDecodeError` 或 `IncompleteReadError` 泄漏到 Host。

Phase 3 不改变 Protocol schema；跨格式 portable/Claude/Hybrid、HC-157 Agent/Context/SubagentStop 与 Phase 2 MCP 继续使用原 canonical seam。

## 6. Settings

Qwen Settings 是运行时环境的输入，不是 manifest 内的秘密值：

- manifest 只声明 `name`、`description`、`envVar`、可选 `sensitive`；所有
  Qwen setting 可选，禁止默认秘密值；Harness required 固定为 false；
- 支持 user/workspace 两个作用域，workspace 覆盖 user；值不进入 Plugin registry、日志、fingerprint、Protocol
  响应或测试 fixture；
- CLI/Host 通过专用管理方法设置、列出“是否已配置”和删除，不回显敏感明文；
- MCP/Hook/LSP 只能取得同一 Qwen 插件已声明、已配置且通过 denylist 的全部 envVar，
  作为 child-only overlay；Commands、Skills、Agents 不 spawn、不接收 Settings env。
  禁止 `PATH`、`NODE_OPTIONS`、动态加载器等进程控制变量；
- Qwen 缺少或为空的 envVar 字段不是可选值，而是 declaration invalid；合法 envVar
  对应的 credential value 可以缺失，此时只省略该 env entry。未来 Harness-native
  的 required 缺值才使对应 component disabled 并给出诊断，任何格式都不允许以当前
  shell 环境静默补值。

Settings 需要独立的 Protocol、安全存储与迁移架构评审，不能夹带在 MCP 实现中。

### 6.1 Phase 4A 已确定的设计门禁（已通过主任务评审）

Phase 4A 唯一设计依据是 [HC-158 Settings Phase 4A 设计](../architecture/扩展与插件机制设计方案.md)
中的同名章节。本阶段只完成设计与威胁模型，没有修改产品代码、Protocol schema、生成物、
CLI 或测试实现；设计已通过主任务评审，Phase 4B 的实现证据见 6.2。

- Qwen 只接受 ExtensionSetting 的 name、description、envVar、sensitive?；name、
  description、envVar 均为必需 string，sensitive 可选，Qwen required 固定 false。
  envVar 经过有界安全标识符校验后按原样保留，setting_key 就是 exact canonical
  envVar；缺少、为空或非法 envVar 是 declaration invalid。name/description 只展示，
  不进入 record identity 或 declaration_digest；digest 绑定 exact envVar、sensitive
  effective boolean、required=false 和 qwen-extension-wide-v1。
- Qwen 没有 component consumer 字段；唯一映射是同一插件的每个有效 MCP/Hook/LSP
  子项接收该插件 workspace > user 解析后已配置的全部 declared settings child-only
  overlay。Commands、Skills、Agents 不接收 env，env 不得进入 argv/cwd/config；
  value_type/consumer/required 扩展只能属于未来 Harness-native schema。
- 所有值统一由现有配置/持久化 seam 驱动的 Settings service 管理：metadata 在
  用户私有 ~/.harness/settings/v1，值只放当前用户 credential manager。不可证明
  backend、权限、锁、atomic replace、fsync 或 schema/journal 时稳定 disabled，
  禁止明文文件、PluginStore、Git workspace、registry、catalog、fingerprint、
  Transcript、日志和 fixture。
- v1 durable index 的唯一顶层 shape 是 `schema_version=1`、`scope`、
  `scope_binding_digest`、index-level `revision`、`records`、`tombstones`、
  `journal_refs` 和 `workspace_registry`；record 只保存 immutable identity、展示字段、
  exact envVar、sensitive、required=false、consumer_scope 和 credential generation，
  不保存 store_revision、store_state、runtime_state、pending_operation、diagnostic 或
  secret。user index 的 workspace_registry 以 digest-only locator 登记 workspace
  scope；registering/registered/removal_pending/partial/removed 的登记与移除按 user
  lock → workspace digest 顺序可重试，不枚举 credential backend。
- identity 是 plugin_id、package_digest、declaration_digest、setting_key、env_var
  和 scope_binding_digest。user digest 绑定固定域、Harness home filesystem identity、
  OS user/profile principal 和 backend namespace；workspace digest 绑定 root filesystem
  identity、symlink、额外 trusted roots 和 trust policy。跨用户/profile/root 变化、
  声明漂移和重装都 stale，display name 不参与查找，workspace 覆盖 user。
  declaration_digest 忽略 name/description，但当前 package_digest 是包含 manifest bytes
  的完整 package hash；只要 package_digest 变化，旧 credential 按外层 binding 不可复用，
  必须重新 set，HC-158 不实现跨 package digest 迁移。
- journal file 与 index.journal_refs 的顺序固定为 scope lock 内 temp→fsync→atomic
  rename journal、原子提交 refs、锁内重读确认，然后才允许 credential 或 record/tombstone
  mutation。ref 前崩溃的安全 orphan 只在固定 journal 目录内按普通文件、权限和安全文件名
  校验后删除；ref 缺失/损坏/越权则整个 scope SETTINGS_STORAGE_UNAVAILABLE。完成时先
  移除 ref、重读确认，再删除 journal；跨文件 phase 不一致按 index active generation/
  tombstone 与 closed PendingOperation union 唯一裁决。
- 尚不存在的 index 在 settings.list 中逻辑返回 `store_revision=0`，不创建文件。set/remove
  在锁后重读，只有 expected=0 且 index 仍不存在时 set 才 bootstrap；workspace 首次 set
  同时按 user lock → workspace lock 推进 registry registering→registered，两个并发首次
  set 只有一个成功，另一方 SETTINGS_STORE_REVISION_CONFLICT；bootstrap ref 提交创建
  revision=1 的空 v1 index，record 成功后再递增。不存在 index 或无 record 的 remove
  返回 SETTINGS_RECORD_NOT_FOUND，不创建 index。
- list 读取 live index/backend 派生的 store_state，同时附带当前 Host startup snapshot
  派生的 runtime_state；唯一 summary 要能表达 configured + pending_restart，且不把
  runtime_state 回写 metadata。set/remove 为 next-host 生效，不热更新当前
  Run/generation/Hook/LSP。Protocol v3.7、settings.list/set/remove、
  TTY no-echo 与 --secret-stdin 由 Phase 4B 实现；sensitive 和 non-sensitive 使用
  同一输入规则，值、account、路径永不出现在 wire。`settings.list` 的 summary 使用
  Protocol 字段 `env_var`；Qwen 中它等于 exact canonical `envVar`，客户端把该字段作为
  set/remove 的 `<setting-key>` 位置参数，不能假设 wire summary 另有 `setting_key` 字段。
  CLI 的 `--workspace` 与 `--cwd` 是互斥别名，同时出现时返回稳定参数错误，不按出现
  顺序选择其中一个。
- `store_state` 是闭集 configured/absent/stale/pending/tombstoned/partial/blocked：
  blocked 只表示 metadata、权限或 credential backend 无法安全验证；它与只由当前
  Host/generation snapshot 派生的 `runtime_state` 不互换，也不写回 durable record。
- 管理契约的稳定失败包括 SETTINGS_PROTOCOL_MINOR_REQUIRED、SETTINGS_STORAGE_UNAVAILABLE、
  SETTINGS_SCOPE_INVALID、SETTINGS_WORKSPACE_SCOPE_REQUIRED、SETTINGS_RECORD_STALE、
  SETTINGS_DECLARATION_STALE、SETTINGS_DECLARATION_INVALID、
  SETTINGS_DECLARATION_AMBIGUOUS、SETTINGS_ENV_FORBIDDEN、
  SETTINGS_INPUT_NONINTERACTIVE、SETTINGS_BACKEND_UNAVAILABLE、SETTINGS_VALUE_INVALID、
  SETTINGS_VALUE_TOO_LARGE、
  SETTINGS_RECORD_NOT_FOUND、SETTINGS_STORE_REVISION_CONFLICT、SETTINGS_OPERATION_IN_PROGRESS、
  SETTINGS_CLEANUP_PENDING、SETTINGS_UNINSTALL_PARTIAL 和 SETTINGS_UNINSTALL_CONFLICT；
  完整 wire 字段和 enum 以架构文档中的 typed contract 为准。交互 set 只能用 TTY
  no-echo；非交互 set 必须显式从非 TTY stdin 读取一个有界 UTF-8 值，禁止 argv value
  和任意 workspace path。set/remove 必须提交必填的 `expected_store_revision`；首次
  set 也先 list，Host 锁后重读并 CAS，冲突返回 SETTINGS_STORE_REVISION_CONFLICT，
  HC-158 不提供无条件写入。list/remove 只提交 scope 与完整 stable identity/expected
  revision。
- PendingOperation 是按 operation 判别的 SetPending、RemovePending、UninstallPending、
  MigrationPending closed union，各自只接受对应 phase（set: prepared/credential_written/
  metadata_committed/cleanup_pending；remove: prepared/tombstone_committed/
  credential_cleanup_pending；uninstall: planned/tombstones_committed_per_scope/
  deleting_per_record/partial_retryable；migration: prepared/rewriting_credentials/
  metadata_committed/cleanup_pending）。非法组合 fail closed；SettingsSummary 只返回
  operation/state/retryable 脱敏摘要，不返回 journal operation_id、account、record、
  scope list 或路径。
- value validator 在 Protocol string、CLI stdin 和 Host 中相同：一个不含 NUL 的 UTF-8
  string，UTF-8 bytes 上限 65536，不做 trim 或隐式解码。先检查类型/输入形状和 byte
  上限：超出 65536 bytes 返回 SETTINGS_VALUE_TOO_LARGE；上限内的非法/不完整 UTF-8、
  NUL、多个 stdin record 或其他输入形状返回 SETTINGS_VALUE_INVALID。CLI 使用有界
  lookahead 读取，允许单个 LF/CRLF framing 并拒绝额外记录；Host 必须再次校验，
  value 不回显。
- set 状态为 prepared → credential_written → metadata_committed → cleanup_pending
  → done；prepared/credential_written 在 metadata 未 commit 前崩溃时都按 journal 精确
  new account 回滚删除，删除失败进入 cleanup_pending/blocked，旧 active 保持；
  metadata_committed 后只前滚清理 old account。remove 为 prepared → tombstone_committed
  → credential_cleanup_pending → done；user registry uninstall 为 planned → 已记录
  per-scope tombstones → per-record deletion → partial_retryable/done。恢复只使用
  journal 中的精确 account，不枚举 backend；跨 scope 部分失败可重试且不误删。未知
  metadata 版本或旧明文来源不自动迁移；未来迁移先写 intent，按上述 migration/set
  状态机重建 credential，新记录提交后才清理旧 generation。
- remove cleanup 完成后 tombstone 仍持久保留，v1 不自动按时间删除；显式有界 GC 只有
  在无 journal_ref、exact account 已清理、scope revision 达到 tombstone revision+1、
  且无旧 Host/generation/Run lease 证明时执行，最小保留 30 天、触发上限 180 天，
  无法证明则继续保留，不能让旧请求或 retry 复活 record。
- user registry uninstall 清理 user record 及 user index 已记录的全部 workspace
  scopes，而非仅当前 workspace；先锁 user index 冻结 immutable scope 清单，再按
  workspace binding digest 排序加锁并逐项提交 tombstone/cleanup。缺失或不可读的已
  记录 scope 返回 partial 并保留 journal，未记录 scope 不触碰，禁止 backend 枚举。
- 每次成功的 `plugins.remove` 都从当前已安装 Plugin record 按
  `plugin_id/package_digest` 解析 Settings，不经过 enabled+trusted runtime catalog；因此
  已停用或 trust 失效的已安装 Plugin 仍必须清理既有 credential。`--purge-data` 只额外
  删除 Plugin data。registry revision 冲突或 cleanup partial 时保持安装记录，不返回已
  清除成功。
- Phase 4B 只能在 macOS Security.framework、Linux secretstorage/Secret Service、
  Windows Credential Manager API 的 capability probe 可证明时启用，否则 fail closed。
  resolver 采用 workspace > user，extension-wide 地给每个有效 MCP/Hook/LSP child
  注入该插件已配置的 declared envVar；Commands/Skills/Agents 不接收 env。拒绝
  PATH、NODE_OPTIONS、PYTHONPATH、LD_/DYLD_ 等 process-control 变量，不从当前 shell
  fallback。值以最短生命周期保存并 best-effort 释放引用；Python/TypeScript 字符串
  不能可靠擦除，残余内存、swap、调试器和 core dump 风险属于明确威胁。
- Host/generation 的 immutable SettingsSnapshot 只在 generation replacement 或
  Host close 释放；每个 Run/child 的临时 resolved env/value 引用才在自身 terminal、
  cancel 或 child close 时 best-effort 释放，并发 Run 不能互相释放 snapshot。

Phase 4B 必须以 fake credential/process/Host/CLI 先写 red tests，再实现 metadata
recovery、precedence、声明漂移、缺值、删除、卸载/重装、最小 env、redaction、取消/
关闭和跨格式回归。Channels 已从 HC-158 实现范围移出，继续保持 unsupported/effective=false，
不能通过 dynamic import 绕过 canonical Host。

### 6.2 Phase 4B 实施证据（主任务代码验收已通过，待用户手工验证）

本停点已按 6.1 的 canonical 设计实现 Settings runtime，Task 仍保持“待验收”，等待
用户完成真实插件手工验证。实现复用现有 AgentHost、PluginManager、MCP/Hook/LSP child
lifecycle 和 CLI/Protocol seam，未建立第二套配置系统；Channels 已决策延期并保持
`unsupported/effective=false`。

- `packages/agent/harness_agent/config/settings.py` 实现 Qwen exact `envVar` declaration、
  shared value validator、credential backend interface/capability gate、v1 metadata 与
  journal/ref、空 store revision=0、required CAS、set/remove recovery、workspace registry
  和 per-record uninstall cleanup。Secret 不进入 registry、catalog snapshot、fingerprint、
  Protocol response、Transcript、日志或 fixture。
- `packages/agent/harness_agent/host/agent_host.py` 在 Host/generation 创建 immutable
  SettingsSnapshot，按 workspace > user 解析；只向有效 Qwen MCP/Hook/LSP child 注入
  extension-wide declared env overlay，并拒绝 process-control/env fallback。Commands、
  Skills、Agents 不接收 Settings env；set/remove 只对 next-host 生效。
- `packages/protocol/schema/v3.json` 为 canonical v3.7 settings.list/set/remove 提供
  摘要、状态、pending、capability/minor gate；生成物通过 `protocol:generate` 同步。
  CLI 使用唯一公开标志 `--secret-stdin`，TTY no-echo，所有值不从 argv 接收、不回显，
  set/remove 必须提交 `expected_store_revision`。
- CLI Settings 语法固定为 `harness plugins settings <list|set|remove>`；set/remove 使用
  位置参数 `<plugin-id> <setting-key>`，digest/CAS 使用必填具名 option，只有 set 可带
  `--secret-stdin`。list/set/remove 分别执行严格 option 白名单，未知、缺值、重复或
  冲突身份 option 直接失败，不由 parser 静默忽略。
- red→green 测试覆盖 declaration/gate、precedence、bootstrap/CAS、journal recovery、
  stale/invalid/缺值、remove/uninstall、redaction、next-host、child-only env、取消/关闭
  和跨格式回归；fake credential/process/Host/CLI 离线运行，并覆盖 registry revision
  drift 不提前 cleanup、旧 MCP snapshot overlay release、Windows atomic ACL 前后校验、
  macOS/Linux/Windows API error/redaction mock 及 CLI parse→execute→RPC。实际命令与计数、
  真实平台未验证边界见 `tmp/handoff.md`。

#### 6.2.1 用户实测返修约束（2026-08-28，待主任务验收）

Controller 的菜单解析、手输 `/` 解析、选择后 Enter 和 `command.execute` 必须持有同一份启动
握手 `CommandRegistry`；Dispatcher 不得回退到只含 builtin 的全局 Registry。Plugin Command 的
提交结果同时保留精确 `rawInvocation`、独立规范化 `args`、stable command ID 和
`requestedSkill` contract。

Qwen command Hook 的 `timeout` 遵循 Qwen 毫秒契约，在 Adapter 和 canonical runtime 的共同边界
转换为秒：缺省 `60000ms`，合法范围为 `1..600000ms`，因此 `10000ms` 变为 `10s`；零、负数、
布尔值、字符串和超过 `600000ms` 的值返回稳定 invalid。Claude/portable 不转换，继续使用既有
秒单位和 `1..600s` 上限。真实 ZA38 只读安装/inspect 必须看到 Hook `adapted/effective` 且
`compatibility=recognized`，但不执行 Hook。

本轮离线验证：Controller focused `5 pass`；Qwen Hook/真实安装选择集 `13 passed`、完整
`test_qwen_plugins.py` `102 passed`；Plugin 主集合 `155 passed`，Phase 0-4B Python 集合
`322 passed`；CLI/Protocol 六文件集合 `80 pass`。

### 6.2.2 用户真实 ZA38 Run 的 v3.7 provenance 契约返修（2026-08-28）

BuildRunAdapter 已经是 HC-158 Phase 1 的生产发送方，因此 `run.started` 的
`command_provenance` 和 `skill.loaded` 的 `provenance` 必须进入同一 canonical v3.7 schema。两者均为
可选字段，但对象是 closed union：只接受 `plugin_id`、64 位小写 `package_digest`、`command_id` 和
`snapshot_id`，禁止额外字段；`command_provenance.command_id` 必须为非空 stable Plugin Command ID，
普通 Skill 的 `skill.loaded.provenance.command_id` 可为 `null`。该摘要绝不携带正文、store 路径、宿主绝对路径
或 secret。只有带 immutable package digest 的 `plugin:` Skill 才发送 `provenance`；内置、项目和用户
Skill 保持原有无 provenance payload，禁止用空 digest 伪造 Plugin 身份。

本轮不升 Protocol minor。当前 v3.7 尚未发布，字段已由既有 HC-158 运行实现发送，问题是 schema 漏项而非
新增 operation/capability；唯一来源仍是 `packages/protocol/schema/v3.json`，通过 `protocol:generate`
更新双端类型、Python schema 副本、digest、fixture/validator 产物，再由 `protocol:check` 校验。

兼容规则固定为：initialize 协商到 v3.7 的连接才能运行 Plugin Command 并接收完整 provenance；协商到 v3.6
或更低时，Host 在 Run preparation 返回 `PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED`，在 Transcript、Agent 和
事件发送前终止，不让旧客户端收到未知 payload 字段。v3.6 已有 `commands.bind` 不改变；没有 Plugin Command
的普通 Run 仍走旧 minor 兼容路径。这样不依赖严格旧 validator 忽略未知字段，也不删除审计所需 provenance。

验收必须使用真实 BuildRunAdapter 事件而非手造相似对象：Python `EventEnvelope.model_validate` 与 TS
`assertEventEnvelope` 都验证生产的两帧，CLI fake transport 还验证 raw invocation、请求身份和
`run.started → skill.loaded → content.delta → run.completed` 的连续 `1,2,3,4` 序列；旧 shape 的普通
Run/Skill 事件仍可通过，provenance 额外字段、路径和正文均拒绝或不出现。

本轮证据：上述 Python 生产事件与旧 minor 测试 `2 passed`，CLI 两文件契约/传输测试 `31 pass`、
`569 expect()`；完整 Plugin/Qwen 集合 `165 passed`，扩展 Skill/Host `79 passed`，MCP/Phase3/
Settings/Protocol 合集 `100 passed`，CLI 六文件集合 `82 pass`。`protocol:generate`、`protocol:check`、
`typecheck`、未暂存和已暂存 `git diff --check` 均通过。

## 7. Channels

Qwen Channels 会动态加载 JavaScript 模块并接管外部消息入口，Harness 当前没有等价 canonical Channel Host。
当前 ZA38 的 `devagent-extension.json` 没有 `channels` 声明；2026-08-28 用户决定本 Task 不实现
Channel runtime。Channels 因此不是 HC-158 的交付或验收前置条件，继续
`unsupported/effective: false`，禁止直接 `import()` 插件入口、启动额外进程或建立外部连接。

未来若出现真实需求，必须另立任务并先完成独立架构安全评审，至少定义模块隔离、进程边界、认证、
入站身份、Thread/Run 所有权、断线清理、速率限制、重放防护和审计；完成 fake Channel E2E
和安全 review 后才允许改变 component effective 状态。

## 8. 管理面非范围

Git/npm/Marketplace/URL 安装、`link`、auto-update、user/workspace install scope 和文件热重载属于 Plugin 管理面，
不是本轮核心运行时前置条件。它们应在核心组件稳定后拆为后续 Task；当前内容寻址复制安装与 next-host 生效保持不变。

## 9. 兼容与回滚

- portable、Claude、Hybrid 继续使用原 Adapter；公共 consumer 的改动必须有跨格式测试。
- 每阶段只把实际接入的 component 或子项从 static preview 移出；未接入项仍可见且 non-runnable。
- 每阶段可以通过关闭对应 component gate 回滚，不修改 PluginStore 已安装内容，不需要数据迁移。
- Settings 若引入持久化，必须有独立 schema version、备份和删除路径；不得和 Plugin registry 同表混存秘密。
