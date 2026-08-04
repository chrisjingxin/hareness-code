# Harness Code 与 Deep Agents Code 的 Python Agent 架构评估

> 历史资料：本文冻结于 2026-07-28，仅用于追溯当时架构对比，不作为当前实现依据。
> 评估日期：2026-07-28  
> Harness Code：`9dbc5deda02cee996ce3d19c4faf2fb4629e22ce`  
> Deep Agents Code（下文简称 dcode）：`4338671aa1d9bd1fd70f20069aac5468697606bf`

> 历史快照：本文中的任务排序和待办判断只对应上述评估提交；当前任务顺序以[架构重构计划](../../architecture/架构重构计划.md)和[任务看板](../../tasks/任务看板.md)为准。

## 结论先行

两者都以 Deep Agents 和 LangGraph 为底座，但已经走向两种不同的产品架构：

- **Harness Code 更像“企业 Agent Host”**：先确定本次运行使用的模型、工具、Skill、执行环境和安全策略，再复用匹配的 Agent 图；同时把运行事实、上下文压缩产物和线程状态持久化。它更重视隔离、可追溯和长生命周期运行。
- **dcode 更像“功能完整的个人 Coding Agent”**：一个进程复用一张 Agent 图，通过 middleware 在每次调用时切换模型和行为，并集成大量现成功能。它更重视开箱即用、模型与沙箱生态、可扩展 Agent、MCP、记忆和可观测性。

两者都没有重新实现 ReAct 循环。最终都是由 `create_deep_agent()` 组织 middleware，再委托 LangChain/LangGraph 执行。真正的区别在于：Harness 在图外增加了更完整的 Host、AgentEngine 和持久化领域；dcode 则在图内外装配了更多产品 middleware 和生态适配。

站在第三方视角，没有脱离场景的绝对赢家：

| 判断维度 | 更占优势的一方 | 核心原因 |
| --- | --- | --- |
| 企业运行隔离、审计和资源治理 | Harness Code | 有 AgentEngineProfile、AgentEnginePool、不可变 Run binding、项目隔离的持久化 |
| 功能完整度和生态覆盖 | dcode | 多模型、多沙箱、自定义 Agent、成熟 MCP、hooks、tracing 已进入实际运行链 |
| 长会话上下文的持久可靠性 | Harness Code | 原文、摘要和压缩状态进入 ThreadPersistence，不依赖进程临时目录 |
| 动态能力和用户可定制性 | dcode | 文件化自定义 Agent、异步 Agent、可写记忆、Goal/Rubric、解释器 |
| 默认文件访问边界 | Harness Code | 文件工具限制在工作区；dcode 本地模式明确允许绝对路径和 `..` 越界 |
| 当前产品成熟度 | dcode | 已交付能力明显更多，边界场景和集成测试也更丰富 |
| 当前架构收敛度 | 两者各有问题 | Harness 仍有大 Server 和未接入设计；dcode 的主装配函数及兼容层已经很重 |

**综合建议：Harness 不应整体照搬 dcode。** 应保留自己的运行身份、项目隔离、只读 PromptEpoch、上下文持久化和 fail-closed 执行边界；选择性吸收 dcode 的 MCP 隔离、自定义 Agent 加载、provider 生态和脱敏 tracing。当前更紧急的是修复 Harness 已有运行链上的闭环问题，而不是继续增加 Goal、Rubric、解释器等新能力。

## 1. 评估范围与方法

本报告只比较 Python Agent 部分，包括：

- Agent 图如何创建和复用；
- 模型、middleware、工具、Skill 和子 Agent 如何装配；
- Thread、Run、上下文和记忆如何保存；
- 本地执行、远程沙箱、审批和文件边界；
- MCP、可观测性和扩展入口；
- 生命周期、并发和故障边界。

明确不比较：

- TUI 的布局、交互和命令体验；
- TypeScript 协议类型、JSON-RPC 方法和帧格式；
- 安装包、发布流程和品牌体验。

评估以两边当前源码和测试为准。Harness 的 ADR、重构计划和任务文档只用于解释设计方向，**没有进入生产调用链的代码不会按已交付能力计分**。例如，当前 `agent_catalog.py` 中的动态 Agent 目录并未被主运行链调用，不能据此认为 Harness 已支持可配置 Agent。[H-Task-085]

## 2. 两套架构用通俗语言怎么理解

### 2.1 Harness Code：先给运行环境做“身份证”，再复用

```text
一次 Run
  ↓
解析这次实际使用的模型
  ↓
计算 AgentEngineProfile
（项目 + 模型 + 工具 + Skill + MCP + 沙箱 + middleware + prompt 模板）
  ↓
AgentEnginePool 查找或构建对应 Agent 图
  ↓
从 ThreadPersistence 恢复该线程的 PromptEpoch 和 checkpoint
  ↓
创建只属于本次 Run 的 RunContext
  ↓
执行 Deep Agents 图
  ↓
记录运行事实、上下文产物和终态
```

这里最关键的不是“用了 AgentEnginePool”，而是 **Agent 图只保存可共享的执行能力，线程数据和本次运行数据不放在图对象里**。一张图可以服务多个 Thread；每次调用通过 `RunContext` 注入 `thread_id`、`run_id`、PromptEpoch、审批模式和取消令牌。[H-Server] [H-AgentEngine] [H-RunContext]

