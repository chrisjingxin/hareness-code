---
id: HC-166
title: 对齐Qwen插件管理
feature_area: Plugin 管理与加载
parent_task: -
decomposed_by: 历史未记录
priority: P0
status: 已完成
owner: 未认领
branch: -
reviewed_at: 2026-09-02
review_due: -
scope: 将 Harness 的 Shell CLI Plugin 安装、启停、更新、卸载、Settings 和 Adapter 识别改为接近 Qwen Code 的名称加作用域模型；安装后由后续启动的 TUI/Web 统一加载，移除用户侧能力指纹、授权哈希、digest/CAS 参数、静态假预览和过度兼容状态机，同时保留多格式 Adapter、事务安装、路径安全及 Harness 原有运行权限边界。
acceptance: Plugin 通过 Shell CLI 安装一次后可在所选 user/workspace scope 自动加载且无需再次提供源目录；所有管理操作使用插件名称而非 capability fingerprint、package/declaration digest、store revision 或内部 ID；格式默认自动识别；安装确认成功后直接启用，后续启动的 TUI 与 Web 从同一 canonical catalog 使用真实 Commands、Skills、Agents、Context、MCP、Hook 和 LSP，不新增对话内安装或 Plugin 管理 UI；列表只呈现真实已加载组件、禁用状态、警告或失败，不再暴露 static preview 和兼容矩阵；旧 registry 可迁移且 ZA38、Claude、portable Plugin 运行能力不回退。
user_docs: docs/user/插件管理.md
developer_docs: docs/developer/spec/HC-166-对齐Qwen插件管理.md、docs/developer/plan/HC-166-对齐Qwen插件管理.md、docs/developer/todo/HC-166-对齐Qwen插件管理.md、docs/developer/architecture/扩展与插件机制设计方案.md
test_evidence: 用户在主仓库真实安装/更新 ZA38 Extension、启动 Harness 并验证插件命令可用；管理与启动集成 118 passed，Host/Settings 128 passed，Qwen/MCP/runtime 250 passed（另 1 个既有 bare-node 基线失败），CLI/TUI/Web/Protocol 141 passed，Python Protocol 13 passed，typecheck、protocol:check、git diff --check 通过
references: HC-157、HC-158；master 本次提交
completed_at: 2026-09-02
---

# HC-166 对齐 Qwen 插件管理

## 背景与问题

HC-157、HC-158 已让 Qwen/DevAgent Extension 的 Commands、Skills、Agents、Context、MCP、Hook、LSP 和 Settings 接入 Harness canonical runtime，但管理面仍沿用 Harness 自增的能力授权模型：安装后默认停用、enable 要复制 `capability_fingerprint`，Adapter 变化可触发重新授权，Settings 要求用户提交 digest/CAS，列表还区分兼容矩阵和不可运行的 `static_preview`。

这些机制使 ZA38 虽然已经能够运行，却仍不像 Qwen Code 那样“安装一次、按名称管理、在任意工作区按作用域加载”。本任务统一重做 Plugin 管理契约；不是继续增加 Qwen 专用旁路。

## 用户最终得到什么

- `plugins install <source> [--scope user|workspace]` 自动识别 Qwen、Claude 或 portable 格式，展示将安装的实际组件、设置和警告；用户确认后完成安装并在该作用域启用。
- user scope 的插件以后从任意项目启动 Harness 都可用；workspace scope 只在绑定的工作区可用。正常启动只传当前工作区，不再传插件源码目录。
- Shell CLI 的 `list / enable / disable / update / remove / settings` 均以人类可读插件名称和作用域操作；内部稳定 ID 可以保留，但不作为日常输入。
- 安装、更新或启停完成后，后续启动的 Harness 进程直接读取新状态；本任务不监听另一个 Shell 进程的 registry 变化，也不动态改写正在运行的会话。
- TUI 与 Web 启动时使用同一套 Host/Protocol catalog；Plugin Command、Skill、Agent、MCP 等进入现有界面，不新增 Plugin 管理面板。

## 必须修改的产品契约

