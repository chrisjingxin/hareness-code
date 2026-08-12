---
id: HC-116
title: 修复 Web「新建 Thread」按钮点击后退出 Web 会话
priority: P0
status: 已完成
owner: chrisjingxin
branch: master
scope: 对齐 command.execute 帧校验与 InteractiveIntent 类型契约（argument 可选），使 Web「新建 Thread」与命令菜单命令在真实 WebSocket 路径下可执行；收敛 beginNewThread() 单一新建入口并保留全局 Catalog（历史 Thread 侧栏立即可切回）；Web 本地表现状态清理与防双击；补充网关级/契约/adapter/controller 回归测试。
acceptance: 无活动 run 时点击「新建 Thread」回到沉浸式首页、Thread 侧栏历史列表保留、Web 会话保持 web-active（不收敛回 TUI）；active run 时按钮保持禁用；命令菜单命令在 active run 下可执行；连续点击不产生重复请求；bun run typecheck 与 bun test tests/web tests/presentation-coordinator tests/interactive 全绿。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-116-修复Web「新建Thread」.md
test_evidence: bun run typecheck 通过；bun test tests/interactive tests/presentation-coordinator tests/web tests/tui：392 pass / 1 fail（唯一失败 tests/web/bundle.test.ts 为预存 EISDIR 并行 flake，单跑 3 pass，pristine HEAD 同样失败，非本改动引入）；tests/e2e 4 fail/4 error 与 Python pytest 1 个失败均为预存环境问题（HEAD 同款）；双轴 code-review：Standards 0 硬违规 / Spec 0 发现。注意：真实浏览器点按验收因本机 E2E pty 桥 flaky 未执行，详见任务验收清单
references: 3ea1443
completed_at: 2026-08-07
---

## 背景

Web 页面（HC-114 起）只通过 `WebUiGateway` 与 CLI 进程内共享 `InteractiveController` 通信：页面提交 `interactive.intent` 帧，网关做形状防御（`packages/cli/src/presentation-coordinator/contracts/validation.ts`），畸形帧 fail-closed 收敛整个 Web 会话回 TUI。

「新建 Thread」按钮（`packages/cli/src/web/presentation/workspace-sidebar/workspace-sidebar.tsx`）dispatch `thread-new`，Web Adapter（`packages/cli/src/web/application/adapter.ts`）将其转换为 `{ type: "command.execute", commandId: "thread.new" }` 提交——与 TUI `/new` 走同一个 `thread.new` 命令语义（`command-dispatcher.ts`：无活动 run 时 `clear-thread`，有活动 run 时弹确认对话框）。

## 当前存在的问题

点击「新建 Thread」没有新建对话，而是 Web 会话直接结束、页面退出回 TUI。根因是帧校验与类型契约不一致：

- 类型契约：`InteractiveIntent` 中 `command.execute` 的 `argument` 声明为可选（`packages/cli/src/interactive/types.ts`）。
- 帧校验：`validation.ts` 的 `command.execute` 分支使用 `exactFields(value, ["type", "commandId", "argument"])`，要求对象**恰好**包含 3 个键，`argument` 缺失即校验失败。
- Adapter 实际发射：`thread-new` 与命令菜单（active run 路径）发送的 `command.execute` 均**不带** `argument` 字段。

因此每点击一次按钮就产生一帧畸形帧 → 网关 `notifyInvalidMessage()` → `cleanup("invalid-message")` → 阶段收敛 `returning-tui → tui-active` → 网关关闭渲染 channel → 页面显示"接管已结束"。表现即"退出了 web ui"。

受影响范围：`command.execute` 的两处 Web 发射点（`thread-new` 按钮、active run 下选择命令菜单命令）。其余 13 种 interactive intent 与 5 种 workspace intent 的发射形状均与校验匹配，不受影响。

## 为什么现在要修改

- 用户可见的 P0 功能损坏：Web 侧无法新建对话，且点击一次即把整个 Web 会话踢回 TUI（`requestReturn` 之外的意外退出路径，破坏单窗口 invariant 的用户体验）。
- 校验比类型契约更严格属于协议防御层的实现 bug；当前测试未覆盖"真实帧形状"这一层：adapter 测试用 fake client（不过校验）、controller 测试直接 dispatch（不过网关）、`contracts.test.ts:209` 反而把"缺 argument 即畸形"固化成断言。E2E smoke 用例未点击该按钮，且本机 pty 桥 flaky。
- 修复后 Web 与 TUI 的 `/new` 语义完全一致（共享同一 `thread.new` 命令），无需新增任何产品能力。

## 目标设计

目标流程（无活动 run）：

```text
点击「新建 Thread」→ adapter 发 command.execute(thread.new) → 网关校验通过
→ controller 执行 clear-thread → 清空 Timeline 回到沉浸式首页 → 页面保持 web-active
```

关键 invariant：

