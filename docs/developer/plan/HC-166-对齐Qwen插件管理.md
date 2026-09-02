# HC-166 对齐 Qwen 插件管理实施计划

关联：[Task](../task/archive/HC-166-对齐Qwen插件管理.md) · [Spec](../spec/HC-166-对齐Qwen插件管理.md) · [Todo](../todo/HC-166-对齐Qwen插件管理.md)

本 Plan 是实现设计，执行代理只负责按设计 TDD 落地，不重新决定产品范围。开发严格分两轮，每轮只有一个联合验收点；第一轮通过主任务验收后才能进入第二轮。

## 1. 目标架构

```text
Shell CLI plugins install/update/enable/disable/remove/settings
  → canonical Protocol v3 新 minor
  → AgentHost Plugin management handler
  → PluginManager + PluginStore registry v3
      ├─ 受管 artifact（内部 content digest）
      ├─ 名称唯一身份（用户入口）
      ├─ user activation
      └─ workspace activation override
  → 管理进程返回并退出

下一次启动 Harness
  → PluginManager 按当前 workspace 解析 effective activation
  → 当前 Adapter 重建 canonical Plugin catalog
  → Commands / Skills / Agents / Context / MCP / Hook / LSP / Settings
  → 现有 TUI 或 Web 表现层
```

没有对话内安装、TUI/Web 管理 UI、外部 registry watcher、当前 Thread Context 重注入或跨进程热刷新。

## 2. 实现时不得重开的决定

1. 正常管理入口只用插件名称；internal ID、digest、revision 不进入用户参数。
2. 安装确认后在选定 scope 直接 enabled，不再紧跟一次 enable。
3. 安装 artifact 全局按名称大小写不敏感唯一；workspace scope 只是 activation override，不复制 package。
4. user activation 是缺省值，workspace override 优先；workspace install 写 user disabled + 当前 workspace enabled。
5. update/remove 是 artifact 级操作，不接受 scope；enable/disable/settings 是 scope-aware 操作。
6. Plugin 专用 capability/trusted fingerprint 和 authorization/reauthorization 状态全部删除；模型/执行策略领域的同名 fingerprint 不动。
7. Adapter 内部可以保留逐组件严格诊断，但产品只返回 loaded/disabled/warning/failed。
8. `static_preview` 删除；没有进入 canonical consumer 的条目只成为 Plugin warning。
9. 内容摘要、原子 registry revision、锁、staging、路径校验、`PluginResourceSnapshot` 和 Run generation 保留为内部机制。
10. mutation 只对后续启动的 Harness 生效；不修改已经运行的 TUI/Web Host。
11. 管理 consent 只由 Shell CLI 处理；TUI/Web 不注册 Plugin 管理 interaction。
12. 所有格式共用管理面：Qwen、Claude、Agent Plugins 1.0、Hybrid；每个 Adapter 仍严格保持原格式 schema 和运行语义。

## 3. 数据设计

### 3.1 Registry v3

`packages/agent/harness_agent/plugins/store.py` 将 `REGISTRY_VERSION` 升为 3。推荐记录形状：

```text
registry:
  version: 3
  revision: integer
  plugins: InstalledPlugin[]

InstalledPlugin:
  id                       # 仅内部
  source_id/source_label   # 仅内部/高级诊断
  name/version/description/format/manifest
  package_digest           # 仅内部完整性
  components/diagnostics   # Adapter 内部事实
  activation:
    user: enabled | disabled
    workspaces:
      <workspace_binding_digest>: enabled | disabled
  installed_at_ms
  adapter_revision
  origin                   # 本地 update 所需，wire 只返回脱敏 label
```

不再持久化：

- `capability_fingerprint`
- `trusted_capability_fingerprint`
- 单一 `enabled`
- authorization/compatibility 的派生缓存

`workspace_binding_digest` 使用 Plugin 专用 domain 对 canonical workspace identity 做本地 hash；不得复用 Settings credential policy version 作为 activation 身份，也不得把绝对 workspace 路径写入 registry/wire。可抽取小型共享 path identity helper，但禁止让 PluginStore 依赖整个 SettingsStore。

### 3.2 名称索引