这个设计解决的是企业 Host 常见问题：

- 同一个进程同时服务多个线程时，不能串上下文；
- 用户切换模型后，要知道本次实际用了什么；
- 不同工具、Skill、MCP 或沙箱配置不能误复用同一张图；
- AgentEngine 不能无限增长，需要容量、空闲回收和关闭顺序。

### 2.2 dcode：一张功能丰富的图，通过运行上下文动态切换

```text
进程启动
  ↓
解析模型、MCP、沙箱和项目配置
  ↓
create_cli_agent() 一次性装配完整 middleware 和工具
  ↓
进程内缓存一张 Agent 图
  ↓
每次调用从 runtime context 读取模型、参数、线程等动态值
  ↓
执行同一张图，并把恢复需要的字段写入 checkpoint
```

dcode 的 `make_graph()` 在进程内通过锁只构建一次。之后的模型切换由 `ConfigurableModelMiddleware` 在每次模型调用前完成，不需要重新编译 Agent 图。[D-ServerGraph] [D-Model]

这个设计优先解决的是 Coding Agent 的易用性：

- 快速支持很多模型和 provider；
- 用户可以从文件系统添加 Agent 和 Skill；
- MCP、memory、Goal、Rubric、Web 工具、hooks、tracing 都装入同一条链；
- 本地与多种远程沙箱可以通过配置切换。

## 3. 核心差异总表

| 架构问题 | Harness Code | dcode | 第三方判断 |
| --- | --- | --- | --- |
| Agent 图复用 | 按 AgentEngineProfile 复用多张图，有容量和 TTL | 每个进程缓存一张图 | Harness 更适合多租户式 Host；dcode 更简单 |
| 运行时动态状态 | `RunContext` 明确承载，不进入共享图 | `CLIContextSchema` + checkpoint 私有 channel | 两者都避免重编译；Harness 的运行身份更完整 |
| 模型切换 | 先解析并固化本次 Run binding，再选 AgentEngine | middleware 在模型调用前动态替换 | Harness 更可审计；dcode 更灵活 |
| provider | 当前主要是 OpenAI-compatible | Anthropic、OpenAI、Google、Bedrock 等大量 provider | dcode 明显领先 |
| Thread 持久化 | 项目指纹隔离的 SQLite + LangGraph checkpoint | 全局 SQLite，通过 metadata 中的 cwd 查询 | Harness 隔离更强；dcode 查询和迁移经验更成熟 |
| 系统提示词 | 首次创建 PromptEpoch，线程内稳定 | 启动时生成，部分字段在 middleware 中动态改写 | Harness 更利于复现和 prompt cache |
| 长上下文 | 自定义分级压缩，原文和摘要持久化 | SDK summarization，外置内容在本地临时目录 | Harness 更耐进程重启；dcode 更贴近上游 |
| Memory | 只读快照，在线程生命周期内冻结 | 线程内也会缓存快照，但默认鼓励把经验写回 AGENTS.md 供新线程使用 | Harness 更可预测；dcode 更会“学习” |
| Skill | 注册表快照、稳定 ID、虚拟只读挂载 | 多目录动态加载，兼容 `.agents`、`.deepagents`、`.claude` | Harness 边界更严；dcode 生态更友好 |
| 子 Agent | 当前只有固定 `general-purpose` | 用户/项目自定义、独立模型、异步远程 Agent | dcode 明显领先 |
| MCP | 基础连接、添加、移除；运行图更新闭环不完整 | 独立会话管理、工具过滤、项目信任和失败隔离 | dcode 明显领先 |
| 本地文件工具 | 工作区边界 middleware + virtual root | `virtual_mode=False`，允许工作区外路径 | Harness 默认更安全 |
| Shell | 本地 shell 不是 OS 沙箱；最小环境、审批/白名单 | 本地 shell 也不是沙箱；环境更完整、审批/白名单 | Harness 暴露更少；dcode 兼容性更好 |
| 远程沙箱 | 明确 provider factory，创建失败不回退本地 | 多个现成 provider 和生命周期集成 | dcode 生态更强；两边都能 fail closed |
| 并发工具调用 | 有读写锁，但需审批的写工具绕过锁 | 主要沿用 ToolNode 并行和 HITL | Harness 有意治理，但当前仍有缺口 |
| 可观测性 | AgentEnginePool 诊断和结构化状态为主 | LangSmith tracing、秘密脱敏、hooks | dcode 明显领先 |
| 上游耦合 | 为替换 summarization 操作 Deep Agents 内部 profile | 多处兼容补丁、内部符号和 alpha 版本绑定 | 两者都有风险，dcode 接触面更大 |

## 4. 详细架构比较

### 4.1 图、AgentEngine 和生命周期

Harness 把“可以共用的 Agent 能力”定义为 AgentEngine。当前实现先把内置 `main` 解析成不可变 `ResolvedAgentSpec`，再由同一 spec 生成 `AgentEngineProfile` 并驱动 builder；Profile 包含项目、角色、实际模型、工具能力视图、Skill 快照、MCP、沙箱、审批策略、middleware 和 prompt 模板的指纹。`AgentEnginePool` 按这个 key 做 single-flight 构建、容量限制、空闲回收、draining 和关闭。[H-Profile] [H-AgentEngine]

