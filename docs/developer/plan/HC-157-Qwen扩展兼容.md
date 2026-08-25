# HC-157 Qwen 扩展兼容实施计划

关联 [Task](../task/HC-157-Qwen扩展兼容.md) 与 [Spec](../spec/HC-157-Qwen扩展兼容.md)。

## 阶段顺序

### 阶段一：格式识别与统一静态 Adapter（本轮）

1. 在 canonical Protocol schema 中增加 `qwen-code`，运行项目生成脚本同步 TypeScript、Python、validator、fixture 和 schema digest。
2. 在 CLI 参数解析中接受 `--format qwen-code`，保留显式格式优先和错误提示。
3. 新增 Qwen/DevAgent 静态 Adapter，统一解析两个清单名、身份字段、包内路径和静态组件库存；`qwen-extension.json` 缺少组件路径时使用存在的根目录 `commands/`、`skills/`、`agents/`，缺省或空数组 `contextFileName` 使用实际存在的 `QWEN.md`；`devagent-extension.json` 保持显式字符串路径语义。
4. 在 Adapter 入口实现清单冲突矩阵：Qwen 家族互斥、Qwen 与 portable/Claude 互斥、现有 portable+Claude Hybrid 保持不变。
5. 以清洁 fixture 覆盖 ZA38 数量、默认目录、Context 默认/数组、显式缺失与越界、unsupported 字段、坏 JSON、身份和越界路径；对 MCP command、Hook handler 和单文件 Skill 做畸形负向校验；锁定 Agent frontmatter 的 `color`/`approvalMode`、Hook matcher 和 MCP placeholder。先运行失败测试，再实现最小行为。

**可演示停点：**

```bash
harness plugins validate <clean-za38-fixture>
```

用户应看到 `format: qwen-code`，以及 `commands=3`、`skills=1`、`agents=3`、`mcp=1`、`contexts=1`、`hooks=1` 和包含这些能力的 fingerprint。此时不能继续阶段二。

### 阶段二：静态组件到只读资源快照

- 复用 Adapter 输出的相对来源，从已安装内容寻址 store 建立 `PluginResourceSnapshot`；快照捕获 Skill
  目录下的 `SKILL.md`、根 `references/`、根 `scripts/`、根 `mcp/` 等受控普通文件，保留真实包内
  目录结构并与源目录解耦；不把伪造的 `skills/<name>/references/` 当作根依赖。
- 统一输出 `/.harness/plugins/<plugin-id>/...` 虚拟路径和脱敏资源摘要；绝不把 store 绝对路径写进
  `plugins.list/inspect/install` 结果、诊断或资源 metadata。
- 只在 Command 内容、MCP `command`/`args`/`cwd`/`url` 和 Hook handler `command` 等明确字段中解析
  `<extensionPath>`、`${extensionPath}`、`${extensionPath}${/}` 和 `${CLAUDE_PLUGIN_ROOT}`；每个
  文件目标必须位于允许闭包且已存在于快照，未知字段不做字符串替换，嵌入式越界/未知 token/缺失
  目标 fail closed。
- Commands、Skills、Agents、MCP、Context、Hook 都进入静态快照；MCP/Hook 标记不可运行，Qwen 资产
  不进入现有 Agent/MCP/Hook runtime consumer。
- 复用现有列表链路增加独立静态 preview：它只筛选 `format: "qwen-code"` 且没有任何
  `effective=true` 组件的 Qwen/DevAgent 记录；portable、Claude、Hybrid 不重复投影。`plugins.list.static_preview` 分区返回 Commands、Skills、
  Agents、MCP，`initialize.static_command_preview`、`skills.list.static_preview`、
  `agents.list.static_preview`、`mcp.status.static_preview` 分别供对应列表消费者使用；所有条目
  `disabled/static/non-runnable/read-only`，不写入 effective catalog。

**可演示停点：**