1. 帧校验接受且只接受 `InteractiveIntent` 类型契约允许的形状；`command.execute` 允许 2 键（无 `argument`）或 3 键（带 `argument`），不允许未知键。
2. 合法意图**绝不**触发 fail-closed 收敛；畸形帧（未知键、缺 `commandId`、`argument` 非字符串）行为不变。
3. Web 与 TUI 的 `thread.new` 语义一致：无活动 run 直接清空；有活动 run 弹确认对话框（确认后先取消 run 再清空）。

修复方案（决策见设计文档）：放宽 `validation.ts` 的 `command.execute` 分支校验，与类型契约对齐；不要求 Adapter 补发 `argument`。

## 实施步骤

1. `packages/cli/src/presentation-coordinator/contracts/validation.ts`：改写 `command.execute` 分支为显式键白名单（`type`/`commandId`/`argument`，键数 2 或 3），保留 `commandId` 非空 ≤128、`argument` 可选字符串 ≤64 KiB 约束。
2. `packages/cli/tests/presentation-coordinator/contracts.test.ts`：修正第 209 行"缺 argument 即畸形"断言；新增：无 `argument` 解析通过、带 `argument` 解析通过、带未知键拒绝。
3. `packages/cli/tests/presentation-coordinator/web-ui-gateway.test.ts`：新增回归用例——真实帧路径提交无 `argument` 的 `command.execute(thread.new)`，断言收到 `accepted` outcome 且 Coordinator 仍处于 `web-active`（未触发收敛）。
4. `packages/cli/tests/web/application/adapter.test.ts`：补 `thread-new` 发射形状断言（提交的 intent 为 `{ type: "command.execute", commandId: "thread.new" }` 且不含 `argument`），固化发射端契约。
5. `packages/cli/src/interactive/controller.ts`：新增 `beginNewThread()` 领域方法（threadEpoch+1、clearThread、重置模型/Skill 选择、清 confirmation、settle Interaction、重置 sequence、publish），`clear-thread` 命令结果与确认对话框共用；不再调用 `catalogFeature.reset({})`（保留全局 catalog）。
6. `packages/cli/tests/interactive/`：补成功链路测试——从已有 Thread 新建后 `currentThreadId === null`、Timeline 空、`catalogs.threads.items` 保留、`connection.status === "open"`。
7. `packages/cli/src/web/application/adapter.ts`：`thread-new` 改为置 `threadNewSubmitting` → 提交 → 成功后清空 Web 本地 Thread 级表现状态（draft/composerError/commandMenu/interactionDraft/expandedTools）、`composerFocusRequest++`；快照新增 `threadNewSubmitting`/`composerFocusRequest`。
8. `workspace-sidebar.tsx` 按钮 `disabled` 并入 `threadNewSubmitting`；`composer.tsx` 监听 `composerFocusRequest` 变化聚焦 textarea；补 sidebar/adapter 测试。
9. 回归：`bun run typecheck`、`bun test tests/web tests/presentation-coordinator tests/interactive`。

## 范围

- 修复 `command.execute` 帧校验与类型契约不一致；补齐三层回归测试；Web「新建 Thread」与命令菜单命令恢复可用。

## 非范围

- 不改 `packages/protocol`（本契约是 CLI 进程内 UI 帧，非 JSON-RPC v3）；不动 Python 端与 TUI（TUI `/new` 已正常）。
- 不新增产品能力（不做强制确认、不做删除前二次确认等）。

## 版本影响

无版本变更（bug 修复：Web「新建 Thread」行为回归到既有设计语义，不新增能力、不改协议与配置）。

## 验收清单

- [x] 无活动 run 点击「新建 Thread」：Timeline 清空、回到沉浸式首页、Thread 侧栏历史列表保留、页面停留在 web-active，不再退出。—— 由三层自动化测试覆盖（contracts 帧解析、web-ui-gateway 不收敛回归、adapter 发射形状）；**真实浏览器点按待环境验证**（本机 E2E pty 桥 flaky，见 tmp/handoff.md，另立任务修基建后补 Playwright 用例）。
- [x] active run 时「新建 Thread」按钮保持禁用（sidebar 测试断言）；`/new` 命令确认对话框语义不变（controller 测试断言确认后取消并清空）。
- [x] active run 下选择命令菜单命令可执行（同帧路径，随本修复一并恢复）。
- [x] 连续点击不产生重复请求（adapter 防双击测试：提交中第二次点击不发射新 intent）。
- [x] 新建后侧栏历史 Thread 可切回并恢复（controller 测试断言 catalog 保留）；第一条 Prompt 创建新的持久化 Thread（沿用既有 run.start 懒创建路径，`ipc/client.ts` 生成 thread_id）。
- [x] `bun run typecheck` 通过；`bun test tests/interactive tests/presentation-coordinator tests/web tests/tui`：392 pass / 1 fail（唯一失败 tests/web/bundle.test.ts 为预存 EISDIR 并行 flake，单跑 3 pass，pristine HEAD 同样失败）。证据已写入 test_evidence。

**未完成项（环境阻塞，不在本任务验收内）**：真实浏览器点按与 Playwright E2E 成功链路用例——本机 E2E pty 桥无法跑通（`tmp/handoff.md` 记录），归属 HC-105/HC-115 的 E2E 基建任务。
