---
id: HC-156
title: 修复Web刷新回退
feature_area: Web UI 工作台体验升级
parent_task: -
decomposed_by: 历史未记录
priority: P0
status: 待验收
owner: Codex
branch: master
reviewed_at: 2026-08-19
review_due: 2026-09-02
scope: 修复 Web 接管超过首次 UI token TTL 后刷新认证失败并返回 TUI；采用 handoff-scoped 单次重连 token 轮换，修正真实 Browser E2E 启动夹具并补充超时刷新回归。
acceptance: 首次 URL token 仍受 60 秒 TTL 约束；renderer 接受后旧 token 失效并获得仅当前 handoff 有效的单次重连 token；任意停留时长后在 10 秒宽限内刷新可恢复完整 Web 视图；每次成功重连轮换 token；第二窗口拒绝不消耗主页面 token；focused、typecheck、project checks 与真实 CLI Browser 刷新 E2E 通过。
user_docs: docs/user/故障排查.md
developer_docs: docs/developer/spec/HC-156-修复Web刷新回退.md、docs/developer/architecture/架构总览.md、docs/developer/architecture/adr/0002-project-host-multi-connection.md
test_evidence: "修复前真实 Browser 回归 1 pass/1 fail（TTL 后刷新超时）；修复后 Coordinator/契约/Gateway/TUI 50 pass，真实 Browser/loopback 6 pass，真实 CLI+Playwright 刷新 1 pass；完整 lifecycle E2E 中第二窗口与刷新 2 pass，退出收敛 1 fail，退出用例隔离复跑仍在 20 秒超时。typecheck、build、project:check、git diff --check 通过。CLI 项目脚本 829 pass/1 skip/2 fail，两项均为无代码交集的 Compose TUI 既有文案断言，隔离复跑 36 pass/2 fail。"
references: docs/developer/task/HC-105-建立WebBrowserE2E.md、docs/developer/task/archive/HC-114-WebUiGateway与Pr.md
completed_at: -
---

## 问题

`/web` 首次打开时生成的 UI token 固定 60 秒过期。页面虽然把它保存到 sessionStorage 用于刷新，但接管成功后没有续期或轮换；因此 Web 使用超过 60 秒后刷新必然认证失败，断线宽限到期后返回 TUI。

## 用户结果

- 首次页面仍必须在 60 秒内完成连接，旧 URL 不变成长效入口。
- 页面接管成功后，无论停留多久，正常刷新都在 10 秒宽限内恢复同一 Thread 与 Timeline。
- 每次成功连接只换发一枚当前 handoff 的单次重连 token；旧 token 立即失效。
- 第二窗口继续被拒绝，且拒绝不得消耗主页面尚未使用的 token。

## 非范围

- 不改变 Agent Host 控制权、Run 生命周期、Thread 持久化或跨进程 Protocol。
- 不支持多个并发 Web renderer，不把 token 写入日志、React props 或持久化存储。

## 验收

- [x] 超过 bootstrap TTL 后刷新仍保持 `web-active` 并收到完整 `state.replace`。
- [x] bootstrap token 和已成功使用的重连 token 均不可重复使用。
- [x] 第二窗口拒绝、Origin/handoff 校验、10 秒断线宽限保持失败关闭。
- [x] 修正后的真实 CLI + Playwright 刷新用例可运行并通过。

## 实施与评审记录

- 2026-08-19：先以 2 秒 bootstrap TTL 的真实 Chromium 用例复现“刷新后返回 TUI”，修复前稳定失败。
- 2026-08-19：内部 UI 契约升级到 v5，新增严格校验的 `handoff.token`；Coordinator 只在 renderer 真正被接受后消费并轮换 token，第二窗口拒绝发生在消费前。
- 2026-08-19：修正 Browser E2E 的仓库根路径与 Bun 启动命令；刷新用例以真实 TUI、fake Agent、真实 Web server 和 Chromium 通过，不读取模型凭据。
- 2026-08-19：范围内自检无 Required finding；token 不进入日志、React props 或磁盘，旧 token、错误 Origin/handoff、已收敛会话均失败关闭。
- 2026-08-19：完整 lifecycle E2E 的第二窗口与刷新用例通过；“退出 Harness”用例及其隔离复跑均因 CLI 20 秒内未退出而失败，属于退出收敛链路，未计作本修复全绿证据。
