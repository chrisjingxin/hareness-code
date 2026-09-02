# HC-166 对齐 Qwen 插件管理规格

关联任务：[HC-166](../task/archive/HC-166-对齐Qwen插件管理.md)

上游：[HC-157](../task/HC-157-Qwen扩展兼容.md)、[HC-158](../task/HC-158-对齐Qwen插件运行时.md)

参考：Qwen Code 本地只读快照 `6a432ad2ebce57b0b48cd3d6a8f4f7fab50c33fe` 的 `packages/core/src/extension/`。

用户结果与范围以 Task 为准。本文只规定领域模型、公开契约、状态、生命周期、迁移和 invariant；具体文件顺序与两轮实施安排留给 Plan。

## 1. 通俗目标

Plugin 的正常使用改成：

```text
安装来源
  → Harness 自动识别格式并展示真实内容
  → 用户确认一次
  → 复制到 Harness 管理的安装目录并按 scope 启用
  → Shell 管理进程结束
  → 之后启动 TUI 或 Web 时按 user/workspace scope 加载
```

用户不再经历“安装 → inspect → 复制内部 ID → 复制 capability fingerprint → enable → 重启”这条 Harness 私有流程，也不再为 Settings 复制 package digest、declaration digest 或 store revision。

本任务对齐的是 Qwen Code 的 **Shell CLI 安装后启动使用**语义：按插件名称和作用域管理，安装/更新时确认，后续 Harness 进程自动加载。Qwen 在对话中使用 `/extensions install` 的能力不属于本任务。Harness 仍是跨进程 Host + Protocol 架构，因此内部可继续使用事务 store、generation 和不可变 Run 快照，但这些实现细节不得变成用户凭据。

## 2. 已确认决策

1. 删除 Plugin 专用 `capability_fingerprint`、`trusted_capability_fingerprint` 及其授权状态；与模型 Profile、执行策略或上下文快照有关的同名 fingerprint 不属于本任务。
2. 删除 `authorization-required`、`reauthorization-required`。Adapter 变化只触发重新解析，不触发能力重新授权。
3. 安装确认成功后直接在所选 scope 启用；不再要求紧接一次 enable。
4. 用户以插件名称操作。内部稳定 ID 可以保留，但只能出现在持久化、日志关联或高级诊断中。
5. Settings 用户输入只包含插件名称、setting 名、scope 和值；所有 digest/CAS 留在 Host 内部或删除。
6. 用户状态只有“已加载、已禁用、加载警告、加载失败”。Adapter 内部诊断可以更细，但不能泄漏成另一套产品状态机。
7. 删除独立 `static_preview`。没有进入 canonical consumer 的条目不能出现在 Commands、Skills、Agents 或 MCP 的可用列表中。
8. 正常安装自动识别格式；显式 `--format` 仅允许作为高级诊断和歧义排查选项。
9. 安装、更新、启停、卸载通过独立 Shell CLI 完成；之后启动的新 Harness 进程读取新状态。本任务不刷新其他已经运行的 Host。
10. TUI 与 Web 复用同一 Host/Protocol catalog 使用 Plugin 能力，但不新增 Plugin 管理界面。
11. ZA38 未声明 Channels，本任务不实现 Channel runtime。

## 3. 术语与模型

```text
Installed artifact
└─ Harness 管理目录中的一份不可变插件副本。与原始源码目录解耦。

Plugin name
└─ manifest 声明的人类可读名称。大小写不敏感地唯一，是正常管理入口。

Internal plugin id
└─ Host 用于持久化、迁移和日志关联的稳定身份。不是日常用户输入。

Activation
├─ user：所有工作区的默认启停状态。
└─ workspace：当前工作区对 user 默认值的显式覆盖。

Runtime generation
└─ 一组已解析并进入 canonical consumers 的 Plugin runtime 快照。

Load warning
└─ 至少一个组件可用，但另有组件被跳过或某个加载分支失败。

Load failed
└─ Plugin 已安装且有效 activation 为 enabled，但当前 generation 无法提供可用组件。
```

