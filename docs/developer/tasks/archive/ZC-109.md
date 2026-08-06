---
id: ZC-109
title: Interactive Core 依赖倒置与 Port 完备化
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-109-ports
scope: 将 AgentGateway 接口迁入 interactive/ports，AgentClientInteractiveAdapter 移入 infrastructure/agent-client-gateway.ts，移除 interactive 对 AgentClient、StartRunInput、JsonRpcRemoteError 的依赖，并引入 Clock、Scheduler、IdGenerator、PromptHistoryStore 四个可注入 Port 与对应基础设施实现，使测试可注入、reducer 纯净。
acceptance: `interactive/` 目录对 `../ipc/`、react、opentui、DOM、WebSocket 零 import（新增 tests/interactive/architecture.test.ts 固化）；controller.ts 不再出现 JsonRpcRemoteError；state.ts 不再直接调用 crypto.randomUUID()；controller.ts 不再直接调用 Date.now()；全部既有 interactive 测试不改断言即通过；`bun run typecheck`、`bun run test`、`bun run project:check` 全绿。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: bun run typecheck (pass), bun test --isolate (346 pass, 0 fail), bun run build (pass), bun run project:check (pass)
references: docs/developer/tasks/ZC-103.md
completed_at: 2026-08-05
---

## 背景

ZC-103 已把交互语义提取为 `interactive/` 共享内核，但依赖倒置只做了一半（最终架构方案阶段 2）。当前调用链：

```text
interactive/controller.ts ──import──> ../ipc/client（JsonRpcRemoteError，controller.ts:18）
interactive/agent-port.ts ──import──> ../ipc/client（AgentClient、StartRunInput，agent-port.ts:22）
        │
        ▼
infrastructure/ 目录尚不存在（AgentClientInteractiveAdapter 仍在 interactive/agent-port.ts:75）
```

Port 现状：只有 `InteractiveAgentPort`（agent-port.ts:43-72，17 个方法）与 `InteractiveScheduler`（types.ts:146-148，只有 setTimeout）。缺失方案 §5.5 要求的 Clock、IdGenerator、PromptHistoryStore；`crypto.randomUUID()` 在 state.ts 内联 4 处（state.ts:127、399、409、431），`Date.now()` 在 controller.ts:367 内联。

## 当前存在的问题

1. `interactive/` 直接依赖 `../ipc/client`，违反方案 A-07（interactive 对 ipc/平台库零依赖）与依赖方向规则 `interactive ✕ ipc`。
2. 远程错误鉴别（`safeModelDefaultSyncError`，controller.ts:1206-1228）依赖 IPC 层的错误类，Core 无法在无 IPC 环境下独立测试错误映射。
3. 时间与 UUID 内联，Interaction timeout 测试必须依赖手动 scheduler 之外的真实时钟；reducer 无法确定性复现。
4. 生产实现（AgentClientInteractiveAdapter）与接口同住一个文件，未来 WebUiGateway（阶段 7）需要第二套 gateway 实现时无处安放。

## 为什么现在要修改

- 阶段 2 是阶段 3（IntentOutcome 稳定错误码）、阶段 4（Feature 拆分）、阶段 6/7（单 Composition Root + WebUiGateway）的硬前置：拆分与网关都要求 Core 只依赖 Port。
- ZC-103 的测试 harness（tests/interactive/controller.test.ts 的内存 port）说明接口已稳定，本次是纯结构性迁移，行为不变、可独立验收。

## 目标设计

```text
interactive/ports/agent-gateway.ts（接口，原 InteractiveAgentPort 改名 AgentGateway）
               ▲ implements
infrastructure/agent-client-gateway.ts（AgentClientGateway，原 AgentClientInteractiveAdapter）
               │
               ▼
ipc/AgentClient
```

### Port 清单（方案 §5.5）

| Port | 核心用途 | 生产实现（infrastructure/） |
| --- | --- | --- |
| AgentGateway | Run、Thread、Model、Skill、MCP、Config 语义调用 + 稳定错误映射 | `agent-client-gateway.ts` |
| Clock | 当前时间、duration 计算 | `system-clock.ts`（SystemClock） |
| Scheduler | Interaction timeout、可取消定时任务 | `system-scheduler.ts`（SystemScheduler） |
| IdGenerator | 本地 notice、view item ID | `system-id-generator.ts`（CryptoIdGenerator） |
| PromptHistoryStore | TUI Prompt history 持久化 | `prompt-history-file-store.ts`（FilePromptHistoryStore） |

### 关键决策