- registry 每次读取和 mutation 都建立 `casefold(name) → record` 索引。
- 新安装、更新、名称查询和迁移使用同一函数，禁止 CLI 与 Host 分别实现大小写规则。
- `plugin_id` 继续绑定 artifact 和旧 Settings/日志，不作为查找 API。
- 正常响应展示 manifest name；高级诊断可以返回 internal locator，但 mutation 不接受 locator 代替名称。

### 3.3 产品状态投影

新增一个纯函数，把 effective activation 与 Adapter 结果映射为：

```text
disabled  effective activation=false
loaded    enabled 且所有可声明组件均进入 consumer
warning   enabled 且至少一个组件进入 consumer，同时存在跳过/失败诊断
failed    enabled 且没有组件进入 consumer，或关键 catalog 构造失败
```

`PluginComponentReport.status/effective/can_enable` 若为降低第一轮风险暂留，只能在 Agent 内部使用；Protocol summary、CLI 输出、启动提示和操作门禁不能包含它们。

### 3.4 v2 → v3 迁移

迁移由 PluginStore 在首次需要 registry 时，通过同一跨进程 lock 执行：

1. 锁内重读原始 bytes，严格按 v2 schema 解码，不用 v3 宽松兼容读取。
2. 校验每个 artifact 的安全字段和 package 位置；不执行插件进程。
3. `enabled=true/false` 映射到 user activation；丢弃两个 fingerprint 字段。
4. 用当前 Adapter 重解析 components/diagnostics/adapter revision；Adapter 变化不产生重新授权。
5. 先原子写 `registry.v2.backup.json` 并 fsync，再写 v3 temp、fsync、atomic replace、目录 fsync。
6. replace 前任一步失败时保持 v2 原文件 bytes 不变；replace 已发生但随后无法确认目录持久化时返回 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN`，并尽力恢复 v2 bytes。POSIX 无法在目录 fsync 失败后证明 rename 的持久性，因此该路径不能承诺“任一步失败原文件绝对不变”；backup 保留且下次启动必须先报告/重试，不得把不确定提交伪装成普通写失败。
7. 大小写不敏感同名记录不进入 runtime、不随机选择；返回 `PLUGIN_NAME_CONFLICT`，保留 artifact 和 v2 backup，advanced inspect 给出脱敏 source label/internal locator。冲突恢复不要求 fingerprint/digest。

迁移测试必须覆盖重复执行、写入故障、截断 v2、包缺失、同名冲突和 backup 已存在。

## 4. Protocol 与 Shell CLI 设计

### 4.1 Protocol

在 `packages/protocol/schema/v3.json` 基于实施时当前 minor 增加一个 minor，并通过生成器同步 TS/Python。修改或新增：

```text
plugins.list        { scope?, include_disabled? }
plugins.inspect     { name, scope? }
plugins.validate    { source, format? }            # 高级诊断
plugins.install     { source, scope? }
plugins.update      { name, source? }
plugins.set_enabled { name, scope?, enabled }
plugins.remove      { name, purge_data? }

