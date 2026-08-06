---
id: ZC-110
title: Typed IntentOutcome 与纯状态化
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-110-outcome
scope: 将 InteractiveController.dispatch 返回值从 `Promise<InteractiveResult | void>` 改为 `Promise<IntentOutcome>`（accepted/rejected + PresentationEffect），引入稳定 RejectionCode 与 AgentGatewayError 错误码体系，TUI/Web 仅在 accepted 后清理草稿/关闭面板/滚动，并将中文展示文案（activity label、notice 文案）移出领域状态由 Presenter 映射。
acceptance: 所有 dispatch 路径返回明确 IntentOutcome（无 void 分支）；拒绝提交（busy/connection-closed/stale-interaction/capability-missing/not-found）在 TUI 与 Web 均保留用户输入并展示对应原因；同一 InteractiveIntent 序列在 TUI/Web 产生相同 Core outcome（adapter parity 测试）；state.ts 与 InteractiveSnapshot 不含中文 label（grep 验证）；`bun run typecheck`、`bun run test`、`bun run project:check` 全绿。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: bun test --isolate, bun run typecheck, bun run build, bun run project:check 都 100% 通过；包含 IntentOutcome 类型定义、纯状态化领域 notices 移除中文字符串，以及 TUI & Web 双端草稿保护及 parity 测试
references: docs/developer/tasks/ZC-103.md、docs/developer/tasks/ZC-104.md
completed_at: 2026-08-05
---

## 背景

最终架构方案决策 D-06/D-07：**dispatch 返回 Typed IntentOutcome，UI 仅在 accepted 后清理草稿或关闭面板；领域状态保存语义，不保存平台文案**（A-06：所有 dispatch 均返回明确 IntentOutcome，拒绝提交不会清空用户 draft）。

当前实现（ZC-103 产物）：

- `types.ts:86-90`：`InteractiveResult = present | request-handoff | request-exit`；`dispatch` 签名 `Promise<InteractiveResult | void>`（types.ts:140），只有 `command.execute` 分支会返回结果，其余全部 void。
- `state.ts:64-78`：`InteractiveActivity` 的 10 种 kind 各带中文 `label`（"正在思考"、"等待工具审批"等 15 处），Snapshot 直接携带文案。
- 系统 notice 由 reducer 内联中文模板（appendNotice：`已加载 Skill：…`、`协议序号缺口：…`、`\n错误：…` 等）。
- TUI/Web adapter 目前只能通过"提交后看 snapshot 是否变化"推断成败，无法精确执行"accepted 才清草稿"。

## 当前存在的问题

1. `void` 返回无法区分"受理成功"与"静默失败"，两个 adapter 各自猜测，行为不一致（违反 A-06 与阶段 3 完成条件"两个 UI 的提交和拒绝行为一致，输入不丢失"）。
2. 拒绝原因（busy/connection-closed/stale-interaction/capability-missing/not-found）散落在 snapshot 状态推导中，没有稳定 code 供 UI 展示与测试断言。
3. 中文文案在领域状态中，Web 无法做主题化/本地化映射，TUI 文案变更会污染 reducer 测试。

## 为什么现在要修改

- 阶段 3 是阶段 7（WebUiGateway 的 `intent.outcome` 消息需要 accepted/rejected 载荷）与 ZC-104 验收（Web 提交失败保留输入）的契约基础。
- 远程错误已由 ZC-109 收敛到 AgentGatewayError，本任务只需在其上定义 RejectionCode 映射，无协议层改动。

## 目标设计

### IntentOutcome（types.ts，方案 §5.6）

```ts
export type IntentOutcome =
  | { status: "accepted"; effects?: readonly PresentationEffect[] }
  | { status: "rejected"; code: RejectionCode; message: string }

export type RejectionCode =
  | "busy"                  // active Run 中重复提交
  | "connection-closed"     // 连接已关闭，进入只读
  | "stale-interaction"     // Interaction requestId 已失效
  | "capability-missing"    // 缺少 capability
  | "not-found"             // Thread/Model/Skill/MCP 不存在
  | "invalid-argument"      // 参数不合法（含 MCP 校验失败）
  | "agent-error"           // 远端执行错误（映射自 AgentGatewayError）
```

`PresentationEffect`（原 InteractiveResult 三个变体并入）：`{ type: "present"; target; initialQuery? } | { type: "request-handoff"; threadId } | { type: "request-exit" }`。**accepted 才可能携带 effects**；rejected 永不携带。

### 拒绝场景与 Adapter 行为（方案 §5.6 表格）

| 场景 | Outcome | Adapter 行为 |
| --- | --- | --- |
| 提交消息被受理 | accepted | 清空 draft、请求滚动到底部 |
| active Run 中重复提交 | rejected: busy | 保留 draft、展示提示 |
| 连接已关闭 | rejected: connection-closed | 保留 draft、进入只读 |
| Interaction requestId 已失效 | rejected: stale-interaction | 清空旧表单草稿 |
| 缺少 capability | rejected: capability-missing | 保持当前面板并显示原因 |
| Thread/Model/Skill 不存在 | rejected: not-found | 保留当前选择上下文 |