优势：

- 配置不同的 Run 不会无意共享错误的图；
- AgentEngine 的资源可以被统一关闭；
- 可观察 MISSING、BUILDING、ACTIVE、IDLE、DRAINING 等状态；
- 同一配置下多个 Thread 仍能共享昂贵的图和模型客户端。

代价：

- “什么变化必须生成新 AgentEngine”变成关键正确性问题；
- Profile 指纹漏字段时，旧能力会被错误复用；
- AgentHost 仍需要协调配置、模型绑定、AgentEngine、ThreadPersistence 和运行生命周期；ZC-091 已把角色解析与构图输入收敛到 `ResolvedAgentSpec`，后续仍可继续拆出更深的领域 module。

dcode 选择每进程一张图。它用一个 `asyncio.Lock` 防止重复构建，并让动态模型 middleware 在调用时替换模型。[D-ServerGraph] [D-Model]

优势：

- 心智模型和资源生命周期更简单；
- 切模型不需要维护多张图；
- MCP 发现和沙箱创建不会在每个请求中重复执行。

代价：

- 配置变化通常需要重建进程或专门增加动态更新通道；
- 缺少 AgentEngine 级容量、驱逐和诊断；
- 越来越多的动态差异会进入 middleware 条件分支。

**判断：** 对单用户、单项目 CLI，dcode 的方案更省代码；对多连接、长驻、需要审计的企业 Host，Harness 的 Profile + Pool 更合适。但 Harness 必须把 Profile 完整性当成安全和正确性边界，而不只是缓存优化。

### 4.2 模型选择与运行事实

Harness 在 Run 开始前解析模型，优先级为：本次请求、最近一次 Run、旧绑定、配置默认。解析后把“用户请求了什么”和“实际执行用了什么”原子写入 ThreadPersistence；AgentEngineProfile 也使用实际模型生成。[H-Server] [H-Persistence]

这带来三个好处：

1. 一次 Run 中模型身份稳定；
2. 恢复和审计能回答“当时到底用了哪个模型”；
3. 模型不可用或选择冲突时倾向明确失败，而不是悄悄改变行为。

ZC-086 已将这套优先级、legacy 转换、脱敏和 Run binding 构造收进
`execution_binding.py` 的纯函数 interface。Server 只编排输入与输出，ThreadPersistence 只处理
SQLite/JSON adapter，AgentEngine 和历史事实消费同一解析结果。[H-Task-086]

dcode 支持 `provider:model` 和大量 provider。`ConfigurableModelMiddleware` 在每次模型调用前读取 runtime context；模型不同就创建对应实例，并把实际 model spec 和参数写入 checkpoint，恢复时继续使用。[D-Model] [D-Resume] [D-Config]

它的灵活性明显更强，但有两个取舍：

- 无效的运行时模型覆盖会记录异常，然后继续使用当前模型。这保证任务不中断，却会造成“用户以为已经切换，实际仍用旧模型”的语义风险；
- 跨 provider 切换需要清理 Anthropic 专属设置、调整缓存 key，并通过文本规则改写 system prompt 的 Model Identity 段，兼容逻辑更复杂。
- 主模型能动态切换，不代表所有辅助模型都同步切换。summarization 和默认 rubric 在建图时接收初始模型；切换主模型后，需要逐个确认这些组件的模型身份。

**判断：** dcode 赢在 provider 覆盖和动态性；Harness 赢在运行身份的一致性。企业场景中，Harness 的 fail-closed 更值得保留，不建议复制 dcode 的静默回退。

### 4.3 Prompt、Memory 和 Skill

Harness 首次运行 Thread 时创建 `PromptEpoch`，固定以下内容：

- system prompt；
- 当前工作区和执行方式；
- Skill 索引；
- 用户级和工作区级只读 Memory 快照；
- 工具、Skill、模板等指纹。

以后恢复 Thread 时直接读取旧 PromptEpoch，不重新读取工作区。这使同一 Thread 的规则不会因磁盘文件变化而悄悄漂移，也有利于 provider prompt cache。[H-Prompt] [H-RunContext]

dcode 的 MemoryMiddleware 会读取用户和项目 AGENTS.md。默认提示模型主动把经验写回文件，也可以切换为只读；同时用 ManagedMemoryGuard 保护机器维护的 onboarding 区块。读取结果会保存到 checkpoint 的 private state，因此同一个 Thread 通常也不会在每轮重新读取磁盘；它的“学习”主要通过写回文件影响新 Thread，而不是让当前 Thread 的 prompt 每轮变化。[D-Agent] [D-Memory]

两者代表不同价值取向：

| 问题 | Harness | dcode |
| --- | --- | --- |
| 线程运行是否可复现 | 强 | 中 |
| 能否跨线程自动学习 | 弱 | 强 |
| 规则变化何时生效 | 新 Thread/显式新 epoch | 通常是新 Thread 或显式刷新 |
| 记忆污染或错误沉淀风险 | 低 | 较高 |
| 用户维护成本 | 需要明确更新 | Agent 可主动更新 |

