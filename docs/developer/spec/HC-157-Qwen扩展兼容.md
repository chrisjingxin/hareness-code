# HC-157 Qwen 扩展兼容规格

关联 [Task](../task/HC-157-Qwen扩展兼容.md)。

## 通俗流程

Plugin 来源进入 staging 后，Harness 先看用户是否明确指定格式，再看根目录是否存在唯一的 Qwen 家族清单。存在 `qwen-extension.json` 或 `devagent-extension.json` 时，两个文件使用同一个 `qwen-code` Adapter；普通目录名或组件目录不能触发 Qwen 推断。Adapter 先输出不带宿主绝对路径的兼容报告与 capability fingerprint，信任并启用后再把已支持的 Context/Agent 转换为既有 canonical 快照与 AgentCatalog。

```text
目录 / ZIP
  → staging 文件边界
  → 显式格式与清单冲突判断
  → qwen-code 静态 Adapter
  → 身份、路径、JSON、组件计数校验
  → PluginDescriptor / capability_fingerprint
  → validate/install 结果
  → trust + enable 门禁
  → ContextLifecycle / AgentCatalog / ResolvedAgentSpec
```

## 公开接口

- `PluginFormat` 增加 `qwen-code`；`hybrid` 仍只表示 portable + Claude。
- `plugins.validate`、`plugins.install` 的 `format` 增加 `qwen-code`，并同步 Protocol 生成产物。
- 根清单二选一：`qwen-extension.json` 或 `devagent-extension.json`；输出 `format: "qwen-code"`，`manifest` 保留实际文件名。
- `PluginComponentReport.kind` 新增 `contexts` 语义；已有报告模型继续承载 `commands`、`skills`、`agents`、`mcp`、`hooks`、`settings` 等静态库存。

## 格式识别规则

1. 显式 `qwen-code`：必须恰好存在一个 Qwen 家族清单；两个清单同时存在返回 `PLUGIN_FORMAT_CONFLICT`，没有清单返回 `PLUGIN_FORMAT_MISMATCH`。
2. 显式 `agent-plugins-1.0` 或 `claude-code`：Qwen 家族清单与对应 portable/Claude 显式清单共存时返回 `PLUGIN_FORMAT_CONFLICT`；没有跨格式冲突时保持原 Adapter 行为。
3. `auto`：先拒绝两个 Qwen 清单或 Qwen 与 portable/Claude 清单共存；再识别唯一 Qwen 清单；没有 Qwen 清单时完全沿用现有 portable、Claude 和 portable+Claude Hybrid 规则。
4. Qwen/DevAgent 不根据 `commands/`、`skills/`、`agents/`、`mcp/` 或普通目录推断格式；没有清单时不加载 Qwen Adapter。

## Qwen/DevAgent 清单语义

首版读取并报告实际使用的字段：

- `qwen-extension.json` 缺少 `commands`、`skills` 或 `agents` 字段时，分别使用根目录中存在的 `commands/`、`skills/`、`agents/` 默认目录；目录不存在时不虚构组件报告。
- `devagent-extension.json` 不使用上述默认目录，仍只解析清单中显式声明的路径。

