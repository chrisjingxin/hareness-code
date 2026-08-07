# ZC-101 / ZC-102 回检问题整改方案

原始需求：[ZC-101](../tasks/archive/ZC-101-legacy.md)、[ZC-102](../tasks/ZC-102.md)

原始方案：[ZC-101 方案](ZC-101.md)、[ZC-102 方案](ZC-102.md)

> 本文是针对 2026-08-03 代码回检结果建立的独立整改方案。后续执行 Thread 必须同时读取两份原始任务、原始方案和本文；本文只覆盖下列已确认缺陷，与原方案冲突时以本文的整改决定为准。

## 通俗说明

ZC-101 的 Host 控制租约主体已经落地，ZC-102 的 Coordinator、静态服务和接管状态机也已有实现，但真实入口还存在两类阻断：浏览器会把两个本应独立的随机端口误判为非法，TUI 又会因为 React 调用了未绑定的实例方法而在启动时抛错。因此，现有测试即使全部通过，也不能证明真实 `/web` 可用。

此外，页面在认证成功后没有继续处理 lifecycle `shutdown`，系统浏览器启动失败不会正确通知 Coordinator，第二次 handoff 会错误重建仍可输入的 TUI；ZC-101 的生成类型还把一个固定字段放宽成了可选字段。这些问题需要在 ZC-102 完成前统一整改，并补上能覆盖真实调用方式的回归测试与交付证据。

```text
修正 Protocol 固定字段
  → 修正 Browser 双端口校验与连接收敛
  → 修正 TUI 订阅和二次 handoff 稳定性
  → 修正系统 Browser opener 错误传播
  → 更新故障排查
  → 定向测试、项目级检查、OCR 检视和任务证据闭环
```

## 回检结论

| 级别 | 问题 | 结论 | 已确认依据 |
| --- | --- | --- | --- |
| P0 | 页面要求 Agent attachment 与 Bun 页面使用同一端口 | 属实 | Bun server 与 Python attachment listener 都以端口 `0` 独立绑定；`validateAgentEndpoint` 却比较两者端口，正常 URL 会在创建 socket 前失败。现有单测还把同端口写成了正确预期。 |
| P0 | `useSyncExternalStore` 收到未绑定的 Coordinator 方法 | 属实 | `getSnapshot` 和 `subscribe` 都读取 `this`；以普通函数调用时分别在 `this.phase`、`this.listeners` 抛出 `TypeError`。 |
| P1 | accepted 后不再持续处理 lifecycle shutdown，abort 也不关闭已认证 Agent socket | 属实 | `waitForAccepted` 收到 accepted 后移除 message listener；`authenticate` 成功后又移除 abort listener，后续 close/error 只会 abort，不能关闭 AgentClient transport。 |
| P1 | 系统 Browser 启动失败不进入 Coordinator cleanup | 属实 | opener 在 `spawn` 后立即返回成功，没有监听异步 `error`；命令不存在时 Promise 已无法向调用方表达失败，子进程还可能产生未处理的 error event。 |
| P1 | 第二次 handoff 在 opening 阶段重置 TUI Controller | 属实 | 第一次归还后的 React key 为 `1`，第二次 opening 被改回 `0`；此时 `tuiLocked=false`，本应继续使用当前 Controller，却发生卸载重建并丢失当前 Thread/界面状态。 |
| P2 | `ControlStatus.holder.attachment_id` 被生成成可选字段 | 属实 | ZC-101 方案定义该字段固定存在，owner 使用 `null`；Schema 的 `required` 漏掉该字段，生成的 TS 使用 `?`、Python 使用 `NotRequired`。Python 当前运行时总会输出字段，因此这是契约缺陷，不是当前序列化缺失。 |
| 交付 | 故障排查提示复制终端 URL | 属实 | 当前 `/web` 只显示不含 URL 的启动 notice，设计也明确禁止把携带 token 的 URL 写入 notice；文档给出了无法执行且不安全的操作。 |
| 交付 | ZC-102 无证据且仍在进行中 | 状态属实，但不是额外代码缺陷 | 这表示任务尚未完成，整改结束前应继续保持进行中。 |
| 交付 | ZC-101 缺少 OCR 证据 | 证据缺口属实 | 现有任务证据没有 OCR。它不推翻已经通过的 Host 定向测试，但按当前协作规范，涉及 ZC-101 的整改完成后必须补做限定范围的检视并记录证据。 |