settings.list       { name?, scope? }
settings.set        { name, setting, scope?, value }
settings.remove     { name, setting, scope? }
```

增加结构化 `interaction.plugin_consent`，内容为 install/update preview，response 仅为 accept/cancel。该 handle 只由对应 Shell 管理命令声明。

删除或停止公开：

- Plugin params/summary 中的 user-facing `id`、capability/trusted fingerprint、authorization、compatibility、can_enable。
- Settings params/summary 中的 plugin_id、package/declaration digest、env_var、expected store revision。
- Skills/Agents/MCP 等 response 的 `static_preview`。
- mutation 的 runtime_generation、effective_on/next_host 提示。

所有改变 shape 的 operation 绑定新 minor；旧 minor 不发送新形状，也不能由 Host 猜测字段别名。

### 4.2 Shell grammar

`packages/cli/src/args.ts` 与 `index.ts` 固定为：

```text
harness plugins install <source> [--scope user|workspace]
harness plugins list [--scope user|workspace] [--enabled-only]
harness plugins inspect <name> [--scope user|workspace]
harness plugins enable <name> [--scope user|workspace]
harness plugins disable <name> [--scope user|workspace]
harness plugins update <name> [--source path-or-zip]
harness plugins remove <name> [--purge-data]
harness plugins settings list [<name>] [--scope user|workspace]
harness plugins settings set <name> <setting> [--scope user|workspace]
harness plugins settings remove <name> <setting> [--scope user|workspace]
```

- scope 缺省 user；workspace scope 使用 `--workspace/--cwd` 解析出的当前 workspace，两个别名冲突继续拒绝。
- 正常 install/update 不输出或要求 `--format`；开发诊断的显式 format 只留给 validate/inspect。
- install/update 必须在 TTY 显示 preview 并确认；非交互返回 `PLUGIN_CONSENT_REQUIRED`，本任务不加 `--yes`。
- Settings value 继续由 TTY no-echo 或受控 stdin 读取，不进 argv。
- 输出 JSON/文本只包含 name、version、scope、status、真实组件、warnings 和结果；不得生成“复制 fingerprint/digest 后继续”的指令。

### 4.3 Host 内部并发

- Handler 在 registry/scope lock 内按名称解析当前 record，并在写入前重读。
- Settings Handler 从当前 Plugin declaration 补齐 internal ID、package/declaration digest、env var 与当前 store revision；调用现有安全 SettingsStore 时把 CAS 留在 Host 内。
- 内部 CAS 冲突可安全重读一次；仍冲突返回 `PLUGIN_OPERATION_CONFLICT`，用户重试原命令。
- update 时声明 name+env var 未变，内部事务把 credential binding 重绑到新 package；声明变化返回 reconfigure warning，不注入旧值。
- remove 在删除 artifact 前完成所有已登记 user/workspace Settings 清理；partial cleanup 保留现有可重试 journal，不能先宣称 removed。

## 5. 第一轮：管理内核与 Shell CLI

### 5.1 目标文件

- `packages/agent/harness_agent/plugins/model.py`
- `packages/agent/harness_agent/plugins/store.py`
- `packages/agent/harness_agent/plugins/manager.py`
- `packages/agent/harness_agent/plugins/adapters.py` 及格式 Adapter（只做必要返回形状调整）
- `packages/agent/harness_agent/plugins/resources.py`
- `packages/agent/harness_agent/config/settings.py`
- `packages/agent/harness_agent/host/agent_host.py`
- `packages/protocol/schema/v3.json` 与生成文件
- `packages/cli/src/args.ts`
- `packages/cli/src/index.ts`
- `packages/cli/src/ipc/client.ts`（Shell consent interaction）
- 对应 Agent/Protocol/CLI tests

### 5.2 TDD 顺序

1. 先写 v3 registry、v2 migration、名称唯一和 activation precedence 失败测试。
2. 再写新 Protocol params/result/consent 与旧字段拒绝测试，运行生成器形成 red boundary。
3. 写 Manager install 即启用、name-based mutation、auto Adapter、四状态和 static preview 删除测试。
4. 写 Settings name/key/scope、内部 CAS、update rebind 和 uninstall cleanup 测试。
5. 写 CLI parse→dispatch→fake Agent consent accept/cancel、无 fingerprint/digest/revision 和跨 workspace 测试。
6. 实现最小代码直到上述测试 green；不得先改文档掩盖未实现行为。

### 5.3 第一轮验收标准

全部满足才允许主任务验收：

- [x] ZA38 在临时 home 中以 `plugins install <source>` 自动识别并一次确认后显示 enabled；不再执行第二条 enable。
- [x] list/inspect/enable/disable/update/remove/settings 只使用名称和 scope；CLI help、错误和响应中没有 capability fingerprint、digest、expected revision 或内部 ID 操作指引。
- [x] user plugin 可从另一个 workspace 的新 Host 加载；workspace plugin 只在绑定 workspace 的新 Host 加载。
- [x] v2 enabled/disabled 与 Settings 可迁移；replace 前故障保持 v2 bytes；replace 后结果或目录 durability 无法确认时按主任务已接受的 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN` 语义 fail closed；同名冲突 fail closed。
- [x] Adapter revision 变化重新解析但不 reauthorize；Qwen、Claude、portable/Hybrid 共用管理测试。
- [x] static preview 不再进入 Plugin/Skill/Agent/MCP response；坏或不支持组件只成为 warning。
- [x] Settings secret 与内部 binding 不进入 stdout、Protocol response、registry、日志、Transcript 或 fixture。
- [x] focused Agent + Python Protocol + CLI 测试通过；`protocol:generate`、`protocol:check`、`typecheck`、`git diff --check` 通过。
- [x] 不启动真实 ZA38 MCP/Hook/LSP、模型、网络或读取真实凭据。

