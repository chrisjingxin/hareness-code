# HC-157 修复 Web 刷新回退 Todo

关联 [Task](../task/archive/HC-157-legacy-修复Web刷新回退.md)、[Spec](../spec/HC-157-修复Web刷新回退.md) 与 [Plan](../plan/HC-157-修复Web刷新回退.md)。

## 可演示停点：长时间停留后刷新仍在 Web

- [x] 加入超过 bootstrap TTL 后刷新失败的真实 Browser 回归，记录修复前红灯。
- [x] 实现 bootstrap token 与 handoff-scoped 单次 reconnect token 的接受后轮换。
- [x] 新增并严格校验 `handoff.token`，Browser 仅写当前标签页 sessionStorage。
- [x] 覆盖旧 token 失效、第二窗口拒绝不消费 token、失效主连接替换和收敛清理。
- [x] 修正 E2E 仓库根路径与 Bun 启动命令，真实 CLI 刷新用例通过。
- [x] 同步文档并完成 focused、类型、项目与 diff 检查；把证据写回 Task。

查看方式：执行 `/web`，保持页面超过一分钟后刷新，确认仍为同一 Web 工作台与 Timeline。