```bash
harness plugins install <clean-za38-fixture>
harness plugins list
```

用户应在安装结果/`plugins.list.resource_snapshots` 中看到 ZA38 的 `commands=3`、`skills=1`、
`agents=3`、`mcp=1`、`contexts=1`、`hooks=1`，并在 `plugins.list.static_preview` 及
`initialize`、`skills.list`、`agents.list`、`mcp.status` 的对应静态分区看到 Commands 3、Skill 1、
Agents 3、MCP 1。所有 preview 都是 disabled/static/non-runnable/read-only，来源只使用
`/.harness/plugins/<plugin-id>/...`；安装未 trust，不能启动任何进程。根 `references/`、`scripts/`、
`mcp/` 的依赖可通过快照读取，Skill 的 `../../references/...` 不能越界。
对已有 portable、Claude、Hybrid fixture，四个对应 preview 分区应为空，且其原有 runtime catalog
和列表结果不重复、不改变。

### 阶段三：Context 与 Agent 权限

- 先在失败测试中锁定：trusted+enabled Qwen Context 才能进入主 Agent 与对应 Plugin Agent 的 Run snapshot；
  重复输入只注入一次，坏 UTF-8/超大/篡改/未信任均失败关闭；恶意参考文本不能改变 Core Policy、Sandbox 或工具边界。
- 扩展既有 `ContextLifecycle.prepare` 的稳定参考块输入，复用 `ContextBlock`、`RunContextSnapshot`、持久化和 projector 链路；不新增 Prompt/Context runtime。
- 让 Qwen Agent Markdown 走专用 frontmatter parser，再转换为 canonical `AgentDefinition` 与
  `ExecutionPolicyDefinition`。覆盖 `name`、`description`、`color`、`approvalMode`，并按 Qwen
  `agent-frontmatter-schema.ts` 独立解析 `permissionMode`：`default/plan/acceptEdits/auto/dontAsk`
  分别映射为 `default/plan/auto-edit/auto-edit/default`；只接受源码可证明的 tools/disallowedTools/model/
  runConfig 子集，未知、类型错误、bypass 类模式或两个模式字段冲突在 validate/install 前成为 invalid。
- 让已校验的三个 ZA38 executor 进入既有 `AgentCatalog`、`agents.list` 和 `resolve_plugin_agent_spec`；
  Agent 请求、父 Agent 当前 policy 与 Host/workspace policy 只做交集。auto-edit 不能授予 bypass、写入、
  Shell、网络或 delegation；未请求能力默认关闭。
- 把阶段二 static preview 过滤从整包改为组件粒度：effective Agents 从 preview 消失，Commands/Skills/MCP
  仍保留；portable、Claude、Hybrid 既有 runtime 与 preview 均不变。

**可演示停点：**

```text
trust + enable clean ZA38 fixture
  → agents.list 显示 3 个 effective executor
  → 主 Agent / 对应 Plugin Agent 的 Context snapshot 各有 1 个 DEVAGENT reference
  → 受限父/Host policy 下 fake/offline read-only runner 可读，写/Shell/网络/delegation 均被拒绝
  → Commands 3、Skill 1、MCP 1 static preview 保留，Agents preview 为空
```

到达该停点后只更新 Todo 与 `tmp/handoff.md`，等待用户验收，不进入阶段四。

### 阶段四：SubagentStop 与子 Agent 交互

- 复用既有 `HookDefinition`/`HookRunner` 的受控进程与 matcher seam；Qwen `SubagentStop` 在
  enabled+trusted、Agent matcher 命中且 Managed child 最终输出返回父 Agent 前执行。Hook 输入只
  包含 agent 身份、最后输出、Harness 虚拟 workspace 和 `stop_hook_active`，输出/超时/取消/异常
  全部有界并失败关闭。