### 3.1 安装与作用域分离

安装副本是用户级受管资源，同一名称只有一份当前 artifact；scope 只决定是否在某个工作区生效，不复制第二份 package。

| 操作 | 是否需要 scope | 语义 |
| --- | --- | --- |
| install | 是，缺省 `user` | 安装 artifact，并写入该 scope 的 enabled activation |
| list/inspect | 是，缺省 `user` | 省略时只查看 user activation；只有显式 `workspace` scope 才按当前 workspace 计算覆盖状态 |
| enable/disable | 是，缺省 `user` | 修改 activation，不改 artifact |
| update | 否 | 替换该名称的 artifact，保留已有 activation；可选提供新的本地 source |
| remove | 否 | 卸载 artifact，并清理其全部 activation；仅停用某 workspace 应使用 disable |
| settings list/set/remove | 是，缺省 `user` | 管理该 scope 的 setting；workspace 值覆盖 user 值 |

workspace activation 的优先级高于 user activation；没有 workspace override 时继承 user。workspace install 等价于“安装 artifact + user 默认 disabled + 当前 workspace enabled”，不会让其他工作区意外启用。

## 4. 名称与身份

### 4.1 名称唯一性

- 安装名称使用 manifest 的 canonical name；名称匹配大小写不敏感，展示保留 manifest 大小写。
- 同一安装库不能有两个同名 artifact。再次 install 同名插件返回 `PLUGIN_ALREADY_INSTALLED`，提示使用 update。
- update 后名称不得改变；名称改变视为新插件，必须先卸载旧插件再安装。
- Shell CLI 不要求 `local-xxx/name`。高级 inspect 可展示脱敏 source label 和 internal ID，用于迁移冲突诊断，但正常 mutation 不接受 internal ID 代替名称。

### 4.2 内部完整性

本任务保留现有内容寻址 package、`package_digest`、原子 registry revision、锁和 generation，原因是它们分别承担复制校验、并发提交与不可变 Run 的内部职责。约束如下：

- digest/revision 不出现在正常 mutation 参数、确认文案或操作指引中。
- digest 变化不再意味着用户授权失效。
- Adapter report revision 可保留；Host 发现 revision 变化时从已安装 package 重新解析并刷新状态。
- `PluginResourceSnapshot` 与 `/.harness/plugins/...` 虚拟路径继续作为 Host 内部资源隔离层，来源固定为受管安装副本，不读取原始源码目录；Protocol/UI 不展示宿主 store 路径或要求用户理解虚拟路径。

这是一项 Harness 架构适配，不改变用户所见的 Qwen 式管理语义。

## 5. Adapter 自动识别

`format=auto` 是所有正常入口的固定缺省值。识别只读取 staging 后的安全副本：

1. Agent Plugins 1.0 manifest 进入 portable Adapter。
2. `.claude-plugin/plugin.json` 进入 Claude Adapter；同时满足既有 Hybrid 明确定义时进入 Hybrid Adapter。
3. `qwen-extension.json` 或 `devagent-extension.json` 进入 Qwen Adapter。
4. Claude/Gemini converter 只有在其输入特征完整且转换结果通过 canonical 校验时生效。
5. 多个互斥格式同时命中且无法由既有 Hybrid 规则唯一解释时，失败为 `PLUGIN_FORMAT_AMBIGUOUS`，不得靠遍历顺序猜测。
6. 没有格式命中时失败为 `PLUGIN_FORMAT_UNSUPPORTED`。

显式 format 可以保留在 `validate/inspect` 或开发环境中，用于确认 Adapter 诊断；正常 Shell install/update 教程不展示该选项。

## 6. 安装与更新确认

### 6.1 统一预览

install/update 在写 registry 前生成一份有界 `PluginMutationPreview`：

```text
operation: install | update
name / old_version? / new_version?
source_label
activation_scope（仅 install）
components[]:
  kind / count / display_sources
settings[]:
  name / description / required / configured_at_scope
warnings[]
```