回检还确认：现有真实 loopback Coordinator 测试可以通过，但它只模拟 lifecycle 和 Host status，没有加载 Browser bundle，也没有挂载 `WebAwareRoot`，所以无法发现上述两个 P0。

## 整改目标与关键 invariant

- Bun 页面与 Python Agent attachment 是两个独立 loopback 服务，端口允许不同；安全边界是协议、精确主机、显式端口和 Host 的 Origin/token 校验，不是端口相等。
- lifecycle 从页面创建到页面终止始终只有一个资源监督者。`shutdown`、close、error、`pagehide` 或 bootstrap 失败任一发生，都要幂等关闭 lifecycle socket 和 Agent socket/AgentClient。
- React 只能调用保留 Coordinator 接收者的 wrapper，不能把依赖 `this` 的方法引用直接交给 hook。
- `opening` 阶段 Host 尚未确认 Web holder，现有 TUI Controller 必须保持挂载；只有 `tuiLocked=true` 才卸载。每次 owner 恢复后，下一代 Controller 只重建一次。
- Browser opener 的 Promise 必须真实表达“系统已成功创建启动进程”或“启动失败”，不能先报告成功再异步报错。
- `ControlStatus.holder.attachment_id` 在所有合法 wire 结果中固定存在：owner 为 `null`，attached/revoking 按 Host 状态返回对应 attachment ID。
- attachment endpoint、token、完整 handoff URL 不进入 snapshot、notice、日志、测试失败文本或用户文档。

## Interface 与错误语义

### 1. Agent endpoint 校验

将无 DOM 的校验收窄为对 endpoint 本身负责，不再接收或比较页面端口：

```ts
validateAgentEndpoint(endpoint: string): boolean
```

合法 endpoint 必须同时满足：

- `protocol === "ws:"`；当前不引入本机 TLS。
- `hostname === "127.0.0.1"`，不接受 `localhost`、IPv6、通配地址或其他主机。
- 使用显式有效端口；页面端口和 Agent 端口可以不同。
- 不包含 username、password、query 或 fragment；path 保持 Host 当前 canonical 根路径。

非法 endpoint 继续在任何 socket 创建前终止 bootstrap，只显示脱敏错误。不能通过把 Python attachment 代理到 Bun server、让两个服务争用同一端口或放宽到任意 URL 来规避问题。

### 2. Browser 连接监督者

从 `web/app.ts` 的顶层流程中提取一个无 DOM、可注入 fake WebSocket 的内部 `BrowserConnectionSupervisor`。名称可以按邻近实现调整，但职责必须集中：

```ts
type BrowserConnectionSupervisor = {
  readonly signal: AbortSignal
  waitForAccepted(): Promise<void>
  bindAgent(closeAgent: () => void): void
  abort(reason: string): void
  dispose(): void
}
```

- lifecycle message listener 在整个 handoff 期间保留；`accepted` 只完成一次等待，不能移除对后续 `shutdown` 的处理。
- `bindAgent` 注册长期关闭动作；若 bind 时已经 aborted，立即关闭 Agent 资源。
- Agent 完成认证后，页面必须保存可关闭的 `AgentClient` 或 transport，并由 supervisor 在 abort 时调用一次 `close/destroy`；不能只关闭认证中的原始 socket。
- `abort` 幂等：第一次记录脱敏原因并关闭两个连接，重复 close/error/pagehide 不产生第二次异常或资源操作。
- `dispose` 只移除页面事件监听；不能在 Agent 仍存活时静默丢弃关闭能力。
- 收到 `shutdown` 后，无论处于 accepted 前、认证中、initialize、acquire、历史加载、ready 后还是 active Run，都走同一个 abort 路径。

页面正常发送 `released` 后仍等待 Coordinator 的 `shutdown/close` 完成最终收敛；不要重新引入 Browser 自己判断 owner 已恢复的解锁逻辑。

### 3. TUI 外部 store 与 Controller generation

`WebAwareRoot` 必须始终调用同一组 React hooks，并通过闭包保留 Coordinator：

```ts
subscribe = listener => coordinator?.subscribe(listener) ?? noOpUnsubscribe
getSnapshot = () => coordinator?.getSnapshot() ?? disabledSnapshot
```

不得再传递 `coordinator.subscribe` 或 `coordinator.getSnapshot` 裸方法。wrapper 应使用 `useCallback` 或等价稳定引用，避免每次 render 重新订阅。

`handoffVersion` 改为所有 `WebHandoffSnapshot` 分支共有的只读字段，而不是只存在于 `idle`：