### 5.4 第一轮停点

执行代理更新 Task/Spec/Plan/Todo 和 `tmp/handoff.md`，报告真实命令、计数、未验证项与 diff；然后停止。不得进入第二轮，不得 git add/commit/push/reset/checkout/clean。

### 5.5 第一轮执行证据（2026-09-02）

第一轮实现已由主任务正式验收通过：registry v3/v2 migration、activation/name mutation、四状态与 static preview 删除、Settings 内部 binding/CAS、Host consent、Protocol v3.8 生成物和 Shell CLI grammar 已同步。replace 后只能证明结果不确定并返回 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN`，保留 backup 并尽力恢复；这不是“任一步失败旧文件绝对不变”的承诺，主任务已接受该设计。第一轮管理/迁移与 Plugin/refresh/fixtures 为 108 passed，Host server/Settings RPC/config 为 128 passed，Qwen/MCP/Phase3/runtime/full-demo 为 187 passed；CLI/Protocol focused Bun 为 69 passed，Python Protocol contract 为 13 passed；`bun run protocol:generate`、`bun run protocol:check`、`bun run typecheck` 和 `git diff --check` 通过。

上述第一轮记录中的 bare-node Phase3 失败是主任务确认的仓库基线，不归因 HC-166；`bun run project:check` 的 HC-151 阻塞也与本任务无关。自动化没有真实模型、网络、凭据或 ZA38 MCP/Hook/LSP 外部执行。

## 6. 第二轮：启动加载与整体验收

第二轮不开发 Plugin 管理 UI，只验证新 registry/Protocol 对既有运行入口的影响并做必要的共享消费者适配。

### 6.1 目标文件

- `packages/agent/harness_agent/host/agent_host.py` 启动 catalog 装配
- `packages/agent/harness_agent/plugins/runtime.py`、MCP/LSP/Context/Agent/Skill consumers（仅兼容新 model/status）
- `packages/cli/src/interactive/runtime.ts`、catalog/command binding 相关共享代码（仅启动加载适配）
- TUI/Web 既有 catalog/presentation tests；不新增管理 component
- `docs/user/插件管理.md`
- `docs/developer/architecture/扩展与插件机制设计方案.md`
- `docs/developer/architecture/架构总览.md`
- HC-166 文档链与 `tmp/handoff.md`

### 6.2 验证闭环

1. 在临时 home Shell 安装 fixture，关闭管理 sidecar。
2. 分别启动新的 fake TUI Host 与 fake Web Host，绑定不同 workspace。
3. 断言 Commands、Skills、Agents、Context、MCP、Hook、LSP、Settings 只按 effective scope 进入既有 consumer。
4. 运行中从另一个 Shell 修改 registry，断言当前 Host 不被改写；关闭后新启动 Host 才看到变化。
5. 真实 ZA38 只做本地只读 install + 新进程启动的手工步骤；自动化不启动真实外部组件。
6. 删除文档中的 fingerprint、reauthorization、static preview、必须 `--format`、对话内管理和 next-host 热刷新旧教程。

### 6.3 第二轮验收标准

- [x] 离线 fake TUI Host 从非插件源码 workspace 的 registry v3 读取 `/za38-*`、Plugin Skill/Agent 等已支持组件，并走既有运行链。
- [x] 同一安装的离线 fake Web Host 得到相同 canonical catalog；没有 Qwen/Claude/portable 专用 UI 分支。
- [x] TUI/Web 没有新增 Plugin 管理入口、安装弹窗、Settings secret 控件或 registry watcher。
- [x] Shell disable/update/remove 后的新 Host 状态正确；已经运行的 Host 保持启动 snapshot，Context 不被中途重注入。
- [x] Claude、portable/Hybrid、full-demo 和 HC-157/HC-158 runtime 回归通过；唯一 bare-node Phase3 失败为主仓库基线。
- [x] 用户文档只给出“Shell 安装/管理 → 启动 Harness → 使用”的路径；架构文档已同步启动 generation、名称/activation/registry v3 和旧状态边界。
- [x] Agent、CLI、TUI、Web、Protocol 相关集合通过；`project:check` 的唯一阻塞为无关 HC-151，已单独记录。
- [x] 用户可以按 handoff 中的 ZA38 手工命令完成最终验收；本轮未代替用户执行真实外部组件。

### 6.4 最终停点

第二轮完成后已停止等待用户测试；用户在主仓库完成真实 ZA38 验收并确认通过后，现已进入提交和归档流程。

### 6.5 第二轮执行证据（2026-09-02）

新增 `packages/agent/tests/test_hc166_startup_integration.py`：`10 passed`。与 `test_hc166_plugin_management_red.py`、Plugin/refresh/fixtures 集合合并为 `118 passed`；Host server/Settings RPC/config `128 passed`；Qwen/MCP/Phase3/runtime/full-demo 集合 `250 passed, 1 failed`，唯一失败为主仓库已确认的 `test_qwen_bare_node_hook_and_lsp_are_frozen_without_inheriting_path` 基线失败。CLI/TUI/Web/Protocol focused Bun `141 pass, 0 fail`；Python Protocol contract `13 passed`。

自动化使用临时 HOME、临时 workspace、仓库离线 fixture、fake credential backend 和 fake MCP connection；没有真实模型、网络、凭据或外部 Hook/LSP/MCP 进程。`protocol:generate`、`protocol:check`、`typecheck`、`git diff --check` 均通过；`bun run project:check` 仍被 HC-151 复核日期 `2026-08-30` 阻塞。真实 ZA38 手工路径只写入用户文档和 handoff，未自动执行。

## 7. 风险与处理

| 风险 | 处理 |
| --- | --- |
| 删除 Plugin fingerprint 时误删模型/策略 fingerprint | 只删除 `harness_agent.plugins`、Plugin Protocol/CLI 字段；对 threads/runtime_state 做负向回归，确认未改 |
| v2 迁移破坏现有 ZA38 | 原始 bytes 锁内严格解析，先 durable backup 后 atomic v3；replace 前故障注入验证原文件不变，replace 后目录 fsync/commit-uncertain 路径显式报告并尽力恢复 v2，不能伪称具有不可证明的 durability |
| workspace activation 与 Settings scope 不一致 | 两者使用相同 canonical workspace 输入，但各自 domain-separated digest；在 Host 一次解析 workspace |
| name-based 操作误命中同名插件 | casefold 唯一索引；安装/迁移/查询共享；歧义 fail closed |
| 去掉用户 CAS 导致并发覆盖 | CAS 保留在 Host/Store 内，锁内重读并最多安全重试一次 |
| Adapter 诊断简化后坏组件误入 runtime | 产品状态简化不放宽 Adapter/consumer gate；有效组件清单由实际 consumer 构造结果生成 |
| static preview 删除造成 TUI/Web 类型回归 | 第一轮生成协议，第二轮只做共享消费者机械适配和既有界面回归，不建管理 UI |
| Shell 安装后旧 Host仍可使用 | 明确是本任务语义；文档说明新进程生效，不增加 watcher 或当前会话重注入 |
| Settings update 错绑 credential | 只在 name + setting + env var 一致时内部 rebind；其他变化 warning + 不注入 |

## 8. 回滚

- 第一轮尚未通过时，可按完整 HC-166 diff 回滚到 registry v2；不得删除用户 store。
- v3 migration 保留 `registry.v2.backup.json`，回滚工具只恢复 schema，不执行 Plugin 进程或迁移 secret。
- Protocol 与生成文件必须作为同一变更回滚，禁止只回滚 TS 或 Python 一端。
- 第二轮仅做启动 consumer 适配和文档，不引入独立持久格式；可与第一轮一起回滚。
