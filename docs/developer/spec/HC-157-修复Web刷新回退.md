# HC-157 修复 Web 刷新回退规格

关联 Task：[HC-157](../task/archive/HC-157-legacy-修复Web刷新回退.md)。

## 通俗流程

首次 `/web` 链接只负责把页面安全带进当前 handoff；连接成功后，CLI 立即给该标签页一张新的、只能成功使用一次的“刷新票”。每次刷新成功再换一张，旧票作废。这样首次链接仍短期有效，但已经接管的页面不会因为使用超过一分钟而失去刷新能力。

## 行为规格

1. `open()` 生成 bootstrap token，绑定 `handoffId + loopback Origin`，TTL 默认 60 秒。
2. WebSocket upgrade 只读校验当前 token；renderer 门禁拒绝第二个仍活跃的连接时，不消费 token。
3. `attachRenderer` 在接受连接前再次校验 presented token；接受后原子轮换 reconnect token，使 presented token 立即失效。
4. `WebUiGateway` 在完整视图前发送 `handoff.token`；Browser 只把 token 写入当前标签页 sessionStorage。
5. reconnect token 不使用独立墙钟 TTL，生命周期严格受当前 handoff 限制；归还 TUI、退出、超时或 CLI close 时立即清除。
6. 刷新断开仍使用现有 10 秒宽限；宽限内成功连接保持 `web-active`，超时才返回 TUI。

## 安全不变量

- token 无 Agent Host capability，不进入日志、React props、地址栏持久片段或磁盘。
- 每枚 token 最多成功建立一个 renderer；已使用 token、错误 Origin、错误 handoff 和已收敛 handoff 全部拒绝。
- 第二窗口判定发生在 token 轮换前，拒绝不能让主页面失去刷新凭据。
- UI 内部消息新增 `handoff.token`，契约版本递增；不修改 `packages/protocol`。

## 错误语义

- upgrade 前凭据无效：HTTP 403。
- upgrade 后并发 renderer：WebSocket `1008 already-open`。
- upgrade 与 attach 之间凭据失效：WebSocket `1008 invalid-token`。