Skill 方面，Harness 先由 `SkillRegistry` 扫描并生成不可变快照，再把内容映射到 `/.harness/skills/<canonical-id>`。它处理来源优先级、稳定 ID、重复项、符号链接和路径穿越，适合作为受控能力目录。[H-Skills] [H-Agent]

dcode 直接支持 built-in、用户和项目的 `.deepagents`、`.agents`，并实验性兼容 `.claude` Skill 目录。它对用户迁移更友好，但装配逻辑需要兼容不同版本的 SkillsMiddleware 输入格式。[D-Agent]

**判断：** Harness 更适合“规则冻结后可复核”的企业任务；dcode 更适合“工具跟着用户工作方式持续生长”。可写记忆不是单向升级，Harness 不应默认引入，而应在未来作为显式、可审阅的模式提供。

### 4.4 子 Agent 和任务分解

Harness 当前生产链只注册固定的 `general-purpose` 子 Agent，并给它单独加 PlanMode 和工作区边界。仓库虽然存在约 843 行的 `agent_catalog.py`，但当前主入口没有加载它，动态 Agent 和 Plugin Agent 仍是计划能力。[H-Agent] [H-Task-085]

dcode 会从用户和项目目录加载 Agent 定义：

- 项目定义可以覆盖用户定义；
- 每个 Agent 有独立 prompt、description 和可选模型；
- 没有显式模型时继承当前运行模型；
- 可以加载异步远程 Agent；
- 子 Agent 也会获得模型切换、shell allow-list 和部分 memory 保护。

[D-Subagents] [D-Agent]

dcode 的同步子 Agent 是“一次任务一个隔离 worker”：不继承父 Agent 的消息和 private state，结束后主要把最终回答交回主 Agent，但与主 Agent 共享 backend，因此可以留下文件修改。dcode 加到主 Agent 的 Memory/Skills middleware 默认也不会自动继承给子 Agent。[D-Subagents] [D-Agent]

dcode 的优势是能力真实可用，适合把 review、research、test 等工作拆成专门角色。风险是自定义 Agent 文件本身成为新的信任输入，多个 worker 共享文件时可能竞争，而且主 Agent、子 Agent、异步 Agent 的工具和安全策略更难保持完全一致。

**判断：** dcode 明显领先。Harness 如果要补齐，应先删除未接入的 catalog，再从最小的“受信 Markdown 定义 → 校验 → 显式策略 → 装入图”开始，不应恢复一个尚无真实调用方的大型抽象层。

### 4.5 上下文窗口和长会话恢复

Harness 不使用 Deep Agents 默认 summarization，而是自建 `ContextWindowMiddleware`：

```text
低于 60%   → 只报告使用率
60%～80%   → 先把大的工具结果脱水
高于 80%   → 摘要旧消息，保留近期回合
高于 90%   → 更激进，只保留一个近期回合
```

原始工具结果、摘要、指针和压缩状态写入 ThreadPersistence；压缩收益不足 20% 时不盲目替换；还有溢出后单次恢复和连续失败熔断。[H-Context] [H-Persistence]

优势：

- Sidecar 重启后仍能追溯被压缩内容；
- 压缩策略、归档和状态在同一持久化边界内；
- 更容易解释为什么压缩、压掉了什么。

代价：

- 自己承担 token 估算、摘要质量、恢复和状态机维护；
- 为替换上游 summarization，Harness 暂时修改 Deep Agents 的进程级内部 profile 注册表，这是一个脆弱接缝。[H-Agent]

dcode 使用 Deep Agents 提供的 summarization tool middleware。它在本地模式下把 `/large_tool_results/` 和 `/conversation_history/` 路由到 `tempfile.mkdtemp()` 创建的目录，然后由 CompositeBackend 提供给 summarization 和 Rubric。[D-Agent]

优点是更贴近上游能力，功能迭代成本较低。缺点是这些外置原文没有接入 dcode 的 `sessions.db`；从源码可推断，进程退出或系统清理临时目录后，旧 checkpoint 中若保留这些路径，原文不一定还能取回。这里属于**高可信源码推断**，仍建议用“压缩后退出并恢复会话”的端到端测试验证。

**判断：** dcode 的实现更轻，Harness 的实现更适合长期审计和可靠恢复。Harness 应减少与上游内部 profile 的耦合，但不应退回临时目录持久化。

### 4.6 Thread、checkpoint 和项目隔离

Harness 的 ThreadPersistence 使用用户级 SQLite，但给 LangGraph checkpointer 强制加入项目指纹 namespace。它同时保存：

- Thread 索引；
- checkpoint；
- PromptEpoch；
- AgentEngineProfile；
- 每次 Run 的请求选择和实际执行绑定；
- 上下文原文、摘要和压缩状态。

数据库文件和数据目录还设置了受限权限。[H-Persistence]

dcode 也使用全局 SQLite checkpointer。Thread metadata 保存 cwd，并通过 cwd 精确过滤会话；源码为大型数据库增加了专门索引，注释中记录了约 12GB 数据库的现实性能问题，体现出较强的生产经验。[D-Sessions]

差别在于：