预览只列出 Adapter 已验证且存在 canonical consumer 的组件；不支持、坏条目或局部转换失败放入 `warnings`，不进入可运行组件清单。预览不包含插件正文、绝对 store 路径、secret value、internal ID、digest、revision 或 capability hash。

### 6.2 Consent

- Shell CLI 的交互式管理命令处理结构化 `interaction.plugin_consent`；TUI 和 Web 不注册该管理交互。
- consent 绑定当前这次 prepare 操作，由 Host 在同一受控调用内完成；用户不复制确认 token 或哈希。
- 用户拒绝或取消时返回 `PLUGIN_OPERATION_CANCELLED`，staging 被清理，registry、activation 和 Settings 都不变。
- 用户确认后再原子提交。install 同时写 activation；update 保留原 activation。
- 无法处理 consent 的非交互客户端返回 `PLUGIN_CONSENT_REQUIRED`，不得默认同意。本任务不新增 `--yes` 或其他跳过确认的自动化入口，也不能恢复 fingerprint/digest 作为替代确认。

### 6.3 本地来源与后续启动

当前任务只承诺本地目录和 zip：

- install 把来源复制进受管 store，并保存足以支持本地 update 的脱敏 origin metadata。
- 原始来源之后被移动或删除，不影响已安装插件运行。
- `plugins update <name>` 优先重读已保存来源；来源不可用时返回 `PLUGIN_SOURCE_UNAVAILABLE`。用户可用 `--source <path-or-zip>` 指定新来源，但这只发生在 update，不是每次启动参数。
- link、Marketplace、Git/npm/URL 下载和自动更新不在本任务范围。

## 7. Activation 与有效状态

### 7.1 状态计算

对当前 workspace，每个插件只返回一个产品状态：

| 状态 | 条件 | 是否进入 runtime |
| --- | --- | --- |
| `loaded`（已加载） | 有效 activation enabled，全部已声明且受支持组件加载成功 | 是 |
| `disabled`（已禁用） | workspace override 或 user 默认计算为 disabled | 否 |
| `warning`（加载警告） | enabled，至少一个组件加载成功，但存在被跳过组件或非致命加载失败 | 仅成功组件 |
| `failed`（加载失败） | enabled，但没有组件可进入 runtime，或关键 catalog 构造失败 | 否 |

`supported/adapted/unsupported/invalid`、`effective`、`can_enable` 可以暂留 Adapter 内部类型以降低实现风险，但不得进入产品状态、正常 Protocol response 或用户操作前置条件。后续实现若能直接收敛内部类型，可在不改变本规格的前提下删除。

### 7.2 列表一致性

- `plugins.list/inspect` 是 Plugin 状态与 warnings 的唯一管理摘要。
- `commands.list`、`skills.list`、`agents.list`、`mcp.status` 只包含当前 generation 中真实存在的条目。
- 所有 `static_preview` response 字段与 Host 拼装路径删除。
- disabled/failed Plugin 的组件不能出现在补全、AgentCatalog、SkillRegistry、MCP tool list、Hook/LSP runtime 中。
- warning Plugin 只暴露成功加载的组件；失败条目只能在 Plugin warnings 中看到。

## 8. 启动加载与生效时点

### 8.1 Shell mutation

install、update、enable、disable、remove 和 Settings set/remove 在独立 Shell CLI/sidecar 中完成持久 mutation。提交前完成 staging、Adapter 解析、registry/Settings 事务和可加载 catalog 的离线构造；成功响应表示下一次 Harness 启动可以读取完整状态。

### 8.2 新进程加载规则

```text
Shell mutation 已提交
  → 管理进程返回并退出
  → 用户启动新的 Harness TUI 或 Web Host
  → Host 读取 registry 和 activation
  → 用当前 Adapter 构建启动 PluginCatalog generation
  → 装配 Commands / Skills / Agents / Context
  → 装配 MCP / Hook / LSP / Settings child environment
  → 本进程所有新 Run 绑定该启动 generation
```