| 当前 Harness 机制 | 本任务目标 |
| --- | --- |
| Plugin `capability_fingerprint`、`trusted_capability_fingerprint` | 从 Plugin model、store、Protocol、CLI/TUI/Web 和授权流程删除；不得误删模型/执行策略中同名但与 Plugin 无关的 fingerprint。 |
| `authorization-required`、`reauthorization-required` | 删除持久化授权哈希与重新授权状态；安装/更新仅做当次内容展示和确认。 |
| 安装后默认 disabled，再复制 fingerprint enable | 确认安装后在所选 scope 直接启用；enable 只需要名称与 scope。 |
| Adapter revision 改变导致重新授权 | 当前 loader 重新解析已安装包；仅 Adapter 代码变化不要求用户重新授权。加载失败或能力不支持时给出警告/失败。 |
| 用户操作 `local-xxx/插件名` | 用户操作使用插件名称；安装库中的名称必须唯一，内部 ID 只用于持久化和跨进程关联。 |
| Settings 要求 package/declaration digest 和 expected store revision | 用户只提供插件名、setting 名、scope 和受控输入值；并发控制、声明绑定及 secret metadata 若仍需要，由 Host 内部处理，不作为用户参数。 |
| `ready/partial`、`supported/adapted/unsupported/invalid`、`effective/can_enable` | 产品状态收敛为“已加载、已禁用、加载警告、加载失败”；Adapter 可保留内部诊断，但不得形成用户必须理解的授权状态机。 |
| 独立 `static_preview` | 删除。只有真实进入 canonical consumer 的组件出现在组件列表；未支持或坏条目显示警告，不伪装成可选命令、Agent、Skill 或 MCP。 |
| 用户可见 package/declaration digest、store revision | 全部从正常 CLI/Protocol/UI 输入与提示移除。内部内容校验、事务 generation 或迁移标识可保留，但不得成为授权或日常操作凭据。 |
| 修改只对 `next_host` 生效 | 明确采用 Shell 管理后再启动 Harness 的流程；下一次启动直接加载新状态，不建设跨进程热刷新或对话中途 Context 重注入。 |
| 正常安装必须 `--format qwen-code` | 默认依据 manifest 和 converter 自动识别；显式 format 最多保留为开发诊断/歧义处理选项，不进入正常教程。 |
| validate/inspect 是正常必经流程 | 可保留高级诊断命令，但 install/update 自己完成必要校验和展示。 |

## 保留的内部边界

- 保留统一 Adapter → canonical runtime 架构，不为 Qwen 建第二套命令、Agent、MCP、Hook、LSP 或 Settings 执行循环。
- 保留安全的 staging、事务提交、锁、generation/recovery、归档与 symlink/越界检查，以及必要的内部稳定 ID。
- `PluginResourceSnapshot`、内容摘要和 `/.harness/plugins/...` 虚拟资源是否继续作为内部实现，由 Spec 按最小改动决定；它们不得再成为用户授权、用户输入或 UI 概念，安装后的行为必须等价于从受管安装副本加载。
- 保留 Harness 既有 Tool Policy、审批、工作区写权限、进程环境过滤和秘密存储边界。简化 Plugin 管理不等于插件可绕过运行权限。
- ZA38 当前未声明 Channels，本任务不新增 Channel runtime。

## 数据迁移与兼容要求

- 旧 registry 升级必须保留已安装包和明确的 enabled/disabled 选择；旧 fingerprint、trusted fingerprint、兼容状态和用户侧 digest/CAS 字段不再参与新状态判断。replace 前迁移故障必须保留 v2 原文；replace 已发生但目录 fsync 失败时只能返回 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN` 并尽力恢复，不能声称在技术上保证原文件绝对不变。
- 已有 Settings 在能够由“插件名称 + scope + setting 声明”唯一匹配时继续可用；冲突或损坏记录只报告可操作的迁移警告，不静默绑定到另一个插件。
- 同一安装库的名称冲突时拒绝安装或要求显式 update，不允许 CLI 随机选择内部 ID。
- Qwen、Claude、portable/Hybrid 共用同一管理流程；格式差异只留在 Adapter 内。

## 两轮交付约束

后续 Spec/Plan/Todo 只能组织为两轮，每轮一个联合验收点，不再按字段、组件或前端拆成很多停点：

1. **管理内核与 Shell CLI**：一次安装并启用、user/workspace scope、名称操作、自动 Adapter 识别、状态简化、旧数据迁移和 Settings 简化一并完成；用 ZA38、Claude、portable fixture 做 CLI/Host/Protocol 离线闭环。
2. **启动加载与整体验收**：验证后续启动的 TUI、Web 从同一 canonical catalog 使用所有已支持组件，不新增管理 UI；完成跨端运行回归、文档和真实 ZA38 手工验收说明。

第一轮完成后统一验收再进入第二轮；轮内不设置额外人工停点。不得为了赶进度暂时保留 fingerprint 作为兼容必填参数；第二轮只验证两个运行界面，不扩展为对话内 Plugin 管理功能。

## 可观察验收

- [x] 在插件源码目录外执行一次 ZA38 安装并确认后，从另一个项目启动 Harness，无需插件源码 `--cwd`、`--format`、fingerprint 或内部 ID，`/za38-*`、Agent、Skill、MCP 等已支持组件按 scope 可用。
- [x] install/update 的确认页只展示名称、版本、来源、真实可加载组件、Settings 和警告；取消不改变现有安装，确认后无需再执行 enable。
- [x] Shell CLI 能按名称和 scope 完成 list/enable/disable/update/remove/settings，且不要求用户输入任何 digest、revision 或授权哈希。
- [x] Shell mutation 完成后启动新的 TUI 或 Web Host，可以观察新状态；已经运行的 Host 不要求跨进程热刷新，也不做对话中途 Context 重注入。
- [x] 列表只出现“已加载、已禁用、加载警告、加载失败”，不存在 `static_preview` 假组件和 capability 授权状态。
- [x] Adapter 更新只影响下一次 Host 启动时的重新解析；不会仅因 Adapter revision 改变要求重新授权。
- [x] 旧 registry/Settings 离线迁移后，已有 ZA38 启停状态和可唯一匹配的配置保留；冲突、损坏和名称歧义有稳定错误且不误绑。
- [x] Qwen、Claude、portable/Hybrid 的 focused tests，Python/TypeScript Protocol 契约、CLI/TUI/Web 测试、`protocol:generate`、`protocol:check`、`typecheck` 和项目检查通过；不使用真实凭据、网络 MCP/Hook/LSP 或模型完成自动化验收。

## 非范围

- Marketplace、远程 Git/npm 搜索与下载安装、自动更新服务、插件发布签名。
- TUI/Web 内安装、更新、启停、卸载或 Settings 管理，以及运行中监听外部 Shell mutation。
- 新增 Channels 或没有 canonical consumer 的新组件运行时。
- 放宽插件运行时的 Tool Policy、审批、工作区、进程或秘密安全边界。
- 把高级 `validate/inspect` 删除；它们只需退出正常必经路径。

## 第一轮执行记录（2026-09-02）

第一轮“管理内核与 Shell CLI”已由主任务正式验收通过。本轮完成 registry v3、user/workspace activation、名称操作、自动 Adapter、四种产品状态、Settings 内部 binding/CAS、Plugin consent、Protocol v3.8、Shell CLI grammar 和生成文件同步，并补齐 v2 迁移故障注入。v2 migration 的 replace 前故障保持 v2 bytes；replace 后结果或目录 durability 无法确认时返回 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN`，保留 backup 并尽力恢复，该 POSIX 限制已由主任务接受。