- 抽取 Adapter/runtime 共用的 Hook 纯校验 seam，统一 matcher、command、timeout、shell、async
  和 Qwen `env`/`args` 受控字段边界；安装报告只有在 canonical runtime 必然可构造时才标记
  `adapted/effective`。已安装报告与包内容漂移、转换失败或目标定义为空时，runtime catalog
  记录 `HookRuntimeFailure`，Host 对匹配 child 建立可观察的 fail-closed gate，不允许无 gate 放行。
- 在 `ManagedAgentExecutor` 增加最终输出 gate：allow 返回结果；submit 注入明确的“执行提交门禁所需
  提交”指令，continue 注入 reason/additionalContext，二者都在同一 checkpoint 的下一模型回合继续；
  skip 只放行当前 gate，不创建第二个 child execution，提交后的下一回合仍重新运行 SubagentStop，
  连续八次后下一次 block 返回稳定上限错误。
- 在 `RunCoordinator` 复用父 Run 的 question InteractionPort，并由 Run 级
  `ChildInteractionRegistry` 登记原 `execution_id`、`parent_execution_id`、Agent ID 与 checkpoint；
  覆盖 submit、continue、skip、取消、超时、无客户端、无效响应、Run 终态和 owner 断连清理。
- Qwen Hook 接入后只把 Hook component 改为 `adapted/effective`；Commands、Skills、MCP preview 和
  portable/Claude/Hybrid runtime 行为保持不变，MCP 仍不连接，Hook 脚本测试只使用 fake runner。
- 阶段四完成前先闭合前阶段 control-plane 契约：`agentSummary` 的 `color`、`approval_mode`、
  `permission_mode` 从 canonical schema 生成到 Python/TypeScript validator、Host/CLI consumer 和
  contract fixture；`mcp.status` 的 `static_preview` 作为正式空数组结果保留。用真实 Host dispatch 的
  Qwen `agents.list` 回归证明新增字段不会在 control-plane 被拒绝，畸形 handler 后下一请求仍可处理。

**可演示停点：**

```text
trusted + enabled clean ZA38 executor
  → SubagentStop matcher 命中并阻断
  → 父 Run question 显示「提交 / 继续修改 / 一次性跳过」
  → 选择继续修改或提交都在同一 child execution/checkpoint 开始下一回合；选择 skip 才直接返回
  → 第九次连续阻断和异常/无交互客户端均失败关闭
```

### 阶段五：验收收口与文档

- 使用清洁打包 fixture 做目录/ZIP 复验，补用户插件管理文档、兼容矩阵和扩展架构说明。
- 运行 Agent focused、CLI、Protocol、typecheck、test、project checks；记录 sandbox/loopback 限制。
- 任务 review 后才能完成归档；本轮不执行归档命令、不提交、不推送。

## 设计约束

- 生产 Adapter 只出现在 `packages/agent/harness_agent/plugins/`；CLI 只负责参数和 Protocol 请求，Protocol 只维护 canonical schema。
- 阶段一/二的 Qwen 静态报告和只读快照保持不变；阶段三仅把已严格校验的 Agents 设为
  `adapted/effective: true`，Context 通过 trusted snapshot 输入 canonical lifecycle，Commands/Skills/MCP
  仍不进入运行时。
- Qwen Agent 的 capability 只是请求上限；最终权限必须是 Plugin request、父 Agent 和 Host/workspace
  边界的交集。任何未知权限模式、trust/fingerprint 变化均 fail closed。
- 不为旧内部接口增加 alias、双写或 fallback；portable、Claude、Hybrid 不因 Qwen 接入而改写其运行路径。
- 所有测试 fixture 离线、无真实 API key、无网络 MCP、无真实 `.env*`。

## 回滚

阶段一可回滚 `qwen-code` schema enum、CLI 分支、Qwen Adapter 和对应测试/文档。阶段二只增加基于
已安装内容的内存资源快照，不改变 registry schema；移除快照入口即可回滚，不需要数据迁移。