- 已经运行的 TUI/Web Host 保持其启动快照，不监听另一个 Shell 进程的 registry 变化，也不重建当前 Thread/Run 的 Plugin Context。
- 新 Host 启动时用当前 Adapter 重新解析已安装 package；Adapter revision 变化不触发授权，只影响本次加载结果。
- 单个插件或组件失败不阻塞其他插件加载；catalog 身份、registry 损坏或 generation 原子性无法证明时整体启动 fail closed。
- enabled Plugin 构造失败时显示 `failed` 或 `warning`；用户退出后可通过 Shell update、disable 或 remove 修复，再重新启动。
- mutation response 返回持久状态、activation 和 warnings；不返回“当前会话已热更新”或 `runtime_generation` 承诺。

## 9. Settings

### 9.1 用户契约

```text
plugins settings list [<name>] [--scope user|workspace]
plugins settings set <name> <setting> [--scope user|workspace]
plugins settings remove <name> <setting> [--scope user|workspace]
```

set 的值继续只允许 Shell TTY no-echo 或受控 stdin，不进入 argv、日志、Transcript 或 response。Shell CLI 不发送 package digest、declaration digest、env var 或 expected store revision。

### 9.2 Host 解析与并发

- Host 在 scope lock 内按插件名称解析唯一当前 artifact，再按 setting name 解析当前声明。
- env var、内部 plugin id、声明摘要、credential account 和当前 index revision 均由 Host 补全并在提交前锁内复核。
- 并发变化由 Host 自己重试一次安全重读；仍冲突时返回 `PLUGIN_OPERATION_CONFLICT`，用户只需重试原命令，不需要先 list 并复制 revision。
- workspace setting 覆盖 user setting；删除 workspace 值后恢复继承 user 值。
- secret backend、journal、tombstone、无明文 fallback、process-control env denylist 和 child-only 注入边界保持不变。

### 9.3 更新后的设置继承

- 同一插件名称、同一 setting name 且 env var 不变时，update 后继续使用现有值；package digest 或 Adapter revision 变化不再单独使它 stale。
- setting 被删除时停止注入并进入现有安全清理流程。
- setting name 或 env var 改变时不猜测迁移，返回 `PLUGIN_SETTING_RECONFIGURE_REQUIRED` warning；旧值不注入到新声明。
- remove 清理该插件全部已登记 user/workspace Settings；部分清理失败时 artifact 不被静默删除，继续使用现有可重试 uninstall journal。

## 10. 公开 Protocol 契约

canonical Protocol 从 v3.7 增量升级；具体 minor 由实施时 schema 当前值决定。公开请求使用以下语义形状，字段命名按 schema snake_case：

```text
plugins.list       { scope?: user|workspace, include_disabled?: boolean }
plugins.inspect    { name, scope?: user|workspace }
plugins.validate   { source, format?: auto|... }               # 高级诊断
plugins.install    { source, scope?: user|workspace }
plugins.update     { name, source?: string }
plugins.set_enabled { name, scope?: user|workspace, enabled }
plugins.remove     { name, purge_data?: boolean }

settings.list      { name?: string, scope?: user|workspace }
settings.set       { name, setting, scope?: user|workspace, value }
settings.remove    { name, setting, scope?: user|workspace }
```

mutation result 至少包含：

```text
name
operation
status: loaded | disabled | warning | failed
scope?                    # 仅 scope-aware 操作
components[]              # 真实加载组件摘要
warnings[]
```

必须从 schema/generated/Host/CLI 一并删除或停止公开：

- Plugin mutation 的 `id`、`capability_fingerprint`。
- Plugin summary 的 `trusted_capability_fingerprint`、`can_enable`、compatibility/authorization 状态。
- Settings mutation 的 `plugin_id`、`package_digest`、`declaration_digest`、`env_var`、`expected_store_revision`。
- `skills.list`、`agents.list`、`mcp.status` 等响应的 `static_preview`。
- 要求当前运行 Host 热刷新的 `runtime_generation` 或等价承诺。

内部日志可以记录脱敏 internal ID、generation 和错误码；不得记录 secret、插件正文、绝对 store 路径或来源中的凭据。

## 11. TUI 与 Web 边界