基线为 HEAD `c2871031ac411a52c0e1ebaa508e3c17e326747f`、detached HEAD、无 staged/tracked 修改；基线已有本 HC-166 四份过程文档未跟踪文件。本轮只在这些文档、两端 Plugin 管理实现、Protocol 生成物、架构文档和对应测试范围内修改，未执行 git add/commit/push/reset/checkout/clean。

自动化均使用临时 home、离线 fixture、fake credential backend/fake process；没有真实模型、网络、凭据或 ZA38 MCP/Hook/LSP 外部执行。使用主仓库 `/Users/beichen/Desktop/大模型/github projects/harness-code/packages/agent/.venv/bin/python` 完成 Host/Settings 和 Qwen/runtime 集合；Qwen 集合唯一失败是主任务已指出的 bare-node Phase3 既有基线用例，未归因 HC-166。`bun run project:check` 的唯一阻塞是既有无关任务 HC-151 的复核日期 `2026-08-30`，本轮未修改该任务规避检查。

## 第二轮执行记录（2026-09-02）

第二轮完成启动 catalog 与既有 consumer 的纵向接入：Host 只在第一次资源初始化时从当前 workspace 读取 registry v3，并从同一不可变 snapshot 派生 Commands、Skills、Agents、Context、MCP、Hook、LSP、Monitor 和 Settings；普通 Skill 控制面刷新不会重新读取 Plugin registry。CLI 的 TUI 与 Web 仍通过同一 InteractiveController/Host 入口消费 `initialize.agent_commands`，没有新增 Plugin 管理 UI、watcher 或当前会话 Context 重注入。

新增离线启动集成测试覆盖 TUI/Web catalog 一致性、Qwen/Claude/portable/Hybrid 四种格式、disabled/failed/warning consumer 门禁、update/remove 的旧 Host snapshot 与新 Host 可见性，以及 user scope 缺失 workspace 和 workspace override identity。完整启动集成为 `10 passed`；与管理、refresh、fixtures 合并运行共 `118 passed`。相关 Host/Settings、CLI/TUI/Web、Protocol 回归见本 Task `test_evidence` 和 Plan/Todo；唯一代码集合失败是主仓库基线的 bare-node Phase3，项目检查唯一阻塞仍为 HC-151。

真实 ZA38、模型、网络、凭据和外部 MCP/Hook/LSP 未自动执行；可复制的人工终端路径已写入 `docs/user/插件管理.md` 与 `tmp/handoff.md`。用户已在主仓库完成真实 ZA38 安装、更新、启动与命令调用测试并确认无误；第二轮联合验收通过。
