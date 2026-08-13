---
id: HC-101-legacy
title: 建立 Host 控制租约与可撤销 Web Attachment
priority: P0
status: 已完成
owner: chrisjingxin
branch: codex/zc-101
scope: 在 canonical v3 Protocol 和 Python AgentHost 中建立唯一输入 holder、受控操作许可、可撤销 Web attachment 与单 Connection Run 限制。
acceptance: Host 能线性化地在 owner 与一个 attached Connection 之间转移控制权；非 holder 的受控操作被拒绝；超时或断线可撤销 attachment 并在 Run 收敛后归还 owner；跨语言契约和并发回归测试通过。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/adr/0002-project-host-multi-connection.md
test_evidence: bun run protocol:check（通过）; cd packages/agent && .venv/bin/python -m pytest -q tests/host/test_control_lease.py tests/host/test_agent_host.py tests/host/test_run_coordinator.py tests/protocol/test_protocol_contract.py（36 passed）; cd packages/cli && bun test tests/ipc/protocol-contract.test.ts（2 passed, 280 expect）; bun run typecheck（通过）; bun run project:check（通过）; bun run test（540 passed, 1 skipped, 19 warnings 为既有 aiosqlite 警告）
references: docs/developer/architecture/adr/0002-project-host-multi-connection.md
completed_at: 2026-08-02
---

## 背景

当前 `AgentHost` 已支持 owner stdio Connection、loopback WebSocket attachment、Run owner、ThreadWatch 和 capability ceiling。Web 可以直接连接同一个 Python Host，但 Host 只知道“谁拥有某个 Run”，不知道“当前应由 TUI 还是 Web 接受新的用户操作”。

本任务只建立 Host 级控制权事实，不实现 CLI Web 会话或页面。后续 HC-102 和 HC-104 都必须通过这里定义的 interface 使用控制权，不能在 TypeScript 或浏览器中自行判断谁可输入。

## 当前存在的问题

- owner 与 attached Connection 可以同时调用 `run.start`、配置写入、Skill/MCP 写入和 compact。
- `run.multithread` 已出现在 capability 中，但 attachment ceiling 和“同一 Connection 只能有一个 active Run”的服务端语义不完整。
- attachment token 只能等待过期或连接自行断开；CLI 在 ready timeout 时无法主动撤销已经认证或仍可认证的 attachment。
- 如果“检查 holder”和“受理受控操作”分两步执行，acquire/release 与 `run.start` 并发时会产生 TOCTOU，破坏唯一输入源 invariant。
- attached Connection 断开后的取消、Interaction 收敛、租约归还顺序没有统一所有者。

## 为什么现在要修改

CLI 生命周期和 Web UI 都依赖一个可查询、可测试的 Host 事实。若先在 TUI 或 Web 中实现互斥，异常关闭、第二窗口和并发请求仍可绕过前端状态，后续只能增加更多补丁。

## 目标设计

新增深模块 `ControlLease`，其 interface 负责 holder、attachment 登记、受控操作许可和归还顺序：

```text
Connection request
  → ControlLease 线性化校验/登记
  → RunCoordinator 或配置/catalog operation
  → operation 结束或 Connection 断开
  → ControlLease 更新状态
```

关键 invariant：

- Host 启动后 holder 是 owner Connection。
- 只有 owner 签发且仍有效的 attached Connection 可以 acquire。
- 任一时刻最多一个 holder；控制权变更与受控操作受理必须在同一锁或 permit 机制下线性化。
- `run.start`、`run.cancel`、`context.compact`、config preview/commit、Skill/MCP 写操作统一受控；只读查询不受控。
- release 只允许当前 holder，且该 Connection 没有 starting/active Run 或未收敛 Interaction。
- revoke/断线先阻止新请求，再取消并等待该 Connection 的 Run 收敛，最后归还 owner。
- Web attachment ceiling 显式排除 `host.attach`、owner-only operation 和 `run.multithread`。

attachment create 结果必须包含不暴露凭据的稳定 `attachment_id`；owner 可通过 canonical operation 撤销该 attachment。撤销既覆盖未消费 token，也覆盖已建立的 attached Connection。

## 实施步骤

1. 在 `packages/protocol/schema/v3.json` 定义 `host.control.acquire/release/status`、attachment revoke 所需载荷、稳定错误码和受控 operation 元数据，并重新生成 TS/Python 类型与 fixture。
2. 在 `packages/agent/harness_agent/host/` 新增 `ControlLease`；用单一锁或 permit 让 holder 变更和受控操作受理不存在 TOCTOU。
3. 让 `AttachmentManager` 返回 `attachment_id` 并支持 owner revoke；token 未消费、认证中和已连接三种状态使用同一关闭路径。
4. 将 AgentHost 受控 operation gate、Run starting/active 计数和 Connection disconnect 接入 `ControlLease`，不在各 handler 复制判断。
5. 固定 attachment capability allowlist；缺少 `run.multithread` 时，在 RunCoordinator 锁内拒绝同一 Connection 的第二个 starting/active Run。
6. 补充 TS/Python contract fixture，以及 acquire/release/revoke、非 holder、active Run、断线、幂等和并发竞争测试。
7. 更新架构总览和 ADR 0002，只记录已实现的 Host invariant 与错误语义。

## 范围

- v3 控制权与 attachment 撤销契约。
- Python Host 的 ControlLease、attachment lifecycle、受控 operation gate。
- capability ceiling 和单 Connection Run 限制。
- 跨语言 contract 与 Host 并发/生命周期测试。

## 非范围

- 不实现 Bun Web server、TUI 接管页或浏览器 lifecycle channel。
- 不实现 React Web UI。
- 不增加 daemon、远程认证、多用户或 owner takeover。
- 不保留旧内部 attachment 返回形状的兼容 wrapper。

## 验收清单

- [ ] owner 与 Web 不可能并发受理两个受控输入；并发 acquire 与 `run.start` 有确定测试。
- [ ] 非 holder 的所有受控 operation 返回统一结构化错误，只读查询仍可使用。
- [ ] active/starting Run 阻止主动 release；断线或 revoke 会先取消并收敛 Run 再归还 owner。
- [ ] attachment token 单次、限时、Origin 绑定，并可由 owner 按 `attachment_id` 撤销。
- [ ] Web ceiling 不包含 `host.attach` 和 `run.multithread`，且同一 Web Connection 的第二个 Run 被拒绝。
- [ ] Protocol fixture、TS 校验和 Python Host 回归均覆盖成功、权限错误、畸形参数和并发竞争。

## 前置

- 无；这是 HC-102 和 HC-104 的 Host 基础。
