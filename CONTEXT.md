# Harness Code 领域词汇

本文是跨 `cli`、`protocol` 与 `agent` 的通用语言。代码、协议和开发文档不得用同义词表达另一种生命周期。

| 术语 | 定义 | 不包含 |
| --- | --- | --- |
| Agent Core | 执行 Agent 行为的 Python 领域与运行模块。 | transport、连接、UI。 |
| Agent Host | 一个 Project 范围内承载 Core、ThreadPersistence、RuntimePool、配置、Skill、MCP 与 RunRegistry 的 Python 进程。 | 多 Project、daemon discovery。 |
| Connection | 一个前端与 Host 的临时协议连接，保存协商、capability、watch 和待处理 Interaction。 | Runtime、数据库、独立配置副本。 |
| Interactive Core | TUI 与 Web 共用的进程内交互语义层，统一 Run、Timeline、Interaction、Thread 操作、catalog 与 Slash Command；代码 interface 固定称为 `InteractiveController`。 | 持久化身份、UI 状态、Host transport、Harness `Session`。 |
| Interactive Adapter | 把 TUI 或 Web 的输入映射为 Interactive Core intent，并把共享 snapshot 渲染到具体界面。 | Agent RPC、业务 reducer、命令语义。 |
| Handoff | TUI 把 Host 输入控制权临时交给 Web、随后再归还的过程。 | Thread、Run、可恢复对话、Harness `Session`。 |
| Host owner | 启动 Host 并决定其生命周期的 CLI Connection。 | Run 的天然所有权；Run owner 由发起连接决定。 |
| Project | CLI 启动 Host 时绑定的规范化 workspace 与配置边界。 | 客户端 `initialize` 传入的 cwd。 |
| Thread | Project 内持久化的对话。 | Harness `Session`。 |
| Run | Thread 上的一次 Agent 执行，同一 Thread 最多一个 active Run。 | Connection 生命周期。 |
| Run owner | 成功发起 Run 的唯一 Connection，独占该 Run 的审批、问答和取消。 | Host owner、observer。 |
| Interaction | Run 向 owner 发起的审批或问答 JSON-RPC request。 | 可广播 Event、sequence。 |
| Event | Run 产生的不可变观察结果；同一 Run 内以 sequence 连续排序。 | 反向 Interaction。 |
| ThreadWatch | 对空闲 Thread 的原子历史快照和后续 Event 订阅。 | active Run replay。 |
| Transport | JSON-RPC framing、I/O、背压和关闭行为。 | capability、Run 路由、领域决策。 |

协议字段使用 `snake_case`，TypeScript SDK 对表现层公开 `camelCase` 语义对象。Harness 不引入 `Session` 概念；持久化对话只称 Thread，共享交互层只称 Interactive Core，TUI/Web 控制权交接只称 Handoff。