- Harness 把项目隔离放入 checkpoint namespace，是写入边界；
- dcode 主要通过 thread UUID 和 metadata 查询区分项目，是读取和发现边界。

**判断：** Harness 的默认隔离更强，尤其适合一个 Host 访问多个项目。dcode 在超大历史库的查询优化、迁移和兼容方面更成熟，值得借鉴其索引与升级测试，而不是照搬 namespace 设计。

### 4.7 工具、审批、工作区和沙箱

Harness 本地后端使用 `virtual_mode=True`，再通过 WorkspaceBoundaryMiddleware 校验文件路径和符号链接，主 Agent 和子 Agent 都使用这条边界。Shell 使用独立的审批/白名单逻辑，且默认不继承完整环境；远程沙箱创建失败时不回退本地。[H-Execution] [H-Workspace] [H-Approval]

需要明确：**工作区文件边界不等于操作系统沙箱。** Harness 自己的模块说明也限定它只保护 Deep Agents 文件工具，不保护 shell 和 MCP。允许 shell 后，命令仍可能访问工作区外资源，最终安全性取决于审批、allow-list、最小环境或远程沙箱。

dcode 本地文件和 shell 后端显式使用 `virtual_mode=False`。Deep Agents 对该参数的说明非常直接：绝对路径和 `..` 可以绕过 `root_dir`，Agent 可以访问工作区外文件。[D-Agent] [D-Filesystem]

dcode 这样做换来了更接近普通终端的能力，例如处理跨仓库文件、用户配置和全局工具；同时它通过以下措施降低风险：

- 对 shell、写文件、删除、Web 和子 Agent 等工具做 HITL；
- 支持 shell allow-list；
- Web fetch 有 SSRF 和 DNS 相关防护；
- MCP 项目配置需要信任；
- 支持多种远程沙箱；
- tracing 配置包含秘密脱敏。

[D-Agent] [D-Tools] [D-MCP] [D-Config]

**判断：** 如果默认目标是“只允许修改当前工作区”，Harness 更合理；如果目标是“像本机开发者一样操作所有环境”，dcode 更方便。企业产品不应复制 dcode 的 `virtual_mode=False` 默认值。

### 4.8 工具并发

Harness 增加了 `ConcurrencyGuardMiddleware`：

- 只读工具共享读锁，可以并行；
- 写工具和未知工具拿独占写锁；
- 但需要 HITL 的工具为了避免审批打包死锁，会跳过锁；
- 审批通过后，ToolNode 仍通过 `gather` 并行执行这些写操作。

[H-Concurrency]

最后一条意味着当前注释所说的“写操作不与任何操作并发”并不对所有路径成立。人工审批确认的是“是否允许”，并不能保证两个被允许的文件写入彼此没有竞态。这是 Harness 当前运行链上的真实架构缺口，而不是未来优化项。

dcode 主要沿用 ToolNode 并行和 HITL，没有建立相同的读写调度层。它更简单，但也没有给并行写提供强一致性承诺。

**判断：** Harness 的方向更好，当前实现却没有闭环。修复应放在“审批恢复后的实际执行入口”，而不是再增加一套未接入的 ToolBatch 抽象。

### 4.9 MCP

Harness 已有 MCP 连接管理、状态、添加和移除能力，工具在构建 Agent 图时注入。[H-MCP] [H-Server]

但源码显示两个需要优先处理的问题：

1. `mcp_config_fingerprint()` 只包含服务器名称和 transport，测试还明确要求忽略 command 和 URL。同名同 transport 的服务器即使端点改变，也会得到相同 AgentEngineProfile。
2. 添加或移除服务器会更新 McpConnectionManager 的工具列表，但已经构建的 Agent 图拿到的是原工具快照；处理函数没有让 AgentEnginePool 失效，也没有重建已有图。

因此可推断：MCP 状态可能已经显示“添加/移除成功”，但已缓存 AgentEngine 仍使用旧工具集合。第二点是**调用链推断**，目前测试覆盖了配置写入和 manager 行为，没有看到“热更新后新 Run 的图工具发生变化”的端到端测试。

dcode 的 MCP 实现更完整：

- 每个 server 有独立的惰性持久 session 和锁；
- 工具发现使用短生命周期 session；
- 支持 allowedTools、disabledTools；
- 项目 MCP 有信任边界；
- 有 OAuth 和自动发现；
- 多 server 预检和发现并行且有界；
- 单个 server 失败不必隐藏其他 server。

[D-MCP] [D-ServerGraph]

**判断：** dcode 明显领先。Harness 首先要修的是 Profile 指纹和 AgentEngine 失效闭环，再考虑增加 OAuth 或自动发现。

### 4.10 Goal、Rubric、解释器、hooks 和 tracing

dcode 还具备 Harness 当前没有的能力：

- GoalToolsMiddleware：把目标和约束作为 checkpoint 状态管理；
- ReliableRubricMiddleware：根据 rubric 做自评和重试；
- QuickJS 解释器及 PTC 工具桥；
- 外部 hooks；
- LangSmith tracing 和秘密脱敏。

[D-Goal] [D-Rubric] [D-Hooks] [D-Config] [D-Agent]