dispatch 内每个 case 显式 return accepted/rejected；`approval-mode.cycle`、`confirmation.resolve` 等也返回 accepted（不再允许 fall-through void）。Controller close 后 dispatch 返回 `rejected: connection-closed`（替代当前 no-op）。

### 纯状态化（D-07）

- `InteractiveActivity`：删除 `label` 字段，kind 即契约；TUI 的 `timeline.tsx`/`theme.ts` 与 Web 的 presentation 各自维护 kind → 中文文案映射（`presentation-shared/timeline-presenter.ts` 在 ZC-112 统一）。
- notice：改为 `{ kind: string; payload?: Record<string, string | number> }`（如 `kind: "skill-loaded", payload: { name }`），presenter 负责格式化中文；`appendNotice` 签名调整，controller 内所有中文模板迁出（`已加载 Skill：`、`协议序号缺口：`、`上下文：…`、`错误：`、`已取消：`、`Agent 运行失败`、`Agent 需要补充信息`）。
- `runtime.ts` 的 `runtimeStatusSummary`/`approvalModeLabel` 等中文 label 保留在 runtime 展示函数（非领域状态），或在 ZC-112 前仅移除"领域状态内"的文案；本任务边界：InteractiveState/Snapshot 零中文，展示函数不强制迁移。

### 契约测试（方案 §13.1）

- IntentOutcome 单元测试：每个 intent 的 accepted/rejected 分支、draft 保留语义（通过 harness 断言 reject 后 adapter 不清空）。
- Adapter parity：同一 Intent 序列（submit/command/cancel/interaction.respond/thread.open）驱动 TUI adapter 与 Web adapter，断言 Core outcome 相同、草稿处理一致（tests/web/application/adapter.test.ts 扩展 + tests/tui/application/adapter.test.ts 对照）。

## 实施步骤

1. `types.ts`：定义 IntentOutcome/RejectionCode/PresentationEffect；修改 `InteractiveController.dispatch` 签名；删除 InteractiveResult。
2. `controller.ts`：dispatch 全分支显式返回；submit/command/thread/model/skill/mcp/interaction/confirmation/approval-mode 各路径补 rejected 判定（busy/connection-closed/stale-interaction/capability-missing/not-found/invalid-argument）；`applyCommandResult` 改为 accepted+effects；close 后 dispatch 返回 connection-closed。
3. `state.ts`：删除 activity label 与 notice 中文模板，改为 kind+payload；`appendNotice`/`applyAgentEvent` 相应调整。
4. TUI adapter（`tui/application/adapter.ts` dispatchInteractive/内部 submit 路径）：仅在 accepted 后清 draft/关闭 picker/滚动；rejected 保留 draft 并显示 transientNotice（中文映射放 TUI presentation）。
5. Web adapter（`web/application/adapter.ts`）：同样按 IntentOutcome 处理；rejected 显示 inline 错误并保留输入。
6. 更新测试：controller.test.ts 全部 dispatch 断言改为断言 IntentOutcome；新增 rejected 场景回归（busy/stale/connection/capability/not-found）；新增 adapter parity 测试；state.test.ts 文案断言改为 kind 断言。
7. `架构总览.md` 补"IntentOutcome 与纯状态"小节；验证 typecheck/test/project:check；提交证据。

## 范围

- dispatch 返回类型改造、RejectionCode 体系、adapter 行为对齐、文案移出领域状态、parity 测试。

## 非范围

- 不改 InteractiveIntent 集合与 AgentGateway 方法（ZC-109 产物不动）。
- 不做 Feature 拆分（阶段 4）与 presentation-shared 抽取（阶段 5）；TUI/Web 的文案映射先各自实现。
- 不修改 packages/protocol；无版本变更；不动 Python 端。

## 验收清单

- [ ] `grep -n "InteractiveResult" packages/cli/src/` 无生产引用；dispatch 签名变为 `Promise<IntentOutcome>`。
- [ ] controller.test.ts 中所有 dispatch 调用断言 accepted/rejected（无 void 断言）。
- [ ] 拒绝回归：active Run 重复提交 → busy 且 draft 保留；连接关闭后提交 → connection-closed；stale interaction.respond → stale-interaction；缺 capability 的 skill.set-enabled → capability-missing；未知 thread.open → not-found。
- [ ] parity 测试：TUI 与 Web adapter 对同一 intent 序列产生相同 outcome 与草稿行为。
- [ ] `grep -rn "[\u4e00-\u9fa5]" packages/cli/src/interactive/state.ts` 仅注释/文档，无字符串字面量文案（activity label 与 notice 模板）；InteractiveSnapshot 无 label 字段。
- [ ] `bun run typecheck`、`bun run test`、`bun run project:check` 全绿；证据与 OCR 结论写入本任务。