TUI 与 Web 不提供 Plugin 安装、更新、启停、卸载、Settings 或 consent UI，也不直接读取 `~/.harness/plugins`。两端只消费 Host 启动时发布的现有 runtime catalogs：

- Plugin Commands 进入现有 Slash Command Registry 和补全。
- Plugin Skills 进入现有 SkillRegistry/Skill 选择器。
- Plugin Agents 进入现有 AgentCatalog/派发入口。
- Plugin MCP 进入现有 MCP 状态和工具列表。
- Context、Hook、LSP 和 Settings 环境由 Agent Host 装配，不在 UI 建立 Plugin 专用状态。
- 两端不因插件格式分支；Qwen、Claude、portable/Hybrid 的贡献在 canonical catalog 后表现一致。

第二轮只验证 TUI/Web 启动加载和实际使用没有回归。禁止借此新增 Plugin 管理面板、安装弹窗、secret 输入组件或外部 registry watcher。

## 12. Registry 迁移

现有 Plugin registry v2 升级为新版本时，在跨进程锁内执行一次、原子、幂等迁移：

1. 保留 artifact 定位、name、version、format、manifest、package digest、components、diagnostics、installed time 和 internal ID。
2. `enabled=true` 映射为 user activation enabled；`enabled=false` 映射为 user activation disabled。
3. 删除 `capability_fingerprint`、`trusted_capability_fingerprint` 的持久化和判定；不因两者不等阻止迁移后的加载。
4. Adapter report revision 不匹配时用当前 Adapter 重解析；解析结果决定 loaded/warning/failed，不产生 reauthorization。
5. Settings 依照第 9.3 节按名称、setting 和 env var 迁移，不再绑定 package digest 漂移。
6. replace 前任一步失败时保留旧 registry 原始 bytes，不产生半份新 schema；replace 已发生而目录 fsync 或 replace 结果无法确认时只返回 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN`，保留 backup 并尽力恢复 v2。POSIX 无法在此时证明旧文件的绝对 durability，因此规格不能承诺“任一步失败原文件不变”；该错误不得伪装成普通 `PLUGIN_REGISTRY_WRITE_FAILED`。

若旧 registry 存在大小写不敏感的同名多记录：

- 不随机选择、不同时启用。
- 这些记录进入 `PLUGIN_NAME_CONFLICT` 迁移诊断，artifact 保留且不进入 runtime。
- 高级 inspect 提供脱敏 source label/internal locator 供人工决定保留哪份；恢复命令不要求 fingerprint 或 digest。正常 name-based mutation 在冲突解除前失败关闭。

## 13. 错误与 warning 语义

| 代码 | 语义 | 是否已修改持久状态 |
| --- | --- | --- |
| `PLUGIN_ALREADY_INSTALLED` | 同名 artifact 已存在，应使用 update | 否 |
| `PLUGIN_NOT_FOUND` | 名称在安装库不存在 | 否 |
| `PLUGIN_NAME_CONFLICT` | 名称不唯一，禁止猜测 | 否 |
| `PLUGIN_SCOPE_INVALID` | scope 或 workspace binding 非法 | 否 |
| `PLUGIN_FORMAT_AMBIGUOUS` | 自动识别命中互斥格式 | 否 |
| `PLUGIN_FORMAT_UNSUPPORTED` | 没有可用 Adapter | 否 |
| `PLUGIN_CONSENT_REQUIRED` | 当前客户端不能完成确认 | 否 |
| `PLUGIN_OPERATION_CANCELLED` | 用户拒绝或取消确认 | 否 |
| `PLUGIN_SOURCE_UNAVAILABLE` | update 的本地来源不可用 | 否 |
| `PLUGIN_OPERATION_CONFLICT` | 内部 revision 并发冲突，重试原操作 | 否或保持上一完整提交 |
| `PLUGIN_LOAD_FAILED` | artifact/activation 已提交，但新 runtime 无法加载 | 是；状态为 failed |
| `PLUGIN_SETTING_RECONFIGURE_REQUIRED` | setting 声明变化，旧值不注入 | update 可已提交；作为 warning |
| `PLUGIN_REGISTRY_MIGRATION_BACKUP_FAILED` | v2 backup 写入失败，迁移未替换 registry | 否；可重试 |
| `PLUGIN_REGISTRY_MIGRATION_BACKUP_CONFLICT` | 已有 backup 与当前 v2 原文不同，拒绝覆盖 | 否 |
| `PLUGIN_REGISTRY_WRITE_FAILED` | registry temp 在 replace 前写入或 fsync 失败 | 否；可重试 |
| `PLUGIN_REGISTRY_COMMIT_UNCERTAIN` | replace 已发生但结果或目录持久化无法确认 | 结果不确定；保留 backup 并尽力恢复 |

原有 `PLUGIN_CAPABILITY_CONFIRMATION_REQUIRED`、`PLUGIN_REAUTHORIZATION_REQUIRED` 和 Settings 用户侧 revision/digest stale 错误退出正常产品流程；若旧 registry 解码需要识别，只能作为迁移输入，不能返回给新客户端要求用户处理。

## 14. Invariant

1. 用户完成任何正常 Plugin mutation 都不需要 internal ID、fingerprint、digest 或 revision。
2. 一个大小写不敏感名称最多对应一个可运行 artifact；歧义时 fail closed。
3. workspace activation/setting 覆盖 user；删除 workspace override 后恢复继承，不复制 artifact。
4. 未进入 canonical consumer 的组件不会出现在可用列表或模型工具描述中。
5. Adapter revision 变化只重新解析，不改变用户 activation，也不创造重新授权状态。
6. 每个 Harness 进程从启动 registry 构建不可变 runtime generation；外部 Shell mutation 不改写该进程正在进行的 Thread/Run。
7. Shell disable/remove 后新启动的 Harness 不能加载旧 Plugin；已经运行的进程不属于本任务的跨进程刷新范围。
8. Settings secret 永不进入 argv、registry、Protocol response、日志、Transcript、fixture 或前端持久状态。
9. Plugin 运行仍经过 Harness Tool Policy、审批、工作区和进程环境边界。
10. Plugin 管理只存在于 Shell CLI；TUI/Web 只消费同一 Host runtime catalog。

## 15. 两轮验收契约

本节定义验收分组，不替代 Plan：

### 第一轮：管理内核与 CLI

- registry/Settings 迁移、名称和 scope 模型、自动 Adapter、安装即启用、update/remove、四状态和 static preview 删除形成一条完整 Host/Protocol/Shell CLI 闭环。
- ZA38、Claude、portable/Hybrid 使用离线 fixture；安装来源与运行 workspace 分离验证。
- 第一轮结束统一评审，不在“删字段、改 schema、改 manager、改 CLI”等子步骤之间设置人工停点。

### 第二轮：启动加载与整体验收

- 分别从已安装 registry 启动 TUI 和 Web，验证 Plugin Commands、Skills、Agents、Context、MCP、Hook、LSP 和 Settings 进入既有运行入口。
- 完成跨端运行一致性、迁移、并发、取消、失败恢复、用户文档和 ZA38 手工验证说明。
- 第二轮结束做完整项目验收；不新增管理 UI，也不另拆 Web 补丁阶段。

## 16. 测试要求

- Adapter：自动识别、歧义、unsupported、局部坏组件 warning、Adapter revision 重解析。
- Store/registry：install 即启用、user/workspace precedence、同名冲突、v2 幂等迁移、迁移失败原子回退、内部 digest tamper 校验仍有效。
- Runtime：新进程读取最新 activation、启动 generation 不变、外部 mutation 不改写当前会话、disable/remove 后的新进程不加载旧插件、单插件失败隔离。
- Settings：name/key/scope API、Host 内部并发复核、继承、声明不变保留、声明变化 reconfigure、卸载清理、secret redaction。
- Protocol：schema 生成、Python/TypeScript 双端契约、旧字段拒绝、新 minor capability gate。
- CLI：无需 format/fingerprint/digest/revision/internal ID；consent accept/cancel；源码目录外安装后跨 workspace 加载。
- TUI/Web：启动后从同一 canonical catalog 使用 Plugin Commands、Skills、Agents、MCP 等现有界面，不出现 Plugin 管理入口或格式专用分支。
- 回归：ZA38 Commands/Skills/Agents/Context/MCP/Hook/LSP/Settings、Claude、portable/Hybrid，以及既有 Plugin Policy/approval/workspace boundary。
- 自动化仅使用临时 home、fixture、fake subprocess/credential backend；不得启动真实 ZA38 MCP/Hook/LSP、模型、网络或读取真实凭据。

## 17. 非范围

- Marketplace、Git/npm/URL 安装、远程搜索、自动更新服务和发布签名。
- link 模式与源码实时监听。
- 对话中的 `/extensions install` 等 Plugin 管理 Slash Command、TUI/Web 管理面板，以及运行进程监听外部 Shell mutation。
- Channels 或没有 canonical consumer 的新组件 runtime。
- 取消 Harness 的事务 store、路径校验、Run generation、Tool Policy、审批、工作区或秘密保护。
- 删除高级 validate/inspect；它们只退出正常用户必经路径。

## 18. 第一轮实现同步（2026-09-02）

第一轮按本规格落地到 registry v3、PluginManager、Host/Settings wrapper、Protocol v3.8 和 Shell CLI：Plugin 以名称和 scope 操作，安装确认后直接写入 activation；workspace override 使用独立 binding digest；update/remove 保留 artifact 级语义；Plugin 专用 fingerprint、authorization/reauthorization 和 static preview 不再进入产品摘要。`interaction.plugin_consent` 只由 Shell install/update 使用，并在提交前绑定预览时的 package identity。

当前已验证的第一轮离线 focused 证据包括管理/迁移故障注入、Adapter revision refresh、Qwen/Claude/portable/Hybrid 闭环、Plugin fixtures、Host/Settings、Qwen/MCP/runtime/full-demo 和 CLI/Protocol contract；第一轮生成文件、typecheck 和 diff check 均通过。replace 后结果或目录 durability 无法确认的迁移路径按主任务已接受的 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN` 语义处理，不宣称旧文件绝对不变。Qwen/runtime 集合中的 bare-node Phase3 失败是主任务已确认的既有基线失败；`project:check` 仍被 HC-151 过期复核日期阻塞。