这些能力提高了长任务自治、调试和集成能力，也是 dcode 产品完整度领先的重要原因。但它们不是免费的：

- Rubric 可能增加模型调用、成本和延迟；
- Goal 增加另一套需要和用户请求、Todo、checkpoint 保持一致的状态；
- PTC 可以绕过常规 `interrupt_on`，需要独立的高风险开关；
- hooks 能启动外部进程，是新的执行边界；
- tracing 必须持续维护脱敏规则。

Harness 已有 AgentEnginePool diagnostics 和结构化运行状态，但 hooks、统一 tracing 和秘密脱敏仍主要是规划项。[H-Architecture] [H-Refactor]

**判断：** tracing 的价值高且边界清晰，应优先借鉴；Goal、Rubric 和解释器只有在明确产品需求出现时再引入。

### 4.11 对 Deep Agents 上游的耦合

两边都不是只调用公开工厂：

- Harness 为停用内置 summarization，临时操作 Deep Agents 的进程级内部 profile；
- dcode 会按 middleware 类名替换 TodoListMiddleware，还使用多处兼容检测、内部辅助函数，并固定到 `deepagents==0.7.0a7`。

[H-Agent] [D-Agent] [H-Package] [D-Package]

Harness 的耦合点较少，但其中一个涉及进程全局状态，故障影响大。dcode 的耦合面更广，不过它与上游同仓开发，能同步调整；Harness 作为下游项目不具备同样条件。

**判断：** Harness 应建立少量明确的 Deep Agents adapter 和升级契约测试，尤其覆盖 middleware 顺序、subagent middleware 是否独立、summarization 替换和 HITL 恢复，不要继续复制上游内部实现。

## 5. 第三方综合评分

评分使用 1～5 分，只表示本报告对应代码版本下的相对成熟度，不表示长期上限。

| 维度 | Harness | dcode | 说明 |
| --- | ---: | ---: | --- |
| AgentEngine 隔离与生命周期 | 5 | 3 | Harness 有明确 Profile、Pool、租约和关闭状态 |
| 运行审计与可复现性 | 5 | 3 | Harness 固化 Run binding 和 PromptEpoch |
| 模型/provider 覆盖 | 2 | 5 | dcode 的 provider 生态远超 Harness |
| 子 Agent 扩展 | 2 | 5 | Harness 生产链目前只有固定 Agent |
| Skill 管理边界 | 4 | 4 | Harness 更受控，dcode 更兼容 |
| 长上下文持久可靠性 | 5 | 3 | dcode 本地外置内容使用临时目录 |
| 自适应 Memory | 2 | 5 | dcode 可主动写回，Harness 有意只读 |
| Memory 可预测性 | 5 | 3 | 只读 epoch 更容易复核 |
| MCP 完整度 | 2 | 5 | Harness 还有 AgentEngine 更新闭环问题 |
| 默认本地文件边界 | 4 | 2 | 两边 shell 都不是沙箱；Harness 文件工具更严 |
| 远程沙箱生态 | 3 | 5 | dcode 有多个现成 provider |
| 可观测性与外部集成 | 2 | 5 | dcode 有 tracing、脱敏和 hooks |
| 架构内聚性 | 3 | 3 | Harness 有大 Server 和死代码；dcode 有大装配函数和大量条件分支 |
| 当前交付成熟度 | 3 | 5 | Harness 仍处源码开发期，dcode 能力覆盖更完整 |

不建议把分数相加。比如企业部署可能把“运行审计”和“工作区边界”看得比“自适应 Memory”重要得多；个人开发者的权重可能完全相反。

## 6. 分场景选择

### 更适合选择 Harness 架构思路的场景

- 一个常驻 Host 同时服务多个 Thread 或客户端；
- 需要记录每次 Run 的真实模型、配置和终态；
- 不同项目、工具和执行环境必须明确隔离；
- 上下文压缩后的原文仍要长期追溯；
- 默认要求 Agent 只能通过文件工具访问工作区；
- 配置错误时宁可拒绝运行，也不能静默降级。

### 更适合选择 dcode 架构思路的场景

- 个人开发者希望安装后立刻使用完整 Coding Agent；
- 经常在多个 provider 和模型间切换；
- 需要从 Markdown 快速增加专用 Agent；
- 高度依赖 MCP、Web、hooks、LangSmith 和远程沙箱；
- 希望 Agent 自动维护长期记忆；
- 能接受本地 Agent 具有接近开发者账号的文件访问能力。

## 7. Harness 当前最值得处理的问题

### P0：修已有运行链的正确性

1. **补齐 MCP → AgentEngine 的失效闭环**
   - 指纹应覆盖所有会改变工具行为的非秘密配置；秘密值可以先规范化再哈希，不应直接省略。
   - MCP 添加、删除或修改后，应明确关闭或淘汰受影响 AgentEngine。
   - 增加端到端测试：更新前后的新 Run 必须看到新工具集，删除的工具不能继续调用。

2. **修复审批写工具的并发缺口**
   - 审批阶段仍可并行收集 action；
   - 真正执行写操作时必须重新进入独占调度；
   - 用两个同时修改同一文件的回归测试证明执行顺序。

