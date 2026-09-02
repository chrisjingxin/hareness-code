# AgentHarness v2 耐久化设计对照调研

## 调研范围

- 外部基线：earendil-works/pi 的 `harness-v2/j4` 分支，核对提交 `f7f933c6e0a127bd2b56336338512092fec0399d`（2026-08-07）的设计文档、`AgentHarness` scaffold、Session/Reducer 与 SQLite 源码。
- 本仓库基线：当前工作区的架构总览、`RunCoordinator`、`ThreadPersistence`、Run execution 与 `threads.watch` 实现。
- 本文只做对比和方案建议，不代表实施决策。

## 先说结论

这篇文章最值得 Harness Code 借鉴的不是 `Session + Lane` 这套外部领域模型，而是更底层的四个设计原则：

1. 对话事实与运行编排事实分开持久化；
2. 外部 effect 前先写 durable intent，effect 后再写结果；
3. 用纯 reducer 从有限记录恢复 suspended operation；
4. 让存储、模型、工具、Hook、timer 全部穿过一个可注入的 effect 边界，以自动生成 crash/race 测试。

这四点能补 Harness Code 当前最大的恢复缺口：本仓库已经耐久保存用户消息、Run binding、Transcript 和 LangGraph checkpoint，但 active Run 的编排状态仍主要在 `RunCoordinator._runs` 与 `asyncio.Task` 中。相同 `run_id` 的重复受理在持久层返回 `created=False` 后只返回“已受理、无事件”，不会从持久记录恢复并继续该 Run。因此“请求不重复写入”已经具备，“进程崩溃后继续已受理工作”尚未形成闭环。

## 外部设计的核心机制

### 1. 四类状态分离

外部设计把状态拆成 conversation tree、lane pointer、每 lane 的 operation log、global facts。Tree 只保存对话，operation log 只保存执行意图和恢复线索；删除 operation log 不应破坏对话本身。[设计目标与 Session 模型](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L23-L77)

这与本仓库“Transcript 业务事实、CompressionCheckpoint、LangGraph 图状态、Diagnostic Log”四层分界方向一致，参见 `docs/developer/architecture/架构总览.md:157-229`。差异是本仓库尚没有专门表达 active Run 编排意图的耐久日志。

### 2. intent-before-effect 与 provisioned id

文章的耐久规则是：执行 provider/tool/hook 等 effect 之前，先写带预分配结果 ID 的 intent；完成后用同一 ID 追加结果。崩溃落在两者之间时，恢复器能精确判断“结果已存在”“可安全重试”或“必须写 synthetic interrupted result”。[记录规则与 record catalog](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L174-L335)

尤其值得借鉴的是工具恢复策略：`tool_started` 固化 effective args、result ID 和 `replay: never | safe`；只有持久记录和当前工具声明都为 `safe` 才允许重放。否则恢复时明确关闭调用，避免重复执行有副作用的工具。

### 3. 纯 reduction 与显式 suspended/resume

打开 Session 只做有界查询与纯 reduction，不写入、不启动 effect；一个未完成 operation 被恢复为 suspended，随后由 `resume()` 或 `abort()` 显式处理。恢复查询按 open operation、该 operation 之后的 records、该 operation 自己追加的 entries 限界，不扫描整段历史。[Recovery](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L642-L692)

