# HC-156 修复 Web 刷新回退实施计划

关联 [Task](../task/HC-156-修复Web刷新回退.md) 与 [Spec](../spec/HC-156-修复Web刷新回退.md)。

## 可演示停点：长时间停留后刷新仍在 Web

1. 先增加真实浏览器回归，把 bootstrap TTL 缩短到 2 秒；等待过期后刷新，修复前应返回 TUI。
2. 在 Coordinator 中把 upgrade 校验与 renderer 接受/消费分离；接受后生成下一枚 handoff-scoped 单次 token。
3. UI 契约新增 `handoff.token`，Gateway 在视图前下发，Browser 写入 sessionStorage；同步严格帧校验与契约版本。
4. 保持第二窗口、Origin、handoff、断线宽限与收敛门禁；增加旧 token 失效和拒绝不消费 token 的单测。
5. 修正 E2E fixture 的仓库根路径与 Bun 启动命令，运行真实 CLI + Playwright 刷新用例。
6. 更新用户故障排查、Web 架构与 ADR；执行 focused tests、typecheck、project check 和 diff 检查。

用户查看方式：运行 `/web`，停留超过一分钟后刷新；页面应恢复同一 Timeline，不再返回 TUI。

## 回滚

回滚 `handoff.token` 契约、Coordinator token 轮换和 Browser sessionStorage 更新即可恢复原行为；不涉及数据库或用户数据迁移。