3. **完成 ZC-085 的死代码清理**
   - 删除没有生产调用方的 Unicode security 和未接入 ToolBatch；
   - Agent catalog 由 ZC-091/ZC-092 在真实角色 AgentEngine 与 Policy seam 上裁剪，避免先删除再重建同类领域类型。

4. **按 ZC-086、ZC-089 收口模型和 Run 生命周期**
   - `server.py` 只做入口编排；
   - 模型解析、Run 状态转换和恢复规则进入各自领域模块；
   - 保留现有不可变 Run binding 语义。

### P1：选择性吸收 dcode 的成熟能力

1. **MCP 每 server 隔离**
   - 借鉴独立 session、独立失败状态、allowedTools/disabledTools 和项目信任；
   - 不需要一次引入 OAuth、自动发现和所有 transport。

2. **最小自定义 Agent 加载**
   - 在真实调用点支持简单 Markdown 定义；
   - 明确来源、覆盖规则、模型和工具策略；
   - 不先建设通用 Plugin catalog。

3. **脱敏 tracing**
   - 先覆盖一次 Run 的模型调用、工具调用、审批、压缩和终态；
   - 默认不记录 secret、完整环境变量和敏感工具参数；
   - hooks 等真正出现外部集成需求时再增加。

4. **扩展 provider adapter**
   - 保持 Run binding 和 AgentEngineProfile 不变；
   - 按真实用户需求逐个加入 provider，而不是复制 dcode 的完整 extras 列表。

### 暂时不建议复制

- 本地 `virtual_mode=False`；
- 无效模型切换后静默使用旧模型；
- 默认可写的长期 Memory；
- Goal、Rubric 和解释器整套同时进入主 middleware；
- 继续扩大 `create_harness_agent()` 或 `server.py` 的条件分支；
- 为尚未存在的 Plugin 生态预建通用抽象。

## 8. 风险和置信度说明

### 已由源码直接确认

- Harness 生产链为 `AgentHost → AgentEnginePool → create_harness_agent() → Deep Agents graph`；
- Harness 动态 Agent catalog 当前未接入；
- Harness 的 PromptEpoch、Run binding、上下文产物进入 ThreadPersistence；
- Harness 的审批工具绕过并发读写锁；
- Harness MCP 指纹忽略 command 和 URL；
- dcode 每进程缓存一张图；
- dcode 本地 backend 使用 `virtual_mode=False`；
- dcode 本地大工具结果和 conversation history 使用临时目录；
- dcode 有动态模型、自定义 Agent、MCP session、Goal、Rubric、hooks 和 tracing。

### 从调用链推断，建议补端到端测试

- Harness MCP 热添加/移除后，已缓存 AgentEngine 仍持有旧工具快照；
- dcode 进程退出或临时目录被清理后，压缩外置原文无法完整恢复；
- dcode 非基础模型在频繁调用时可能反复执行模型解析或实例创建，具体开销取决于 provider 内部缓存。

这些推断没有被包装成既成事实，也没有参与“已交付功能”的判断。

## 9. 最终评价

如果把架构质量理解为“功能多、能跑的场景广”，dcode 当前更强；如果把架构质量理解为“运行身份明确、状态隔离、配置可追溯、长期恢复可靠”，Harness 已形成更适合企业 Host 的骨架。

Harness 当前的主要问题不是方向错误，而是**骨架已经较重，产品能力却还没有完全穿过这套骨架**：MCP 热更新、并发写、动态 Agent、模型领域收口和可观测性都存在“局部实现已有、端到端闭环不足”的情况。

因此最合理的路线不是追平 dcode 的功能清单，而是：

```text
先修运行闭环
  → 删除未接入设计
  → 收口 Server 职责
  → 补 tracing 与 MCP 隔离
  → 再按真实需求增加 provider 和自定义 Agent
```

这样既能保留 Harness 已经建立的企业级边界，也能吸收 dcode 已被实际使用验证的能力，而不会把 dcode 的本地信任模型、临时上下文存储和 feature-heavy middleware 一并带入。

## 10. 一手来源索引

### Harness Code