该分支已经实现 record 类型、open-operation 查询、纯 reducer 和一部分 Memory/JSONL/SQLite 投影；但完整运行时仍未落地。源码中的 `prompt`、`resume`、lane、watch 等仍返回 `HarnessNotImplemented`，见 [`agent-harness.ts`](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/src/harness/agent-harness.ts#L305-L505)。工作包清单也显示 R3、H0-H8、C/N/O 等核心运行时未完成，[Implementation status](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L3176-L3424)。因此它应被视为高质量设计稿和局部原型，而不是已被生产验证的完整实现。

### 4. deterministic effects 与 crash matrix

`drive: manual` 在每个 effect 前停车，测试逐个释放 action；每释放一步就可以关闭、重开并恢复同一后端。这样 crash site 从真实执行轨迹机械生成，而不是手写几个 happy path。[Effects、drive modes 与 race catalog](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L1903-L2084) [Testing strategy](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L3103-L3175)

这是整篇设计里最有复用价值的测试思想：生产和测试走同一 procedure，只替换 effect boundary。

### 5. snapshot + live stream 无缝衔接

`watch()` 在一个动作中捕获 snapshot 并开始缓冲，调用方先把 snapshot 发上 wire，再 `start()` 顺序冲刷缓冲并切到 live，避免 snapshot 与订阅之间的事件缺口。快照还包含 mid-turn streaming message、running tools 和 suspended operation。[Snapshots and subscription](https://github.com/earendil-works/pi/blob/f7f933c6e0a127bd2b56336338512092fec0399d/packages/agent/docs/harness-v2.md#L1102-L1169)

本仓库 `threads.watch` 当前只允许 Thread 空闲时，在 `idle_thread` 临界区内读取历史并登记 watch，见 `packages/agent/harness_agent/host/agent_host.py:2047-2053`。外部方案可作为未来“运行中接入/断线重连”的参考，但应排在 durable operation recovery 之后。

## 与当前 Harness Code 的关键差异

| 维度 | 当前 Harness Code | 外部设计 | 判断 |
|---|---|---|---|
| 领域身份 | canonical 只有 project/thread/message，明确不引入 session；同 Thread 单 active Run，不同 Thread 并发 | Session 内多 lane，每 lane 单 operation，lane 间并发 | 不复制 lane/session；把 lane mutation line 改造成 per-thread mutation line |
| 对话事实 | append-only Transcript；压缩通过投影检查点，不改写 Transcript | append-only tree；compaction entry 是自包含上下文检查点 | 方向一致，保留当前 Transcript/Projector |
| Run 受理 | `accept_run` 原子保存 snapshot、binding、Thread 索引和 user Transcript | `operation_started` 是 durable acceptance | 当前受理事实已有基础，但缺少可恢复的 operation state machine |
| 活跃编排状态 | `RunCoordinator._runs`、`RunState`、`asyncio.Task` 为主；close 主动取消 | operation log 可 reduction 为 suspended；close 后可 resume | 这是首要差距 |
| 工具 crash 窗口 | 完整工具语义在 tool boundary 后批量写 Transcript | effect 前 `tool_started`，后写 result；显式 replay policy | 建议优先补齐 |
| 观察 | 连续 sequence 事件；`threads.watch` 仅 idle 时原子建立 | snapshot + buffer + live，可 mid-run attach | 后续可借鉴 |
| 成本 | Run 内累计 usage，终态事件返回；Transcript record 不承担完整尝试账本 | 每次物理 provider 请求独立 usage record，失败/丢弃尝试也计费 | 可作为独立后续能力，不应阻塞恢复主链 |
| 扩展 | DeepAgents middleware、Plugin Hook、Protocol event、Diagnostic Log 分散但边界明确 | Events 被动、Hooks 可改变执行、Telemetry 纯观察 | 借鉴语义分层，不必照搬 TypeScript Hook API |

本仓库关键证据：

- `packages/agent/harness_agent/threads/thread_persistence.py:3195-3285`：Run 受理及重复受理；
- `packages/agent/harness_agent/host/run_coordinator.py:885-990`：受理后创建进程内 task；
- `packages/agent/harness_agent/host/run_coordinator.py:1063-1083`：Host close 取消并收敛所有 active Run；
- `packages/agent/harness_agent/threads/thread_persistence.py:3985-3991`：`load_run_state` 当前读取的是模型/执行 binding，而不是 unfinished operation reduction；
- `packages/agent/harness_agent/host/run_execution.py:457-487,920-948`：完整语义边界后再排队/flush Transcript。

## 建议方案

### P0：先定义“耐久 Run operation”，不改 Thread 领域模型

新增与 Transcript 平行的 orchestration journal，按 `project_fingerprint + thread_id + run_id` 隔离。最小记录只覆盖：

- `run_accepted` / `run_finished` / `abort_requested`；
- `model_attempt_started` 与结果 ID；
- `tool_started`、effective args、result ID、replay policy；
- 必要的 pending interaction / queued input 受理事实。

Transcript 继续是用户可见历史和模型投影的唯一事实源；journal 不进入模型上下文、UI transcript 或压缩摘要。

### P1：实现纯 reducer 和显式恢复策略

从 journal + Transcript 中有限查询并归约出 `Idle | SuspendedRun | Corrupt`。Host 启动时只列出 suspended，不自动执行副作用；由 UI/调用方选择 resume 或 abort。恢复优先保证：

1. 已受理 user 输入不丢；
2. 已落 Transcript 的结果不重复；
3. 未知是否执行过的有副作用工具默认不重放；
4. 只有证明为 read-only/idempotent 的工具才允许 `safe` replay；
5. 每个 accepted Run 最终都有唯一 completed/failed/aborted 结果。

### P1：同时建立 effect boundary 和 crash-prefix 测试

不要先写恢复分支、最后补测试。先抽出最小 `Effects` port（journal write、Transcript write、provider request、tool execution、interaction、timer），再让现有自动模式直接透传，测试模式逐 effect 停车。至少覆盖：

- accept 后、task 创建前崩溃；
- model request 前后崩溃；
- tool intent 后、effect 前；effect 后、result commit 前；
- abort 与 tool/finish/interaction 的两种顺序；
- 相同 durable prefix 恢复两次仍幂等。

### P2：再扩展运行中 watch 与 usage ledger

- watch：保留现有 Protocol sequence，增加 `snapshot capture + buffer + arm`，支持 mid-run reconnect；不必采用外部设计“无 sequence”选择。
- usage：每个物理 provider attempt 独立写账，包含失败重试、overflow 丢弃和 tool replay；与 Transcript entry 的展示 usage 分离。

## 不建议照搬

1. **不引入 Session/Lane 作为新公开领域对象。** 本仓库已明确 `thread_id` 是唯一跨层会话身份；lane 会制造 Thread/Session/Lane 三套重叠概念。
2. **不替换现有 Transcript + CompressionCheckpoint + ContextProjector。** 两边 append-only/context checkpoint 的核心方向一致，当前方案已针对 LangGraph 和大型 Artifact 做了完整约束。
3. **不增加 JSONL/Memory 生产后端。** 外部多后端服务于库产品与兼容测试；Harness Code 的 canonical SQLite 已足够，可用内存 fake 做 reducer/effects 测试。
4. **不先做多 lane 并发或 fork。** 当前不同 Thread 已可并发，子代理也有 execution identity；只有出现明确的“一个 Thread 内多个长期可见分支”需求时再评估。
5. **不把外部工作包状态当成熟度证明。** 设计很完整，但核心 AgentHarness runtime 在核对提交中仍是 scaffold。

## 建议的决策顺序

如果要立项，应先回答两个产品问题，再进入 Spec：

1. 崩溃恢复默认是“显式提示用户 resume/abort”，还是特定安全阶段自动 resume？建议默认显式。
2. 哪些 Harness 工具能证明为 replay-safe？建议默认 `never`，只为纯读取或具备稳定幂等键的工具逐项开放。

答案确认后，可把交付拆为三段：`durable acceptance + suspended inventory` → `model/tool recovery + crash matrix` → `mid-run reconnect + usage ledger`。不要把 lanes、forks、Hook API 重构塞进首期。