- 初始值为 `0`。
- `idle → opening → active/returning` 期间保持不变。
- 只有确认 owner 恢复、即将发布新 `idle` 时递增一次。
- `Za38Tui` 的 key 始终使用当前 `handoffVersion`，不能因 phase 改回常量 `0`。

这样第一次 handoff 的 opening 使用 key `0`，归还后使用 key `1`；第二次 opening 仍使用 key `1`，直到 Web 真正接管才因 `tuiLocked` 卸载，第二次归还后再以 key `2` 重建。

### 4. Browser opener

opener 只等待进程是否成功 spawn，不等待浏览器进程退出：

```text
注册 spawn/error listener
  → 成功 spawn：unref 并 resolve
  → 同步抛错或异步 error：reject
  → Coordinator 捕获 reject，执行 opener-failed cleanup
```

为测试注入最小 `spawn` adapter 或等价 seam，不能依赖开发机恰好安装了 `open` / `xdg-open`。错误对用户只显示脱敏摘要，不包含完整 handoff URL。

### 5. Protocol 固定字段

在 canonical `packages/protocol/schema/v3.json` 的 `controlHolder.required` 中加入 `attachment_id`，随后只通过生成脚本同步：

- `packages/protocol/src/generated.ts`
- `packages/agent/harness_agent/protocol/generated.py`
- `packages/agent/harness_agent/protocol/protocol_v3.json`
- contract fixture、hash 及生成器维护的其他产物

不保留“缺失等同于 null”的 fallback。缺少字段必须由 TS/Python contract 校验共同拒绝；owner 的合法结果显式携带 `attachment_id: null`。

## 按依赖排序的实施步骤

1. **先补失败回归，锁定边界。** 给不同页面/Agent 端口、未绑定 store 调用、accepted 后 shutdown、已认证 Agent abort、opener 异步 error、第二次 opening key 和缺失 `attachment_id` 分别建立会失败的定向测试。为什么：现有绿灯主要覆盖 Coordinator 内部状态，不能证明真实 Browser/TUI 调用方式。验证：每个测试在对应修复前能稳定命中原缺陷，而不是因超时或随机端口偶发失败。
2. **收紧 ZC-101 Protocol 契约。** 修改 canonical Schema 的 required 字段并重新生成全部双端产物，增加 owner-null、attached-string 和 missing-field 三类 fixture。为什么：ZC-102 需要用稳定 attachment ID 确认 holder，不能让调用方把缺失字段误当成合法状态。验证：`protocol:check` 通过，TS/Python 对缺失字段都拒绝。
3. **修复 Browser endpoint 校验。** 删除页面端口参数和相等判断，保留精确 loopback、协议、端口及 URL 形状校验。为什么：Bun 与 Python 独立监听是既定架构。验证：不同随机端口通过，非 loopback、非 `ws:`、无显式端口和带凭据的 URL 均失败。
4. **统一 Browser 连接生命周期。** 引入 supervisor，持续消费 lifecycle shutdown，并让 AbortController 在认证前后都能关闭 AgentClient/transport。为什么：Host revoke 失败或延迟时，页面也必须本地 fail closed。验证：在 bootstrap 每个阶段注入 shutdown/close/pagehide，都只关闭一次且不再发送 RPC。
5. **修复 TUI store adapter 与 generation。** 用稳定 wrapper 接入 `useSyncExternalStore`，把 `handoffVersion` 放入所有 snapshot，并让 key 全程使用它。为什么：既修复首屏 `this` 崩溃，也避免第二次 opening 丢失仍在使用的 Controller。验证：实际挂载 `WebAwareRoot`，走完两轮 opening/active/idle；opening 不重建，active 卸载，owner 恢复各重建一次。
6. **修复系统 Browser opener。** 监听 `spawn/error` 并通过 Promise 传播结果，使用注入 seam 测试 macOS、Windows、Linux 参数及 ENOENT。为什么：Coordinator 只有收到 reject 才能撤销 attachment。验证：异步 error 进入 `opener-failed`，attachment 只 revoke 一次，错误中不含 URL/token。
7. **修正文档和可观察错误。** 删除“复制终端 notice URL”的指引，改为检查系统 opener、查看脱敏错误并重新执行 `/web`；明确产品不输出含凭据的手工打开 URL。为什么：当前指引既不可执行，也违背凭据不落日志原则。验证：全文搜索不再宣称 notice 含 URL，现有“不泄露 URL”测试继续通过。
8. **完成交付闭环。** 保持 ZC-102 为进行中，直到定向测试、项目级检查和限定范围 OCR 全部通过；对 ZC-101 的原提交及本次 Protocol 整改做限定范围 OCR，对 ZC-102 的最终 diff/commit 单独做 OCR，不混入用户无关改动。修复所有高/中优先级问题后，把命令、范围、结果和版本影响分别写入两个任务证据，再按任务脚本同步状态。

