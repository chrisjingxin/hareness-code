# HC-116: Web「新建 Thread」按钮退出 Web 会话 —— 修复方案

原始任务：[HC-116](../task/archive/HC-116-修复Web「新建Thread」.md)。本方案只记录实现决策，不替代任务源。

## 通俗说明

- **现在的状况**：Web 界面左侧的「新建 Thread」按钮点击后，本应像 TUI 里的 `/new` 一样清空当前对话、回到新对话首页；实际却是整个 Web 会话直接结束，页面退回 TUI，看起来就像"退出了 web ui"。每点一次，Web 会话就死一次。
- **准备怎么解决**：找到 Web 会话退出的真正入口——不是按钮逻辑写错了，而是按钮发给网关的"命令帧"形状不被网关认可，被当成协议违规帧处理，触发了 Web 会话的 fail-closed 收敛。把帧校验放宽到与类型契约一致（顺带修复命令菜单同类问题），并补两个产品语义问题：新建 Thread 不再清空全局 Thread 列表（历史 Thread 保留可切回）、Web 侧清理本地表现状态并防双击。
- **改完后的变化**：点击「新建 Thread」回到空白新对话首页，页面保持在 Web 接管状态，侧栏历史 Thread 仍在；运行任务时按钮保持禁用（`/new` 命令仍弹确认框，与 TUI 一致）；命令菜单在任务运行期间也能正常执行。

## 已确认现状（根因链）

```text
点击按钮
  → adapter.ts:343  thread-new → { type: "command.execute", commandId: "thread.new" }   ← 无 argument 字段
  → ui-client 发送 interactive.intent 帧
  → gateway consume → parseClientFrame
      → validation.ts:152  exactFields(value, ["type","commandId","argument"])           ← 要求恰好 3 个键
      → argument 缺失 → keys.length(2) !== 3 → 返回 undefined（畸形帧）
  → consume 收到 undefined → coordinator.notifyInvalidMessage()
  → coordinator.ts:265  cleanup("invalid-message") → returning-tui → tui-active
  → gateway onCoordinatorPublish 关闭 channel("handoff-converged")
  → 页面显示"接管已结束" = 用户看到的"退出了 web ui"
```

复现证据（真实校验代码，最小脚本）：

```text
thread-new 帧（无 argument）解析结果: UNDEFINED（畸形帧 → fail-closed 退出 Web）
带 argument 的帧: 通过
input.submit 帧: 通过
```

类型契约与校验的差异是根因：

| 层 | `command.execute.argument` |
| --- | --- |
| `interactive/types.ts:114` | `argument?: string`（可选） |
| `validation.ts:152` | `exactFields(..., ["type","commandId","argument"])`（强制存在） |
| Web adapter 实际发射 | 不携带 `argument` |

为什么现有测试没抓住：

- adapter 测试（`adapter.test.ts`）用 fake client 直接回 outcome，不经过帧校验；
- controller 测试直接调 `controller.dispatch`，同样绕过网关；
- `contracts.test.ts:209` 把"缺 `argument` 即畸形"固化成断言，且其 fixture 一律带 `argument: ""`，与 adapter 真实发射形状不一致；
- E2E smoke 用例不点击该按钮；且本机 pty 桥 flaky（见 `tmp/handoff.md`）。

受影响面（已逐项审计 13 种 interactive intent + 5 种 workspace intent 的 adapter 发射形状）：

- **受损**：`command.execute` 两处发射——`thread-new`（按钮）、`selectCommandMenuItem` 的 active run 路径（命令菜单）。
- **正常**：`input.submit`/`run.cancel`/`catalog.refresh`/`thread.open`/`model.select`/`skill.*`/`mcp.*`/`interaction.respond`/`confirmation.resolve`/`approval-mode.cycle` 及全部 workspace intent，发射形状与校验匹配。

## 目标流程与关键 invariant

无活动 run：

```text
点击「新建 Thread」→ command.execute(thread.new) → 网关校验通过 → controller 执行
clear-thread → 清空 Timeline 回到沉浸式首页 → 页面保持 web-active（不退出）
```

有活动 run：

```text
Web 按钮：disabled（既有行为，busy 时不可点）
TUI /new：command.execute(thread.new) → request-confirmation → 弹确认对话框 → 确认 → 先取消 run 再清空
```

Invariant：