| 字段 | 识别与校验规则 | 当前运行边界 |
| --- | --- | --- |
| `name` | 必填、非空、安全身份字符串 | 仅写入 descriptor，不参与路径执行 |
| `version` | 可选非空字符串 | 仅写入 descriptor |
| `contextFileName` | Qwen 清单支持 string/string[]；字段缺省或空数组时使用实际存在的根 `QWEN.md`，不存在则不报组件；显式路径必须是包内普通文件，拒绝绝对路径、`..`、符号链接和缺失文件。DevAgent 清单保持现有非空 string 显式路径契约，数组当前按字段错误拒绝 | trust+enable 后从安装快照以 `REFERENCE/STABLE` 注入既有 ContextLifecycle；未通过门禁不注入 |
| `commands` | 可选包内目录/文件，发现 Markdown 文件；标准 Qwen 清单缺省时扫描 `commands/` | 只报告 `commands`，不注册 Slash Command |
| `skills` | 可选包内目录/文件，发现并校验 `SKILL.md`；标准 Qwen 清单缺省时扫描 `skills/`；单文件也必须校验 front matter、description 和正文 | 只报告 `skills`，不建立 Skill 资源树 |
| `agents` | 可选包内目录/文件，标准 Qwen 清单缺省时扫描 `agents/`；Markdown frontmatter 严格校验 name、description、color、approvalMode，并按源码证据处理 tools、disallowedTools、model、runConfig 子集及独立的 permissionMode 映射；未知/类型错误为 invalid | trust+enable 后转换为 canonical AgentDefinition/ExecutionPolicyDefinition，进入实际 AgentCatalog；不执行 Hook 或 Question/Approval 交互 |
| `mcpServers` | 可选 object；`command`、`url`、`httpUrl` 必须是非空字符串，只有合法传输条目才计数；全为畸形时组件为 `invalid` | 只报告 `mcp`，不解析运行配置、不启动服务 |
| `hooks` | 可选 object；首版只接收同步 `SubagentStop` command handler；共享校验 seam 同时限制 matcher ≤512、command ≤32768、timeout 为非布尔数值且 `0 < timeout ≤ 600`、shell 仅为受控值；`async:true`、`env` 和 Qwen 未定义受控语义的 `args` 均 invalid/unsupported，畸形 handler 不计数 | 仅当安装报告与 canonical runtime 必然可构造、enabled+trusted 且 matcher 命中时，由既有 `HookRunner` 在 Managed child 最终输出返回父 Agent 前执行；其余 Hook event 仍 unsupported |
| `settings` | 缺省或空数组合法；非空值明确报告 `unsupported` | 不修改 Harness 配置 |

首版不执行 `channels`、`themes`、`workflows` 等非范围字段。存在这些字段或其他未知字段时，必须进入 `unsupported` 诊断/组件报告，不能静默忽略；字段自身类型错误仍按清单格式错误稳定失败。

## 报告与指纹

仍未接入运行时的 Commands、Skills 和 MCP 报告为 `unsupported/effective: false`；已通过严格校验并接入 canonical consumer 的 Agents、Context 与 SubagentStop Hook 报告为 `adapted/effective: true`。Context 的 `effective` 只表示它已接入 `ContextLifecycle` 参考块链路，不授予任何执行权限；Hook 的 `effective` 只表示它已接入受控终态 gate，不代表无条件执行或授予权限；实际读取/执行仍必须经过 enabled+trusted catalog。Qwen Adapter 与 runtime 复用同一纯校验 seam；若已安装报告与实际包内容发生漂移，runtime 构造失败或目标定义为空，必须留下 `PLUGIN_HOOK_*` 诊断并建立 fail-closed 失败记录，不能退化为“无 Hook/无 gate”。报告仍保留发现数量、相对来源、能力分类和稳定诊断。

ZA38 清洁 fixture 必须得到：

| kind | count | capability |
| --- | ---: | --- |
| `commands` | 3 | `prompt:command` |
| `skills` | 1 | `prompt:skill` |
| `agents` | 3 | `delegation:agent` |
| `mcp` | 1 | `process:mcp` / `network:mcp`（按静态传输形状） |
| `contexts` | 1 | `context:plugin` |
| `hooks` | 1 | `process:hook` |

`capability_fingerprint` 继续使用 canonical JSON + SHA-256；所有上述组件、Agent frontmatter 能力和资源库存都会参与指纹。报告和诊断不返回 staging、store、data 或源目录的宿主绝对路径；能力或包 digest 改变时旧确认不能复用。

清洁 ZA38 fixture 还锁定后续运行时所需的真实字段形状：三个 Agent frontmatter 保留 `color` 与
`approvalMode: auto-edit`；Hook 保留 `SubagentStop` matcher 和 command handler；MCP 保留
`${extensionPath}${/}` placeholder。阶段一/二只锁定和快照化这些字段；阶段三解释 Agent frontmatter，
阶段四只通过离线 fake runner 验证 Hook gate，不执行用户脚本，MCP 仍不连接。

## 第二阶段：静态资源快照

安装成功后，`PluginManager` 从内容寻址的只读 store 建立 `PluginResourceSnapshot`。该快照复用
第一阶段组件报告的相对 `sources`，包含 Commands、Skills、Agents、MCP、Context 和 Hook 的静态
资源。对 Qwen/DevAgent，根目录 `references/`、`scripts/`、`mcp/` 是受控依赖闭包；它们和组件
自身的普通文件一起复制，保持 Skill 的 `../../references/...`、Command/MCP/Hook 的脚本相对关系，
不伪造 `skills/<name>/references/`。隐藏开发文件、`.env*`、凭据命名文件、符号链接和特殊文件
不得进入模型可读快照。快照正文只保存在进程内不可变对象中，管理接口输出脱敏摘要。

