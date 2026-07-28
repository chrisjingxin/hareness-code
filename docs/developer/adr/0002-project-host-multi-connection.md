# ADR 0002：Project-scoped Agent Host、多个 Connection 与单 Run owner

日期：2026-07-28  
状态：已接受

## 背景

Python Agent 将作为 TUI、无头 CLI、Web 和未来桌面端共享的后端核心。若每个表现层分别持有 Runtime、配置或 ThreadStore，会产生重复状态、并发 Run 竞争和不同协议实现。若 Web 经 TypeScript 转发，又会让 CLI 变成第二个业务服务端。

## 决策

一个 CLI 进程启动一个绑定 Project 的 Python `AgentHost`。Host 唯一持有 Agent Core、ThreadStore、RuntimePool、配置、Skill/MCP catalog 和 active Run registry；stdio 与 loopback WebSocket 都只是 `ProtocolConnection` transport。

```text
TUI / CLI ── stdio JSONL ──┐
                           ├─ AgentHost ── Agent Core / shared resources
Web ─────── WebSocket ─────┘
```

- CLI Connection 是 Host owner；其 EOF/退出关闭整个 Host。attached Connection 断开只取消自己拥有的 Run。
- 同一 Thread 最多一个 active Run；发起 Run 的 Connection 是唯一 Run owner。
- 只有 Run owner 可以响应 Interaction 或取消；其他 Connection 通过 `ThreadWatch` 观察完全相同的 Event。
- `ThreadWatch` 仅能在 Thread 空闲时原子建立，返回持久化快照并登记未来 Event。
- Web 使用 owner 签发的 60 秒、单次、Origin 绑定 token，经 `127.0.0.1` WebSocket 直连 Python；capability ceiling 不能被 `initialize` 提升。
- v3 Schema 是跨语言 wire contract 的唯一事实来源；transport 和 UI 不定义第二套 DTO。

## 后果

Host 资源与表现层解耦，新增前端只需实现 `RpcTransport` 和复用 v3 Client 语义。Event sequence 对所有观察者一致，Interaction 不占用 sequence。CLI 必须负责 Host 和本机静态 Web server 的关闭。

当前明确不提供 active Run replay、浏览器刷新恢复、owner takeover、CLI 退出后继续运行、daemon discovery、远程认证、多租户、Desktop transport 或 REST/SSE 第二套协议。

## 备选方案

- Web 经 TypeScript RPC 代理：拒绝，因为会复制 dispatcher、校验和生命周期状态。
- 每个前端独立启动 Python：拒绝，因为不能共享同一 Project 的 active Run 与 RuntimePool。
- daemon 常驻 Host：暂不采用；当前产品不需要 discovery、认证和脱离 CLI 的生命周期。
