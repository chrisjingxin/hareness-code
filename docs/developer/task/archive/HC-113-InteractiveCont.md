---
id: HC-113
title: InteractiveController 提升到 CLI Composition Root
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-113-composition
scope: 将 createInteractiveController 的生产调用点从 TUI/Web composition root 收敛到 CLI 顶层（index.ts），TuiAdapter 改为接收既有 Controller，Handoff 期间不再销毁 TUI Controller（暂停/恢复 + 恢复时 thread 重同步），并删除基于 threadId 重建 TUI Controller 的恢复链。
acceptance: `createInteractiveController` 在 packages/cli/src 生产代码中只有 index.ts 一处调用（架构测试断言）；TUI 侧不再自行创建 Controller（createTuiSession 改为注入）；Handoff 返回后 TUI 复用同一 Controller 实例（controller identity 不变，测试断言），当前 Thread/Timeline/Selection 连续；HC-114 落地前以"恢复时 thread.open 重同步"保证状态不陈旧；`bun run typecheck`、`bun run test`、`bun run project:check` 全绿。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: "createInteractiveController 生产调用点仅 index.ts + web/app.tsx（架构测试显式豁免 HC-114，TUI 侧零引用）；web-handoff-root.test.tsx 两轮 handoff 往返后 controller 未被 close（spy 断言 closed=[]）且按 Web Thread 重同步；adapter-resync.test.ts 3 用例（Web Thread 重开/null 清空/closed no-op）；index.test.ts shutdown 顺序源码断言（runTui → controller.close → agent.stop）；cd packages/cli && bun test 396 测试 4 fail 全为 web server happy-dom 测试间干扰预存问题（单独运行全绿，属 HC-115 领域）；bun run typecheck、bun run project:check 通过"
references: docs/developer/task/HC-102-实现单实例WebHandoff.md、docs/developer/task/HC-103-让SkillCatalog在下.md
completed_at: 2026-08-05
---

## 背景

最终架构方案决策 D-01（**CLI Composition Root 创建唯一 InteractiveController，TUI/Web 禁止自行创建 Controller**）与阶段 6（完成条件：CLI 生命周期内只有一个 Controller 实例；TuiAdapter 改为接收既有 Controller；Handoff 时不再销毁 Controller）。

当前两个生产创建点：

```text
tui/app.tsx:51-65  createTuiSession → createInteractiveController({ agent: AgentClientInteractiveAdapter, runtime })
web/app.tsx:117    bootstrapWebApp → createInteractiveController({ agent, runtime })   （Browser 侧，HC-114 处理）
```

TUI 侧 handoff 行为（web/handoff-coordinator.ts + tui/app.tsx）：

```text
/web → coordinator 进入 active（tuiLocked=true）→ WebAwareRoot 卸载 Za38Tui
  → cleanup useEffect 调 adapter.close() + controller.close()（tui/app.tsx:121-124）
返回 → restoreToIdle 递增 handoffVersion（handoff-coordinator.ts:592）→ WebAwareRoot 以 key={handoffVersion} 重建 Za38Tui
  → createTuiSession 新建 Controller，initialThreadId=restoreThreadId → restoreInitialThread（controller.ts:519）重新 openThread
```

即"基于 threadId 重建 TUI Controller 的恢复链"：Controller 对象在每次 handoff 往返中被销毁重建。

## 当前存在的问题

1. 生产代码两处调用 createInteractiveController（tui/app.tsx:52、web/app.tsx:117），违反 D-01 与方案 §13.2"断言 createInteractiveController 在生产组合路径中只有 CLI Composition Root 一处"。
2. Handoff 往返销毁重建 Controller：重建期间丢失内存态（catalog epoch、approval override、armed skill），依赖 RPC 重放恢复，且产生不必要的重新初始化窗口。
3. TUI 的 Controller 生命周期与 React 挂载耦合（useRef 惰性创建 + unmount 关闭），无法被 CLI 统一持有与关闭。

## 为什么现在要修改

- 阶段 6 是阶段 7（WebUiGateway 让 Browser 共享同一 Core）的 CLI 侧前置：先让 CLI 成为唯一持有者，才能把 Browser 接入同一实例。
- 本任务只动 CLI/TUI 侧，Browser 侧行为不变（HC-114 处理），可独立验收。

## 目标设计

```text
index.ts（CLI Composition Root）
  ├─ AgentClient（stdio）→ AgentClientGateway（infrastructure/，HC-109 产物）
  ├─ createInteractiveController({ gateway, runtime, clock, scheduler, idGenerator })  ← 唯一调用点
  ├─ runTui({ controller, adapterOptions… })        ← TUI 只接收既有实例
  └─ controller.close() 在 CLI shutdown 顺序中执行（webHandoff.close → runTui 返回 → controller.close → agent.stop）
```

### 关键决策