- [H-Architecture]：[架构总览](../../architecture/架构总览.md)
- [H-Refactor]：[架构重构计划](../../architecture/架构重构计划.md)
- [H-Task-085]：[ZC-085 清理未接线框架](../../tasks/archive/ZC-085.md)
- [H-Task-086]：[ZC-086 提取模型绑定领域服务](../../tasks/archive/ZC-086.md)
- [H-Package]：[`packages/agent/pyproject.toml`](../../../../packages/agent/pyproject.toml)
- [H-Agent]：[`harness_agent/runtime/agent.py`](../../../../packages/agent/harness_agent/runtime/agent.py)
- [H-Server]：[`harness_agent/host/agent_host.py`](../../../../packages/agent/harness_agent/host/agent_host.py)
- [H-Profile]：[`harness_agent/runtime/agent_engine_profile.py`](../../../../packages/agent/harness_agent/runtime/agent_engine_profile.py)
- [H-AgentEngine]：[`harness_agent/runtime/agent_engine.py`](../../../../packages/agent/harness_agent/runtime/agent_engine.py)
- [H-RunContext]：[`harness_agent/runtime/run_context.py`](../../../../packages/agent/harness_agent/runtime/run_context.py)
- [H-Prompt]：[`harness_agent/threads/prompting.py`](../../../../packages/agent/harness_agent/threads/prompting.py)
- [H-Context]：[`harness_agent/threads/context_window.py`](../../../../packages/agent/harness_agent/threads/context_window.py)
- [H-Persistence]：[`harness_agent/threads/thread_persistence.py`](../../../../packages/agent/harness_agent/threads/thread_persistence.py)
- [H-Skills]：[`harness_agent/extensions/skills.py`](../../../../packages/agent/harness_agent/extensions/skills.py)
- [H-Execution]：[`harness_agent/runtime/execution.py`](../../../../packages/agent/harness_agent/runtime/execution.py)
- [H-Workspace]：[`harness_agent/policy/workspace_boundary.py`](../../../../packages/agent/harness_agent/policy/workspace_boundary.py)
- [H-Approval]：[`harness_agent/policy/approval_policy.py`](../../../../packages/agent/harness_agent/policy/approval_policy.py)
- [H-Concurrency]：[`harness_agent/policy/concurrency_guard.py`](../../../../packages/agent/harness_agent/policy/concurrency_guard.py)
- [H-MCP]：[`harness_agent/extensions/mcp.py`](../../../../packages/agent/harness_agent/extensions/mcp.py)

### Deep Agents Code

以下链接固定到本次评估的 dcode commit，避免后续主分支变化导致证据漂移。

- [D-Package]：[`libs/code/pyproject.toml`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/pyproject.toml)
- [D-Agent]：[`deepagents_code/agent.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/agent.py)
- [D-ServerGraph]：[`deepagents_code/server_graph.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/server_graph.py)
- [D-Model]：[`deepagents_code/configurable_model.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/configurable_model.py)
- [D-Config]：[`deepagents_code/config.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/config.py)
- [D-Resume]：[`deepagents_code/resume_state.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/resume_state.py)
- [D-Sessions]：[`deepagents_code/sessions.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/sessions.py)
- [D-Subagents]：[`deepagents_code/subagents.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/subagents.py)
- [D-MCP]：[`deepagents_code/mcp_tools.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/mcp_tools.py)
- [D-Tools]：[`deepagents_code/tools.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/tools.py)
- [D-Goal]：[`deepagents_code/goal_tools.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/goal_tools.py)
- [D-Rubric]：[`deepagents_code/reliable_rubric.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/reliable_rubric.py)
- [D-Hooks]：[`deepagents_code/hooks.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/hooks.py)
- [D-Memory]：[`deepagents/middleware/memory.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/deepagents/deepagents/middleware/memory.py)
- [D-Filesystem]：[`deepagents/backends/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/deepagents/deepagents/backends/filesystem.py)

[H-Architecture]: ../../architecture/架构总览.md
[H-Refactor]: ../../architecture/架构重构计划.md
[H-Task-085]: ../../tasks/archive/ZC-085.md
[H-Task-086]: ../../tasks/archive/ZC-086.md
[H-Package]: ../../../../packages/agent/pyproject.toml
[H-Agent]: ../../../../packages/agent/harness_agent/runtime/agent.py
[H-Server]: ../../../../packages/agent/harness_agent/host/agent_host.py
[H-Profile]: ../../../../packages/agent/harness_agent/runtime/agent_engine_profile.py
[H-AgentEngine]: ../../../../packages/agent/harness_agent/runtime/agent_engine.py
[H-RunContext]: ../../../../packages/agent/harness_agent/runtime/run_context.py
[H-Prompt]: ../../../../packages/agent/harness_agent/threads/prompting.py
[H-Context]: ../../../../packages/agent/harness_agent/threads/context_window.py
[H-Persistence]: ../../../../packages/agent/harness_agent/threads/thread_persistence.py
[H-Skills]: ../../../../packages/agent/harness_agent/extensions/skills.py
[H-Execution]: ../../../../packages/agent/harness_agent/runtime/execution.py
[H-Workspace]: ../../../../packages/agent/harness_agent/policy/workspace_boundary.py
[H-Approval]: ../../../../packages/agent/harness_agent/policy/approval_policy.py
[H-Concurrency]: ../../../../packages/agent/harness_agent/policy/concurrency_guard.py
[H-MCP]: ../../../../packages/agent/harness_agent/extensions/mcp.py
[D-Package]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/pyproject.toml
[D-Agent]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/agent.py
[D-ServerGraph]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/server_graph.py
[D-Model]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/configurable_model.py
[D-Config]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/config.py
[D-Resume]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/resume_state.py
[D-Sessions]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/sessions.py
[D-Subagents]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/subagents.py
[D-MCP]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/mcp_tools.py
[D-Tools]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/tools.py
[D-Goal]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/goal_tools.py
[D-Rubric]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/reliable_rubric.py
[D-Hooks]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/code/deepagents_code/hooks.py
[D-Memory]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/deepagents/deepagents/middleware/memory.py
[D-Filesystem]: https://github.com/langchain-ai/deepagents/blob/4338671aa1d9bd1fd70f20069aac5468697606bf/libs/deepagents/deepagents/backends/filesystem.py