## 19. 第二轮实现同步（2026-09-02）

第二轮将 Plugin registry v3 接入既有 Host 启动边界：Host 第一次资源初始化按当前 workspace 生成一次 `ExtensionCatalogSnapshot`，从同一 snapshot 派生 Skills、Commands、Agents、Context、Settings、MCP、Hook、LSP 和 Monitor；后续普通 Skill 控制面刷新不重新读取 Plugin registry。Shell 的外部 enable/disable/update/remove 不改写已运行 Host/Thread/Run，下一次 Host 才读取新的 activation 或 artifact。

TUI 与 Web 继续使用同一 CLI `CommandRegistry`/`InteractiveController` 和 Host startup catalog，不增加 Plugin 管理 UI、secret 控件、格式专用分支、registry watcher 或当前会话 Context 重注入。disabled/failed Plugin 不进入 consumer，warning 只保留实际有效组件之外的脱敏诊断，格式差异止于 Adapter。

新增离线启动回归覆盖 TUI/Web catalog 一致性、Qwen/Claude/portable/Hybrid 四格式、disabled/failed/warning 门禁、update/remove 旧 Host snapshot 与新 Host 可见性，以及 user/workspace identity。启动集成测试 `10 passed`；与管理、Plugin refresh、fixtures 合并 `118 passed`；CLI/TUI/Web/Protocol focused Bun `141 passed`；Python Protocol contract `13 passed`。Qwen/runtime 集合 `250 passed, 1 failed` 的唯一失败为主仓库基线 bare-node Phase3；`project:check` 唯一阻塞为 HC-151 复核日期。自动化仍只使用临时 home、离线 fixture、fake backend/process。