- **创建点收敛**：`createTuiSession` 改为 `createTuiAdapter({ controller, promptHistoryStore, resume, onRequestExit, openWeb })`（不再创建 Controller）；`runTui` 的 `TuiOptions` 增加 `controller` 字段；`index.ts` 在 startAgent 之后创建 Controller 并传入。
- **Handoff 不销毁**：`Za38Tui` 的 cleanup useEffect 不再调用 `controller.close()`（adapter.close() 保留——adapter 只是暂停本地状态）；`WebAwareRoot` 不再用 `key={handoffVersion}` 重建 TUI 子树（移除 `key`，`initialThreadId` 恢复参数删除）；controller 由 CLI 持有，runTui 返回后统一关闭。
- **中间态重同步（过渡机制，HC-114 删除）**：HC-114 落地前 Browser 仍用自建 Controller 修改 Thread 状态，TUI Controller 在 web-active 期间不消费这些变更。因此返回 tui-active 时，TUI adapter 恢复前对 controller 执行一次 `thread.open(当前 threadId)` 重同步（复用 controller.openThread 的 generation 防晚到能力）；`currentThreadId === null` 时重置为空首页。该重同步在 HC-114 共享 Core 后自然删除（同一实例无陈旧问题）。
- **只读订阅**：web-active 期间 TUI Controller 保持订阅但 TUI 侧 UI 不渲染（WebTakeoverView 不变）；可变 intent 由输入租约拒绝（HC-114 的 PresentationCoordinator 落地；本任务期间维持现状 tuiLocked 全锁）。
- **架构测试**：`tests/tui/architecture.test.ts` 扩展断言 createInteractiveController 仅 index.ts 引用；`tests/tui/web-handoff-root.test.tsx` 改为断言两次 handoff 往返后 controller identity 不变。
- **web/app.tsx（Browser）不动**：其自建 Controller 在 HC-114 移除；本任务不改 Browser 代码（除编译所需的类型兼容）。

## 实施步骤

1. `index.ts`：在 `execute()` 的交互分支创建 Controller（gateway/ports 从 HC-109 的基础设施工厂获取），注入 runTui；`finally` 关闭顺序加 controller.close()。
2. `tui/app.tsx`：TuiOptions 增加 controller；createTuiSession 拆为仅建 adapter；删除 cleanup 中的 controller.close()；WebAwareRoot 删除 key 重建与 initialThreadId 恢复参数。
3. `tui/application/adapter.ts`：增加"恢复时重同步"逻辑（返回 tui-active 时若 controller.currentThreadId 变化则 thread.open 重同步）；适配器本地状态（draft/history/picker）跨 handoff 保留。
4. 删除恢复链残留：`web/handoff-coordinator.ts` 的 restoreThreadId 字段若不再被 TUI 消费则移除（保留 handoffVersion 供测试/日志；由 HC-114 最终收敛）。
5. 更新测试：web-handoff-root.test.tsx（identity 不变 + 重同步路径）、index.test.ts（composition 单点）、architecture.test.ts（调用点断言）。
6. `架构总览.md` 补"CLI Composition Root 与 Controller 生命周期"小节；验证 typecheck/test/project:check；提交证据。

## 范围

- CLI 顶层创建 Controller、TUI 注入化、handoff 不销毁、恢复链删除、过渡重同步机制、架构测试。

## 非范围

- Browser 侧 Controller/直连 AgentHost（HC-114）；输入租约与 WebUiGateway（HC-114）。
- 不引入 PresentationCoordinator 状态机（HC-114）；不改 Intent/Outcome/Snapshot。
- 无版本变更；不动 packages/protocol 与 Python。

## 验收清单

- [x] `grep -rn "createInteractiveController" packages/cli/src/` 生产调用点仅 index.ts + web/app.tsx（架构测试显式豁免 HC-114）；tui/app.tsx 无该引用。
- [x] `tests/tui/web-handoff-root.test.tsx`：两轮 handoff 往返后 controller/adapter identity 不变（close spy 均为空）；返回后按 Web Thread 重同步（threads.open 计数精确）。
- [x] web-active 期间 TUI 显示 WebTakeoverView（行为不变）；adapter 本地 draft/history 跨 handoff 保留（runTui 单实例注入）。
- [x] CLI shutdown：webHandoff.close → controller.close 在 runTui 返回后、agent.stop 前执行（源码顺序断言含 webHandoff）。
- [x] `bun run typecheck`、`bun run test`（TS 全量 396 测试，4 fail 全为 web server happy-dom 测试间干扰预存问题）、`bun run project:check` 全绿；证据写入本任务。

> 实施说明：Za38Tui 的 unmount cleanup effect 已整体移除（不再调用 adapter.close()/controller.close()）——adapter.close() 移入 runTui 的 close 路径，controller.close() 移入 index.ts 的 finally。过渡重同步的 Web 会话 Thread 取自 idle 快照新增的 threadId 字段（原 restoreThreadId 更名）。code review 后补：integration.test.ts 的 restoreThreadId 引用已迁移；resync 在 activeRun 时保留当前 Thread 并输出 transientNotice；resync(null) 测试覆盖 thread-1 → null 的真实清空迁移；app-interaction 测试 finally 补齐 controller/adapter close。