- `interactive/ports/agent-gateway.ts`：`InteractiveAgentPort` 更名为 `AgentGateway`（`types.ts`、`controller.ts`、`agent-port.ts` 的 import 同步更新；`InteractiveControllerOptions.agent` 字段改 `gateway`）。接口方法不变（17 个），保证 ZC-103 测试 harness 只需改 import。
- `AgentGatewayError`：在 `interactive/ports/agent-gateway.ts` 定义稳定错误类型（code + message），`AgentClientGateway` 负责把 `JsonRpcRemoteError`/网络错误映射为 `AgentGatewayError`（该鉴别逻辑从 controller.ts 的 `safeModelDefaultSyncError` 迁入 gateway）；controller.ts 删除 `../ipc/client` import。
- Clock：`now(): number` + `duration(startMs, endMs)`；controller.ts:367 的 deadline 计算改经 Clock；`InteractiveRuntime` 的 duration/usage 格式化保持现状（ZC-112 再统一）。
- Scheduler：在 ports/scheduler.ts 扩展为 `setTimeout(cb, ms) => () => void` 形态（与现有 InteractiveScheduler 一致，仅移动归属并允许取消语义），controller.ts 与 tests 的 manualScheduler 沿用。
- IdGenerator：`uuid(): string`；state.ts 4 处 `crypto.randomUUID()` 改为经注入的 idGenerator（reducer 签名增加参数或通过 context 传入——选择：`createInitialState`/`appendNotice`/applyAgentEvent 相关函数增加 `ids: IdGenerator` 参数，由 controller 注入）。
- PromptHistoryStore：`load(): Promise<string[]>` + `append(entry: string): Promise<void>`；TUI 的 `tui/application/prompt-history.ts` 文件持久化逻辑迁为 `infrastructure/prompt-history-file-store.ts`，`createTuiAdapter` 的 `promptHistoryFile` 参数改为注入 store（默认 FilePromptHistoryStore）。
- 目录落位：新建 `interactive/ports/`、`infrastructure/`；`AgentClientInteractiveAdapter` 从 `interactive/agent-port.ts` 迁移为 `infrastructure/agent-client-gateway.ts` 的 `AgentClientGateway`，agent-port.ts 中仅保留接口与共享类型（或整体并入 ports/agent-gateway.ts）。
- 架构测试：新增 `tests/interactive/architecture.test.ts`（参照 tests/tui、tests/web 的 regex 断言风格），断言 interactive/ 生产源码不 import `../ipc/`、`react`、`@opentui`、`../tui/`、`../web/`、`node:`、`WebSocket` 全局。

## 实施步骤

1. 新建 `interactive/ports/agent-gateway.ts`：移动/改名接口与 `AgentGatewayError`；新建 `interactive/ports/clock.ts`、`scheduler.ts`、`id-generator.ts`、`prompt-history-store.ts` 接口。
2. 新建 `infrastructure/agent-client-gateway.ts`（迁移 AgentClientInteractiveAdapter + 错误映射）、`system-clock.ts`、`system-scheduler.ts`、`system-id-generator.ts`、`prompt-history-file-store.ts`。
3. 更新 `interactive/controller.ts`、`types.ts`、`state.ts`、`runtime.ts`、`commands.ts`、`command-dispatcher.ts` 的 import 与签名：gateway/clock/idGenerator/scheduler 注入 `InteractiveControllerOptions`。
4. 更新 `tui/app.tsx`（createTuiSession 注入基础设施实现）、`web/app.tsx`（bootstrap 注入）、`tui/application/adapter.ts`（promptHistoryStore）。
5. 更新 `tests/interactive/controller.test.ts` 的 harness（makeHarness 改传内存 gateway/clock/idGenerator）与 `agent-port.test.ts`（改测 AgentClientGateway）；新增 ports/基础设施的单元测试（SystemClock、CryptoIdGenerator、FilePromptHistoryStore 临时文件读写）。
6. 新增 `tests/interactive/architecture.test.ts`；在 `架构总览.md` 补"Interactive Core Port 与基础设施"小节。
7. 验证：`bun run typecheck`、`bun test --isolate tests/interactive`、`bun run test`、`bun run project:check`；提交证据写入本任务。

## 范围

- Port 接口落位 interactive/ports，基础设施实现落位 infrastructure/，依赖方向 `infrastructure → interactive ports + ipc`。
- 移除 interactive 对 ipc 的全部 import；错误映射迁入 gateway。
- 时间/UUID/history 注入化；测试 harness 更新；架构测试新增。

## 非范围

- 不改 InteractiveIntent/InteractiveSnapshot 形状与 dispatch 返回语义（阶段 3 做 IntentOutcome）。
- 不做 Feature 拆分（阶段 4）、不动 TUI/Web 表现层行为。
- 不引入日志 Port、不新增第三方依赖；无版本变更。
- 不修改 packages/protocol 与 Python 端。

## 验收清单

- [ ] `grep -rn "from \"\.\./ipc/\|from '../ipc/" packages/cli/src/interactive/` 无匹配；`grep -rn "JsonRpcRemoteError" packages/cli/src/interactive/` 无匹配。
- [ ] `grep -rn "crypto.randomUUID" packages/cli/src/interactive/` 无匹配；`grep -rn "Date.now" packages/cli/src/interactive/` 无匹配。
- [ ] `tests/interactive/architecture.test.ts` 断言 interactive 零 ipc/平台 import，且测试通过。
- [ ] 既有 controller.test.ts（1020 行）在仅改 harness 注入方式后全部通过（无断言变更）。
- [ ] AgentClientGateway 错误映射测试覆盖：远程错误码 → AgentGatewayError 稳定 code。
- [ ] `bun run typecheck`、`bun run test`（TS 全量）、`bun run project:check` 全绿；证据与 OCR 结论写入本任务。