1. 帧校验接受且只接受 `InteractiveIntent` 类型契约允许的形状。
2. 合法意图绝不触发 fail-closed 收敛；畸形帧（未知键、缺 `commandId`、`argument` 非字符串）仍按原路径收敛。
3. Web 与 TUI 的 `thread.new` 语义一致（共享 `command-dispatcher.ts` 的同一 handler 与 controller 的同一 `beginNewThread()`，无平行实现）。
4. `thread.new`/`clear-thread` 只清 conversation scope，不清全局 Catalog（threads/models/skills/mcp 保留，侧栏历史立即可切回）。

## 修复决策

**决策：放宽 `validation.ts` 的 `command.execute` 分支，与类型契约对齐；不要求 adapter 补发 `argument`。**

备选方案的取舍：

- **方案 A（采用）**：校验接受 2 键或 3 键，键必须是 `type`/`commandId`/`argument` 子集。理由：类型契约声明 `argument` 可选是唯一事实来源（controller 端 `dispatchSlashCommand` 对 `argument: undefined` 已正确处理）；校验防御层不应比类型更严格；一处修改同时修复按钮与命令菜单两个发射点。
- **方案 B（否决）**：adapter 恒发 `argument: ""`。理由：掩盖契约不一致而非修复；未来任何新增的 `command.execute` 发射点（TUI 直连不经过网关，但后续其他客户端/测试可能复用网关）只要漏发 `argument` 就重蹈同样的"一次点击退出会话"故障；且 `contracts.test.ts` 的畸形断言仍是错的，会把错误契约永久固化。
- **方案 C（否决，外部方案）**：新增领域 intent `thread.new` 取代 `command.execute` 转发。理由：该方案能救按钮，但 active run 下命令菜单仍走 `command.execute`（`adapter.ts` `dispatchInteractive`），同样缺 `argument` 仍会畸形帧退出 Web——不能单独构成修复，且引入跨端契约扩展对本次 bug 非必需。保留为后续架构候选，但必须与方案 A 共存。

## 第二阶段决策（产品语义补全，合并自外部方案）

在方案 A 之外，采纳以下已核实的语义问题与打磨项（对应实施步骤 6~9）：

1. **保留全局 Catalog**：`resetThreadState()` 调用的 `catalogFeature.reset({})` 会把 threads/models/mcp 全部清为 idle/空。已核实 `CatalogFeature.reset()` 的 threads 分支直接置空；Web 侧无懒刷新（手动刷新按钮已删除，仅 run 结束/接管时刷新），修复退出 bug 后新建 Thread 会让侧栏历史列表空掉直到下次 run 完成。`thread.new`/`clear-thread` 只清 conversation scope（Thread/Timeline/模型选择/Skill/Interaction/Confirmation/sequence），**保留** threads/models/skills/mcp catalog 与工作区文件树。
2. **收敛单一新建入口**：controller 新增 `beginNewThread()` 领域方法，`clear-thread` 命令结果与确认对话框共用，不维护第二份清空逻辑；`thread.open` 的 `resetThreadState` 保持不变（打开后按既有流程刷新 catalog）。
3. **Web 本地表现状态清理 + 防双击**：`thread-new` 成功后清空 draft/composerError/commandMenu/interactionDraft/expandedTools，新增 `threadNewSubmitting` 发布位与 `composerFocusRequest` 计数，按钮在提交期间禁用，成功后将焦点还给 composer。
4. **统一 busy 语义**：按钮在 activeRun/Interaction 时保持 disabled（既有行为），不引入与 `/new` 确认对话框分叉的直接拒绝路径。

## 公开 interface 及错误模式

`InteractiveIntent.command.execute` 类型不变：

```ts
{ type: "command.execute"; commandId: string; argument?: string }
```

校验规则改为（与类型一致）：

```ts
case "command.execute": {
  const keys = Object.keys(value)
  const known = keys.every(key => key === "type" || key === "commandId" || key === "argument")
  return known
    && (keys.length === 2 || keys.length === 3)
    && isNonEmptyString(value.commandId, 128)
    && (value.argument === undefined || isString(value.argument, 64 * 1024))
}
```

错误模式（行为不变）：

- 未知键 / 缺 `commandId` / `commandId` 空或超长 / `argument` 非字符串 → 畸形帧 → fail-closed 收敛（协议违规本就该断开）。
- 业务拒绝（如 busy）→ `intent.outcome(rejected)` + adapter 显示 transient notice，不退出会话。