公开摘要使用：

- `/.harness/plugins/<plugin-id>/...` 作为唯一模型可见的虚拟根；`source` 仅为包内相对来源。
- `read_text/read_bytes` 只接受当前虚拟根下已捕获的资源，拒绝绝对宿主路径、`.`、`..`、未知资源
  和越界路径；Skill 的相对引用通过快照文件位置解析，并要求目标实际存在。
- 每个资源摘要带有 `read_only: true`。MCP/Hook 以及静态 preview 条目带有 `runnable: false`，
  不转换为运行时连接或 handler。未 trust 的 Plugin 不进入 enabled catalog，因而不会产生可启动
  的进程资源。
- 已知根 placeholder 只在受控的 Command 内容、MCP `command`/`args`/`cwd`/`url` 和 Hook
  handler `command` 字段中逐个解析：`<extensionPath>`、`${extensionPath}`、
  `${extensionPath}${/}`、`${CLAUDE_PLUGIN_ROOT}`。每个文件型目标都必须规范化后位于当前虚拟根
  的允许依赖闭包且已进入快照；未知字段不做全局替换，未知 token、`..`、宿主绝对路径或缺失目标
  稳定失败。

安装后的静态资源通过现有列表链路提供只读 preview，而不是混入 effective runtime catalog。该 preview
只服务于 `format: "qwen-code"` 且当前组件尚未接入 runtime 的 Qwen/DevAgent 资源；按组件过滤，已进入
AgentCatalog 的 Agents 不再投影，尚未接入的 Commands/Skills/MCP 继续显示。portable、Claude 和 Hybrid
已有 supported/adapted 资源不重复投影：

- `plugins.list.static_preview` 按 `commands`、`skills`、`agents`、`mcp` 分区；
- `initialize.static_command_preview`、`skills.list.static_preview`、`agents.list.static_preview`
  和 `mcp.status.static_preview` 分别让已有 Command、Skill、Agent、MCP 列表消费者发现这些条目；
- 每个条目标记 `disabled: true`、`static: true`、`runnable: false`、`read_only: true`，来源只含
  包内相对路径或 `/.harness/plugins/<plugin-id>/...` 虚拟路径。ZA38 清洁 fixture 的四个对应
  列表分别显示 3、1、3、1；portable、Claude、Hybrid 的四个 preview 分区为空，effective 列表仍不
  出现 Qwen 资产。

阶段二快照仍提供完整的只读资源摘要；阶段三只把 Context 和 Agent 接到 canonical consumer，Commands/
Skills/MCP 仍停留在静态 preview，MCP/Hook 不启动，Hook 不执行。

## 第三阶段：Context 与 Agent 权限

### Context 输入 → 判断 → 输出

```text
enabled + trusted Qwen PluginResourceSnapshot
  → 读取 contexts 普通文件并验证 UTF-8/大小/digest
  → 生成 ContextBlock(REFERENCE, STABLE)，来源只写虚拟路径
  → ContextLifecycle.prepare 的 RunContextSnapshot
  → Core Policy、Capability、Environment 仍由 Harness canonical block 决定
```

- `ContextLifecycle` 新增稳定参考块输入，不建立第二套 Prompt 系统；它为每个块统一加上低可信说明，重复相同 key/digest 只保留一次，冲突则失败关闭。
- Qwen `DEVAGENT.md` 在主 Agent Run 和对应 `definition.source` 的 Plugin Agent Run 各最多出现一次；resume、compaction 和 projector 重建复用已保存的不可变 Run snapshot，不重新叠加正文。
- Context 正文可以包含提示注入文本，但只能作为 `REFERENCE/STABLE` 背景；它不能改变 `EffectivePolicy`、Sandbox、工具定义、审批规则、workspace 边界或用户消息。
- `PluginManager.context_blocks_by_source` 只从 enabled+trusted catalog 和已安装快照读取。坏 UTF-8、超大文件、store digest 不一致或越界读取稳定失败关闭，不回退到源目录。

### Qwen Agent → canonical AgentDefinition

