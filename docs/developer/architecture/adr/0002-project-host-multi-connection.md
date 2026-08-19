# ADR 0002：Project-scoped Agent Host、多个 Connection 与单 Run owner

日期：2026-07-28  
状态：已接受

## 背景

Python Agent 将作为 TUI、无头 CLI、Web 和未来桌面端共享的后端核心。若每个表现层分别持有 AgentEngine、配置或 ThreadPersistence，会产生重复状态、并发 Run 竞争和不同协议实现。若 Web 经 TypeScript 转发，又会让 CLI 变成第二个业务服务端。

## 决策

一个 CLI 进程启动一个绑定 Project 的 Python `AgentHost`。Host 唯一持有 Agent Core、ThreadPersistence、AgentEnginePool、配置和 Skill/MCP catalog，并持有一个 Host-scoped `RunCoordinator`；Run registry、owner、执行、Interaction、终态和 lease 由 Coordinator 统一拥有。stdio 与 loopback WebSocket 都只是 `ProtocolConnection` transport。

```text
TUI / CLI ── stdio JSONL ──┐
                           ├─ AgentHost ── Agent Core / shared resources
Web ── CLI WebUiGateway ───┘   （HC-114 起内置 Web 不直连 Host；Host WebSocket/attachment
                                能力保留，服务未来独立客户端，方案 15.1 非目标不删除）
```

- CLI Connection 是 Host owner；其 EOF/退出关闭整个 Host。attached Connection 断开只取消自己拥有的 Run。
- 同一 Thread 最多一个 active Run；发起 Run 的 Connection 是唯一 Run owner。
- Host 级控制权由 `ControlLease` 唯一持有：启动后 holder 是 owner；只有 owner 签发、已认证且未撤销的 attached Connection 可以 acquire，任一时刻最多一个 holder。`run.start`、`run.cancel`、`context.compact`、config preview/commit 与 Skill/MCP 写操作统一受控，只读查询不受 holder 限制。
- 控制权转换与受控操作受理在同一把 `ControlLease` 锁内线性化；acquire 与 owner 的受控操作竞争时恰好一方被受理，不存在“先检查 holder 再受理”的 TOCTOU 窗口。
- 只有 Run owner 可以响应 Interaction 或取消；其他 Connection 通过 `ThreadWatch` 观察完全相同的 Event。
- `ThreadWatch` 仅能在 Thread 空闲时原子建立，返回持久化快照并登记未来 Event。
- 内置 Web 不再直连 Python：Browser 通过 UI token 连接 CLI 进程内的 `WebUiGateway`，只消费共享 InteractiveController 的序列化视图；首次 bootstrap token 绑定 handoffId、loopback Origin 与 60 秒 TTL，renderer 接管后换发 handoff-scoped 单次重连 token并在每次成功连接后轮换，二者均无任何 Agent Host capability（D-02/D-03，HC-114/HC-156）。Host 侧的 attachment/ControlLease 能力按原语义保留，服务未来独立客户端（方案 15.1 非目标不删除）。
- owner 可按稳定 `attachment_id` 撤销未消费 token、认证中 socket 或已连接 Connection；撤销与自然断线共用同一收敛路径：先拒绝新 permit，再 fail closed Interaction、取消并等待 Run，最后恢复 owner holder。
- 缺少 `run.multithread` 的 Connection 同时只能有一个 starting/active Run；同一 Connection 的第二个 Run 返回 `CONNECTION_RUN_BUSY`，同 Thread 并发仍返回 `THREAD_BUSY`。
- Web 接管由 CLI `PresentationCoordinator` 管理单实例表现层输入权状态机（`tui-active → opening-web → web-active → returning-tui → tui-active`）。Handoff 只转移表现层输入权，Host `ControlLease` holder 始终为 owner（stdio Connection），Coordinator 任何阶段都不调用 `host.control.*`。TUI 是否锁定以 Coordinator 状态为准，不相信 Browser 自报；ready 超时、断开、刷新宽限到期、第二窗口、畸形帧和 CLI close 都进入同一 `returning-tui → tui-active` 收敛路径，无需轮询 owner。共享 InteractiveController 是 Thread/Timeline 的唯一事实来源，返回 TUI 不重建 Controller、不重拉历史。
- v3 Schema 是跨语言 wire contract 的唯一事实来源；transport 和 UI 不定义第二套 DTO。

## 后果

Host 资源与表现层解耦，新增前端只需实现 `RpcTransport` 和复用 v3 Client 语义。`run.start` 的协议 handler 只负责 wire 转换、先发送 accepted response，再消费 `RunExecution.events` 做 fanout；Event sequence 对所有观察者一致，Interaction 不占用 sequence。CLI 必须负责 Host 和本机静态 Web server 的关闭，退出顺序为 `webUiGateway.close → presentationCoordinator.close`（含静态 server 停止）→ 关闭 owner AgentClient → 关闭 sidecar。

内置 Web 的浏览器刷新在重连宽限内自动恢复：同页重连后经 `state.replace` 重同步完整视图。当前明确不提供 active Run replay、owner takeover、CLI 退出后继续运行、daemon discovery、远程认证、多租户、Desktop transport 或 REST/SSE 第二套协议。`host.control.status` 只提供轮询快照，不提供控制权变更 event。

## 备选方案

- Web 经 TypeScript RPC 代理：拒绝，因为会复制 dispatcher、校验和生命周期状态。
- 每个前端独立启动 Python：拒绝，因为不能共享同一 Project 的 active Run 与 AgentEnginePool。
- daemon 常驻 Host：暂不采用；当前产品不需要 discovery、认证和脱离 CLI 的生命周期。