## 实施步骤（按依赖顺序）

**Phase 1 —— 根因修复（本 bug 必修）**

1. **校验修复**：`packages/cli/src/presentation-coordinator/contracts/validation.ts` 的 `command.execute` 分支改为上述规则。改动仅一处，无依赖。
2. **契约测试更新**：`packages/cli/tests/presentation-coordinator/contracts.test.ts` 第 209 行改为断言"缺 `argument` 解析通过"；新增正反用例（无 `argument` 通过、带 `argument` 通过、未知键拒绝、`commandId` 缺失拒绝）。
3. **网关级回归**：`packages/cli/tests/presentation-coordinator/web-ui-gateway.test.ts` 复用现有真实 controller + real coordinator harness，走真实帧路径提交无 `argument` 的 `command.execute(thread.new)`，断言：收到 `accepted` outcome；Coordinator 仍 `web-active`（未发生 `cleanup`，channel 未被关闭）。
4. **发射端形状固化**：`packages/cli/tests/web/application/adapter.test.ts` 断言 `thread-new` 提交的 intent 精确等于 `{ type: "command.execute", commandId: "thread.new" }`（无 `argument`），防止未来发射端与校验再次漂移。

**Phase 2 —— 产品语义补全（并入外部方案已核实发现）**

5. **收敛新建入口**：`packages/cli/src/interactive/controller.ts` 新增 `beginNewThread()`（threadEpoch+1、`clearThread`、重置模型/Skill 选择、清 confirmation、settle Interaction、重置 sequence、publish），`clear-thread` 命令结果与 `resolveConfirmation` 的 clear-thread 分支共用；不再调用 `catalogFeature.reset({})`（保留全局 catalog）。
6. **controller 层测试**：`packages/cli/tests/interactive/thread-feature.test.ts`（或 controller.test.ts）补成功链路：从已有 Thread 新建后 `currentThreadId === null`、`timeline` 空、`catalogs.threads.items` 保留、`connection.status === "open"`。
7. **Web 本地状态清理与防双击**：`packages/cli/src/web/application/adapter.ts` 的 `thread-new` 改为：置 `threadNewSubmitting` → `executeCoreIntent(command.execute thread.new)` → 成功后清空 draft/composerError/commandMenu/interactionDraft/expandedTools、`composerFocusRequest++`、恢复 `threadNewSubmitting`；rejected 只显示 notice。
8. **表现层接线与测试**：`WebAdapterSnapshot` 增加 `threadNewSubmitting`/`composerFocusRequest`；`workspace-sidebar.tsx` 按钮 `disabled` 并入 `threadNewSubmitting`；`composer.tsx` 监听 `composerFocusRequest` 变化 focus textarea；`web-app.tsx` 无需改动（快照已透传）；补 sidebar/adapter 测试。
9. **回归验证**：`bun run typecheck`、`bun test tests/web tests/presentation-coordinator tests/interactive`；有真实浏览器环境时补手工验证（见验收）。

## 可观察验收

- 无活动 run 点击「新建 Thread」：Timeline 清空、回到沉浸式首页、Thread 侧栏历史列表保留、页面停留在 web-active，不再退出。
- 活动 run 点击「新建 Thread」：按钮禁用（既有行为）；`/new` 命令保持「开始新的 Thread？」确认对话框语义（确认后先取消任务再清空）。
- active run 下选择命令菜单命令可执行（不再踢出 Web）。
- 新建后侧栏历史 Thread 立即可见，可切回并恢复历史内容；提交第一条 Prompt 创建新的持久化 Thread（沿用 `run.start` 对 `threadId: undefined` 的懒创建路径，`ipc/client.ts` 生成新 thread_id）。
- 连续点击不产生重复请求（`threadNewSubmitting` 防双击）。
- Phase 1 + Phase 2 回归测试全绿（契约 / 网关 / adapter / controller），证据写入任务 `test_evidence`。

## 非范围

- 不改 `packages/protocol`（UI 帧契约是 CLI 进程内契约，非 JSON-RPC v3）；不动 Python 端与 TUI 交互语义（`/new` 行为不变，仅内部实现收敛）。
- 不新增领域 intent `thread.new`（方案 C 否决，见决策）。
- 不修 E2E pty 桥 flaky（另立任务）；Playwright E2E 成功链路用例在本机可跑通前以网关级集成测试等价覆盖。