Qwen Code 的 Subagent Markdown frontmatter 以 `name`、`description` 为必填，支持 `color`、`approvalMode`、
`tools`、`disallowedTools`、`model` 和可证明的 `runConfig.max_turns` 子集。Qwen 源码的 `approvalMode`
枚举为 `default/plan/auto-edit/yolo`；`permissionMode` 使用独立源枚举并映射为 Harness 请求：
`default→default`、`plan→plan`、`acceptEdits→auto-edit`、`auto→auto-edit`、`dontAsk→default`。
两个字段同时出现时，只有映射后的请求一致才接受；`bypassPermissions`、未知字段、类型错误、冲突模式、
坏 frontmatter 和空正文在 validate/install 前报告 invalid，不能等到 Agent runtime 才静默失效。

Qwen Agent 通过 `AgentCatalog` 建立不可变 `AgentDefinition` 和 `ExecutionPolicyDefinition`。外部
`approvalMode/permissionMode` 只填入 policy 的请求上限；auto-edit 不等价于 bypass 或无条件写入。未声明
工具时 Adapter 采用 Harness 的最小 read-only 请求（read_file/glob/grep），并明确关闭 MCP、Shell、网络、
delegation；显式请求的写、Shell 或 delegation 仍必须与父 Agent 当前 policy 和 Host/workspace policy 求交集。

最终 capability 是：

```text
Plugin Agent request ∩ parent EffectiveExecutionPolicy ∩ Host/workspace boundary
```

任何一层不允许的写入、Shell、网络、MCP 或 delegation 都从子 Agent capability view 消失；子 Agent 不能
扩大父 Agent。Agent fingerprint、Plugin capability fingerprint 或 trust digest 变化时旧确认失效。

### 第三阶段静态 preview 过渡与非范围

- 三个 ZA38 executor 在 `agents.list.agents` 的 effective catalog 出现，`agents.list.static_preview` 不再重复展示。
- `initialize.static_command_preview`、`skills.list.static_preview`、`mcp.status.static_preview` 仍分别保留 3 Commands、1 Skill、1 MCP 的 disabled/static/non-runnable preview。
- portable/Claude/Hybrid 的静态 preview 继续为空；既有 runtime catalog、Skill、MCP 和 Hybrid 行为不改变。
- 本阶段不连接/启动真实 MCP，不执行 Hook/SubagentStop，不实现 Question/Approval 交互、阻断继续或八次上限。

## 第四阶段：SubagentStop 与子 Agent 交互

### 接入条件与输入边界

只有同时满足以下条件的 Qwen/DevAgent Plugin Agent 才会进入终态 gate：Plugin 已安装、当前
ExtensionCatalogSnapshot 中 enabled 且 trusted fingerprint 匹配、Agent 已进入 canonical
`AgentCatalog`，并且已安装组件报告明确为 `hooks: adapted/effective=true`、`SubagentStop` matcher
命中该 Agent ID。未命中、未 trust、未 enable、invalid、unsupported、async 或范围外 Hook 不执行；
Commands、Skills、MCP 不因 Hook 接入而变成 runtime effective。

Hook stdin 是一次冻结且有界的对象，只含 `agent_id`、`agent_type`、有界的 `last_output`、Harness
虚拟 `cwd/workspace` 和 `stop_hook_active`。不传宿主 store/source 路径、环境秘密、密钥或无界 transcript。
Hook runner 仍使用既有最小环境、stdin/stdout/stderr 上限、超时和 Host close 进程收敛。

安装报告与 runtime 定义必须由同一组 matcher、command、timeout、shell、async 和受控字段校验
共同证明可构造。若损坏 store、平台差异或未来校验漂移导致已标记 `adapted/effective` 的 Qwen
Hook 转换失败、定义为空或出现不支持字段，runtime catalog 记录可观察的失败诊断；Host 按
source/event/matcher 建立失败关闭 gate，不能把它当作 matcher miss 静默放行。同步
`SubagentStop` 不允许 async 结果。

### 结果与失败关闭

空 JSON 输出表示 allow；JSON `decision: "allow"` 表示允许返回；`decision: "block"` 可带字符串
`reason` 和 `hookSpecificOutput.additionalContext`（也接受同名顶层补充字段）。非零退出、超时、取消、
输出过大、非 JSON、类型错误、未知 decision 或异常均不放行，并报告稳定的 `SUBAGENT_STOP_*` 诊断。

阻断计数绑定同一个 child execution；用户可通过现有父 Run question 通道选择：

