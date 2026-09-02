# HC-166 对齐 Qwen 插件管理执行清单

关联：[Task](../task/archive/HC-166-对齐Qwen插件管理.md) · [Spec](../spec/HC-166-对齐Qwen插件管理.md) · [Plan](../plan/HC-166-对齐Qwen插件管理.md)

执行代理按本清单 TDD，不修改产品决策。只有两个联合验收点；轮内完成所有条目再汇报，不按文件或字段要求主任务逐项验收。

## 执行约束

- [x] 开始前读取仓库 `AGENTS.md`、HC-166 Task/Spec/Plan/Todo、相关架构和 `tmp/handoff.md`。
- [x] 记录当前 HEAD、branch、`git status --short --branch`、staged/unstaged/untracked 基线；保护用户已有修改。
- [x] 不执行真实模型、网络、ZA38 MCP/Hook/LSP、真实 credential 读取；使用临时 home、fixture、fake process/backend。
- [x] 不执行 git add/commit/push/reset/checkout/clean；没有主任务明确指令不得进入下一轮。
- [x] 不新增 TUI/Web Plugin 管理 UI、对话内安装、外部 registry watcher或当前会话 Context 重注入。
- [x] 不删除非 Plugin 领域的模型/策略/context fingerprint。

## 第一轮：管理内核与 Shell CLI

### Red tests

- [x] Registry v3：名称大小写唯一、user/workspace activation、workspace precedence、workspace install 的 user-disabled + workspace-enabled。
- [x] v2 migration：enabled/disabled 映射、fingerprint 丢弃、Adapter 重解析、durable backup、幂等、replace 前写故障保持 v2 原文；replace 后目录 fsync/replace 结果无法确认时按主任务已接受的 `PLUGIN_REGISTRY_COMMIT_UNCERTAIN` 语义处理，保留 backup 并尽力恢复；同名冲突 fail closed。
- [x] Protocol 新 minor：name/scope params、plugins.update、plugin consent、Settings 简化、旧 fingerprint/digest/revision/static_preview 字段拒绝。
- [x] Manager：install 确认后直接 enabled；name-based list/inspect/enable/disable/update/remove；auto Adapter；四产品状态。
- [x] Settings：name/key/scope 解析、Host 内部 CAS、workspace 覆盖、update 声明不变 rebind、声明变化 reconfigure、remove 全 scope cleanup。
- [x] CLI：grammar、TTY consent accept/cancel、非交互 consent-required、Settings no-echo/stdin、输出不含内部操作参数。

### Implementation

- [x] 删除 Plugin model/store/manager 中 capability/trusted fingerprint 与 authorization/reauthorization 门禁；保留内部 package digest/revision/snapshot。
- [x] 实现 registry v3、Plugin activation 和名称索引；安全迁移 v2。
- [x] 更新 Adapter/Manager 状态投影，删除独立 static preview 与用户兼容矩阵。
- [x] 更新 Host handlers 和 Settings wrapper，使用户 API 只接收 name/key/scope/value，内部完成 binding/CAS。
- [x] 更新 canonical schema 并运行生成器；同步 Agent/CLI generated/runtime validation。
- [x] 更新 Shell CLI parse、dispatch、help、consent 与结果呈现；新增 update。
- [x] 清除用户提示中的 fingerprint、digest、revision、内部 ID 和“先 inspect 再 enable”流程。

### 第一轮验收

- [x] Qwen、Claude、portable/Hybrid 离线 fixtures 都通过同一 install→enabled→name mutation 流程；参数化闭环测试 4 passed。
- [x] 临时 home 的 user plugin 在另一个 workspace 创建的新 Host 可加载；显式 workspace plugin 只在绑定 workspace 的新 Host 加载；Host scope 闭环测试 3 passed。
- [x] v2 registry/Settings 迁移、故障恢复、并发冲突和 name conflict 回归已覆盖并通过 26 个 HC-166 migration/management tests；replace 后 commit-uncertain 无法保证旧文件 durability，已按主任务接受的设计记录，未宣称旧文件绝对不变。
- [x] `static_preview` 不再出现在 Plugin/Skill/Agent/MCP Protocol response 或 CLI 输出。
- [x] `protocol:generate`、`protocol:check`、Python Protocol tests、focused Agent tests、CLI tests、`typecheck`、`git diff --check` 通过（第一轮 Python Protocol 13 passed、CLI/Protocol Bun 69 passed、生成/check/typecheck/diff 通过；project:check 被无关 HC-151 复核日期阻塞，Qwen 集合另有 1 个既有 bare-node Phase3 基线失败）。
- [x] 更新 HC-166 文档链和 `tmp/handoff.md`，写明命令、计数、diff、未验证项；停止等待主任务验收。

## 第二轮：启动加载与整体验收

前置条件：第一轮已经由主任务明确验收通过。

### Startup integration

- [x] 新 TUI Host 从 registry v3 按当前 workspace 加载 Plugin Commands、Skills、Agents、Context、MCP、Hook、LSP、Settings。
- [x] 新 Web Host 从同一 registry/catalog 获得相同能力；仅做共享消费者必要适配，不新增 UI 组件。
- [x] 外部 Shell mutation 不修改已经运行的 Host/Thread/Run；关闭并新启动后才读取新 activation。
- [x] disable/failed Plugin 不进入任何 consumer；warning 只暴露实际成功组件。
- [x] Qwen、Claude、portable/Hybrid 格式差异保持在 Adapter，UI 与 canonical consumer 不按格式分支。

### Documentation and regression

- [x] 更新 `docs/user/插件管理.md`：Shell 安装/管理后启动 Harness；移除 fingerprint、digest、必须 format、static preview 和对话内管理描述。
- [x] 更新插件架构与架构总览：名称/activation/registry v3/启动 generation；删除旧 trust/reauthorization 结论。
- [x] 回归 HC-157/HC-158、full-demo、Plugin runtime、Settings、Host、Protocol、CLI/TUI/Web；唯一 bare-node Phase3 失败与 HC-151 project check 阻塞单独记录。
- [x] 用真实 ZA38 路径只做 copy-on-install 与新进程启动手工说明；不自动运行真实外部组件。

### 第二轮验收

- [x] 离线 fixture 已证明从插件目录外安装后，新 TUI Host 可见并使用已支持组件；真实 ZA38 路径保留给 handoff 手工验收。
- [x] Web 启动获得与 TUI 相同 catalog；无 Plugin 管理入口、安装弹窗、secret 控件或 format 专用 UI。
- [x] Shell disable/update/remove 后新 Host 状态正确，旧 Host 保持启动 snapshot 且不做 Context 重注入。
- [x] 相关 Agent/CLI/TUI/Web/Protocol 测试通过；项目级检查的唯一阻塞为无关 HC-151，既有 bare-node 失败为仓库基线并已分开记录。
- [x] Task/Spec/Plan/Todo、用户文档、架构文档和 `tmp/handoff.md` 同步；停止等待用户真实测试，不提交、不推送。