## 测试与验证方案

### 定向自动化测试

| 测试层 | 必须覆盖的可观察结果 |
| --- | --- |
| Bootstrap URL | `http://127.0.0.1:页面端口` 携带 `ws://127.0.0.1:另一端口` 时通过；外部主机、错误协议、无端口、凭据/query/hash 被拒绝。 |
| Browser supervisor | shutdown/close/error/pagehide 分别发生在 accepted 前、认证中、认证后、acquire 后和 ready 后时，lifecycle 与 Agent 都关闭一次；accepted 后的 shutdown 不会被忽略。 |
| Browser opener | fake child 发 `spawn` 时 resolve，发异步 `error` 或同步抛错时 reject；Coordinator 进入统一 cleanup。 |
| TUI root | Coordinator 存在时订阅和读 snapshot 不抛错；无 Coordinator 时 hook 顺序稳定；两轮 handoff 中 opening 保留 Controller，active 卸载，idle 各恢复一次正确 Thread。 |
| Protocol contract | owner/null 与 attached/string 合法；缺少 `attachment_id`、错误类型和多余字段在 TS/Python 两端得到一致结果。 |
| Loopback integration | 真实 Bun server 与 lifecycle 继续完成 accepted → ready → active → released；新增测试必须解析实际打开 URL，确认页面端口和 Agent endpoint 端口无需相同。 |

Browser bundle 和 `WebAwareRoot` 至少各有一条直接覆盖其真实调用方式的测试，不能只测试辅助函数或手工构造 Coordinator snapshot。若现有测试设施无法挂载 React 根层，应先增加小型测试 adapter，不以手工验证替代 P0 回归。

### 项目级检查

整改完成后至少执行：

```bash
bun run protocol:generate
bun run protocol:check
cd packages/cli && bun test
cd packages/agent && .venv/bin/python -m pytest -q tests/host/test_control_lease.py tests/host/test_agent_host.py tests/protocol/test_protocol_contract.py
cd ../.. && bun run build
bun run typecheck
bun run project:check
bun run test
```

真实 loopback 测试必须在允许监听 `127.0.0.1` 随机端口的环境运行；沙箱禁止监听时应明确记录环境限制，并在可监听环境补跑，不能把 `EADDRINUSE` 当作实现通过证据。

## 可观察验收

- 从空首页和已有 Thread 执行 `/web`，Browser 页面可以在 Agent 与页面端口不同的正常环境完成 bootstrap。
- 交互式 TUI 挂载 Coordinator 时不抛 `this` 相关异常。
- lifecycle 在 accepted 后收到 shutdown，或页面关闭/刷新时，Browser Agent socket 立即关闭；TUI 仍等待 Host owner 确认后才解锁。
- 系统缺少 `open` / `xdg-open` 时，用户看到脱敏启动失败，attachment 被撤销，不遗留随机监听或未处理子进程异常。
- 完成一次 handoff 后再次执行 `/web`，opening 阶段保留当前 Thread、Timeline 和可输入状态，不发生无理由重建。
- 所有合法 `ControlStatus` 都显式包含 `holder.attachment_id`；owner 为 `null`，缺失字段无法通过任一端 contract 校验。
- 故障排查不再要求用户复制不存在的 URL，也不建议暴露 attachment token。
- ZC-101 与 ZC-102 各自拥有范围明确的 OCR 结论、测试证据和版本影响记录；ZC-102 在这些条件满足前不得标记完成。

## 非范围

- 不实现 ZC-104 的完整 React Web UI、Thread/模型/Skill/MCP 页面或 Slash Command 对齐。
- 不新增反向代理，不合并 Bun server 与 Python attachment listener，不改变 ZC-101 的 Origin/token 认证模型。
- 不为缺失 `attachment_id` 保留 alias、optional 类型、默认 null 或旧数据 fallback。
- 不把完整 handoff URL 重新输出到终端、日志或 handoff 文档作为 Browser opener 的降级方案。
- 不借整改重构无关 TUI Controller、Agent Host Run 语义或持久化结构。