- `submit`：把“执行提交门禁所需提交”作为有界用户选择指令送回同一 Managed runtime/checkpoint，
  再次经过 SubagentStop；不替用户执行 git、不授予新工具或改变 policy；Shell/Git 仍走正常
  EffectivePolicy、workspace guard 和 approval middleware；
- `continue`：把 reason/additionalContext 包装为不可信反馈写入同一 child checkpoint，使用同一
  Managed runtime 开始下一模型回合；
- `skip`：只放行当前一次 gate，不写入 Plugin/Host policy，也不绕过后续 Shell/Git 审批。

连续八次 block/continue 后，下一次 block 返回 `SUBAGENT_STOP_BLOCK_LIMIT`，不能静默放行。交互
无客户端、超时、取消或无效答案返回稳定失败关闭。`ChildInteractionRegistry` 只保存 request 与
`execution_id/parent_execution_id/agent_id/checkpoint_namespace` 的内存 provenance；响应、Run 终态、
owner 断连和 Host close 都必须清理登记，不持有第二个 child execution。

### 权限与运行不变量

用户选择 submit/continue/skip 只影响 SubagentStop gate 的当前裁决，submit 与 continue 都在同一
Managed runtime/checkpoint 开始下一模型回合；最终工具调用仍必须经过既有
`EffectiveExecutionPolicy`、workspace guard 和审批 middleware。Hook 文本是非可信反馈，不能覆盖 Core
Policy、工具定义、用户消息、工作区边界或 approval 规则；本阶段不执行 Hook 提交脚本的 git 操作，
不连接 MCP，不实现第四阶段之后的 Question/Approval 继续交互模型。

### Control-plane 契约闭包

Agent 目录摘要是用户可见的 control-plane 数据，不是只供 Python 内部使用的临时字典。canonical
`agentSummary` 必须保留 `color`、`approval_mode`、`permission_mode` 三个 nullable 字段，并由
`packages/protocol/schema/v3.json` 生成 Python `TypedDict`、TypeScript 类型、双端 validator、schema
副本和 contract fixture；Host 的 `agents.list/inspect` 只能输出同一摘要，CLI 只消费这份类型化结果。
这些字段用于展示和审计，不直接授予 Agent 权限。

`mcp.status` 的正式结果包含 `static_preview`（无静态资源时为空数组）；Host 对 handler 结果仍执行
canonical schema 校验，畸形 handler 只返回 `-32603`，随后请求必须继续走同一 dispatch loop。Qwen Agent
必须至少有一条真实 Host `dispatch("agents.list")` 回归，证明 Agent 字段和 Python/TypeScript 双端契约
同时接受，不能只测试绕过 dispatch validator 的 handler。

## 错误语义与安全不变量

- `PLUGIN_FORMAT_CONFLICT`：清单组合存在互斥格式来源。
- `PLUGIN_FORMAT_MISMATCH`：显式 qwen-code 但没有唯一 Qwen 家族清单。
- `PLUGIN_JSON_INVALID` / `PLUGIN_JSON_ROOT_INVALID` / `PLUGIN_JSON_ENCODING_INVALID`：清单或静态 JSON 不能安全解析。
- `PLUGIN_NAME_INVALID` / `PLUGIN_VERSION_INVALID` / `PLUGIN_MANIFEST_FIELD_INVALID`：身份或字段类型不符合首版契约。
- `PLUGIN_COMPONENT_PATH_INVALID`、`PLUGIN_COMPONENT_MISSING`、`PLUGIN_SYMLINK_REJECTED`：组件路径越界、缺失或经过符号链接。
- Qwen 默认 `QWEN.md` 缺失是“无 Context”而不是错误；显式 `contextFileName` 缺失返回 `PLUGIN_COMPONENT_MISSING`。
- 所有路径解析都通过既有 package-root 安全边界；未知 placeholder 不能被当作宿主路径执行。
- Qwen Agent frontmatter 的未知/类型错误和 `bypassPermissions` 等模式在安装/启用前 invalid；AgentCatalog 诊断不回显正文或宿主路径。
- 第一阶段测试只使用仓库内清洁 fixture/mock，不访问网络、不启动 MCP/Hook、不读取真实 `.env*`。

## 非范围

- MCP 连接、Hook/SubagentStop 生命周期和子 Agent Question/Approval 交互属于阶段四；本阶段只完成 Context、
  AgentCatalog、权限交集和 component-level preview 过渡。
- 不改变 portable/Claude 既有 Adapter、Hybrid 合并、MCP 运行时或 Plugin registry 结构。
