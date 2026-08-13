# HC-102：单实例 Web Handoff 与 TUI 接管生命周期

原始需求：[HC-102](../task/HC-102-实现单实例WebHandoff.md)  
前置方案：[HC-101：Host 控制租约与可撤销 Web Attachment](../task/archive/HC-101-Thread规范记录与Plug.md)  
架构依据：[ADR 0002：Project-scoped Agent Host、多个 Connection 与单 Run owner](../architecture/adr/0002-project-host-multi-connection.md)

## 通俗说明

HC-101 已让 Python Host 知道当前应由 TUI 还是 Web 接受输入，但 CLI 还没有管理“一次 Web 接管”的完整生命周期。现在的 `WebLauncher` 只启动静态页面并签发 attachment，TUI 继续工作，浏览器关闭、第二窗口、ready 超时和页面初始化失败也没有统一收敛路径。

本任务在 CLI 中新增深模块 `WebHandoffCoordinator`。Handoff 表示一次“TUI 将控制权交给 Web，Web 使用后再归还 TUI”的临时接管过程。该模块只协调以下事实：

- 当前是否存在一个 Web handoff；
- 哪个 Browser lifecycle Connection 被接受；
- Browser 是否已经取得 Host 控制权并完成初始化；
- 当前 Web 对应的 Thread 是具体 ID 还是空首页；
- 异常关闭后是否已经撤销 attachment，并确认 Host holder 回到 owner。

它不代理 Agent JSON-RPC，不保存聊天 Timeline，不处理 approval/question，也不实现完整 Web UI。Browser 仍通过 HC-101 的 loopback WebSocket 直连 Python Host。

```text
TUI /web
  → WebHandoffCoordinator 创建 handoff 和 attachment
  → 打开受限本机页面
  → 第一个 Browser lifecycle Connection 被 accepted
  → Browser 认证 Agent attachment、initialize、host.control.acquire
  → Browser ready
  → CLI 查询 Host，确认 matching attached holder
  → TUI 卸载交互 Controller，只显示接管页
  → Browser release/关闭/超时
  → CLI revoke attachment，等待 Host owner
  → TUI 按 nullable Thread 恢复
```

## 已确认现状

- `WebLauncher.open(threadId)` 必须接收现有 Thread，先创建 owner `ThreadWatch`，再签发 attachment 和打开 `/#fragment` 页面。
- 静态服务只区分 `/`、`/index.html` 与 `/app.js`，没有 handoff path、精确 Host/Origin 校验、资源白名单和 Browser lifecycle WebSocket。
- Browser `app.ts` 解析 fragment 后立即认证 Agent attachment，但没有在 bootstrap 最早阶段安装统一 abort/close handler。
- Browser 请求 `run.multithread`，没有请求或调用 `host.control.acquire/release`；HC-101 完成后，其受控操作会被 Host 拒绝。
- Browser 页面要求初始 Thread，使用 `threads.watch` 作为唯一渲染源，无法从空首页创建第一条 Run。
- `/web` Registry 要求 `requiresThread`；CommandResult 与 `openWeb` callback 的 `threadId` 也都是非空字符串。
- TUI Controller 只在 `openWeb` 返回后追加 URL notice，没有接管状态；OpenTUI 根组件始终渲染聊天页并保持 Agent event/Interaction 订阅。
- CLI 当前关闭顺序是 `runTui → WebLauncher.close → AgentClient/sidecar close`，可以继续作为新的总体资源顺序。
- HC-101 已提供 `host.attachment.create/revoke`、`host.control.acquire/release/status`、稳定 `attachment_id` 和显式 attached capability ceiling；HC-102 不修改这些语义。

## 设计原则与关键 invariant

- 一个 CLI 进程最多一个非 idle Web handoff；重复 `/web` 不签发第二个 attachment，也不打开第二个 Browser。
- Web handoff 是 CLI 临时接管生命周期，不是 Harness 持久化领域对象，不写入 SQLite、Thread 或 Protocol v3；仓库领域模型仍只有 project、thread 与 message。
- Browser 在 lifecycle `accepted` 前不得认证 Agent attachment；第二个 lifecycle Connection 在 Agent 认证前被拒绝。
- TUI 是否可输入只以 owner Connection 查询到的 Host control status 为准，不相信 Browser 自报 `ready/released`。
- TUI 只在 Host 确认 matching attached holder 后锁定；锁定期间不保留聊天 Controller 的 Agent event/Interaction 订阅，也不镜像 Web 对话。
- TUI 只有在 Host 确认 holder 已恢复 owner 后解锁；超时或查询失败时保留接管/回收页，不展示可输入 composer。
- Browser 当前 Thread 始终是 `string | null`；`null` 明确表示空首页，不使用 `undefined` 表示业务状态。
- 正常 release、标签页关闭、刷新、bootstrap 失败、ready timeout、第二窗口和 CLI close 均通过同一个 attachment revoke/owner 恢复路径。
- lifecycle 消息是 Browser 与本机 CLI server 的内部契约，不放入 `packages/protocol`，也不创建第二套 Agent RPC。
- URL、snapshot、notice 和日志不得包含 attachment token；token 只短暂存在于 URL fragment 和 Browser bootstrap 内存。

## WebHandoffCoordinator interface

新增 `packages/cli/src/web/handoff-coordinator.ts`。对 TUI/main 和 Bun server 暴露以下小 interface：

```ts
type WebHandoffCoordinator = {
  open(initialThreadId: string | null): Promise<void>
  getSnapshot(): WebHandoffSnapshot
  subscribe(listener: (snapshot: WebHandoffSnapshot) => void): () => void
  attachLifecycle(handoffId: string, channel: LifecycleChannel): Promise<void>
  close(): Promise<void>
}
```

- `open` 只负责创建并启动一次 handoff；Promise 在静态服务、attachment 和系统 Browser 启动成功后完成，不等待 Browser ready。
- `attachLifecycle` 消费一个已经通过 Bun WebSocket upgrade 的本地 channel，并在 module 内完成首连接竞争、消息校验和断线收敛。
- `close` 是 CLI 终态关闭，可重复调用；调用后不再允许 `open`。
- 业务调用方不直接接触 Bun Server、WebSocket、attachment token、timer 或子进程。

`LifecycleChannel` 是 Bun WebSocket adapter 与内存测试 adapter 共用的内部 seam：

```ts
type LifecycleChannel = {
  readonly messages: AsyncIterable<unknown>
  send(message: LifecycleServerMessage): Promise<void>
  close(code: number, reason: string): Promise<void>
}
```

### Snapshot

`WebHandoffSnapshot` 是不包含凭据的只读 discriminated union：

```ts
type WebHandoffSnapshot =
  | {
      phase: "idle"
      tuiLocked: false
      restoreThreadId: string | null
      handoffVersion: number
      error?: string
    }
  | {
      phase: "opening"
      tuiLocked: false
      handoffId: string
      threadId: string | null
    }
  | {
      phase: "active"
      tuiLocked: true
      handoffId: string
      threadId: string | null
    }
  | {
      phase: "returning"
      tuiLocked: boolean
      handoffId: string
      threadId: string | null
      reason: WebReturnReason
      error?: string
    }
```

- 初始 `idle.handoffVersion` 为 `0`。每次 owner 恢复完成后递增一次，TUI 用它区分“初始 idle”和“从 Web 返回”。
- `restoreThreadId` 始终存在；`null` 表示重建空首页。
- `opening` 阶段 TUI 仍可输入，因为 Host 尚未确认 Web holder。若 owner 在此时启动 Run，Web acquire 会由 Host 可靠地返回 `CONTROL_BUSY`，Coordinator 再撤销 handoff。
- `active` 只在 owner 查询 `host.control.status` 并确认 `state=attached`、`holder.attachment_id` 等于本 handoff attachment 后发布。
- `returning.tuiLocked` 保留进入回收前的锁定事实；已进入 active 的 handoff 在 owner 确认前始终为 `true`。未完成 acquire 的 opening 失败不伪造 Web holder。
- owner 恢复查询失败时保持 `returning` 并写入脱敏 `error`，不得切回 `idle`。

### 状态机

```text
idle
  └─ open → opening
       ├─ accepted + acquire + ready + Host matching holder → active
       ├─ opener/bootstrap/ready timeout/invalid lifecycle → returning
       └─ CLI close → returning

active
  ├─ released → returning
  ├─ lifecycle close / refresh / pagehide → returning
  └─ CLI close → returning

returning
  ├─ revoke + Host owner confirmed → idle
  └─ recovery failed → returning(error)
```

任何路径都不能从 `active` 直接进入 `idle`。旧 handoff 进入 `returning` 后不再接受新的 lifecycle Connection 或消息。

## Browser lifecycle 契约

Lifecycle WebSocket 固定使用当前页面 Origin 和路径：

```text
ws://127.0.0.1:<port>/web/h/<handoff-id>/lifecycle
```

handoff ID 使用 `crypto.randomBytes(32)` 生成 256-bit base64url 字符串。它用于路由和竞争识别，不替代 attachment token。

### Server → Browser

```ts
type LifecycleServerMessage =
  | { type: "accepted" }
  | {
      type: "shutdown"
      reason:
        | "already-open"
        | "invalid-handoff"
        | "invalid-message"
        | "ready-timeout"
        | "returning"
        | "host-control-failed"
        | "cli-exit"
    }
```

### Browser → Server

```ts
type LifecycleBrowserMessage =
  | { type: "ready" }
  | { type: "thread.changed"; thread_id: string | null }
  | { type: "released" }
```

规则如下：

- 只接受 UTF-8 JSON 文本帧，单帧上限 16 KiB；对象必须精确匹配类型，未知字段、二进制帧和超限帧均视为 `invalid-message`。
- 第一个匹配 active handoff 的 channel 原子成为 primary，收到 `accepted`。后续 channel 收到 `shutdown: already-open` 并以 policy code 关闭，不能影响 primary。
- `ready` 只能由 primary 在 opening 阶段发送。Browser 必须先完成 Agent auth、initialize、`host.control.acquire` 和初始 Thread 加载，再发送 `ready`。
- Coordinator 收到 `ready` 后通过 owner AgentClient 调用 `host.control.status`；只有 matching attachment holder 才进入 active。Browser 自报 ready 不构成 TUI 锁定依据。
- `thread.changed` 只在 active 阶段有效。字符串必须非空且不超过 256 字符；相同值重复上报幂等，后到值覆盖旧值；`null` 明确清空当前 Thread。
- `released` 只能在 active 阶段发送，并且 Browser 必须先等待 `host.control.release` 成功返回 owner status。Coordinator 仍会 revoke attachment 并独立查询 owner。
- active 前 primary close、消息乱序、畸形消息或异常结束都进入统一 cleanup。重复 `ready` 可幂等忽略；returning 阶段的重复 `released`/close 不启动第二次 cleanup。
- 页面刷新等同 primary lifecycle close：旧 handoff 被回收，新页面无法复用旧 attachment 或恢复 active Run。

## 静态服务与 Browser 打开

旧 `WebLauncher` 不保留为只透传 Coordinator 的浅 wrapper。实现拆为：

- `WebHandoffCoordinator`：拥有状态、timeout、attachment、Host control 和资源收敛。
- Bun Web server adapter：只处理 HTTP 路由、headers、WebSocket upgrade 和 `LifecycleChannel` 映射。
- Browser opener adapter：只封装 macOS `open`、Windows `cmd /c start`、Linux `xdg-open`；测试注入 fake opener。

静态 server 在第一次 `open` 时惰性启动，绑定 `127.0.0.1` 随机端口，并在 Coordinator `close` 前复用。每次 handoff 使用新的 path；旧 handoff path 在返回 idle 后立即变为 404。

### 路由白名单

| Method/Path | 行为 |
| --- | --- |
| `GET /web/h/<active-handoff-id>` | 返回 HTML Shell |
| `GET /web/app.js` | 返回已构建 Browser bundle |
| `WS /web/h/<active-handoff-id>/lifecycle` | lifecycle upgrade |
| 其他 path/handoff | `404` |
| 非 GET/upgrade | `405` |

- 不再提供 `/` 或 `/index.html` fallback。
- 路由只比较解析后的精确 pathname，不读取请求 path 指定的文件，不存在目录遍历或任意静态文件读取。
- HTTP `Host` 必须精确等于 `127.0.0.1:<server-port>`；lifecycle upgrade 的 `Origin` 还必须精确等于当前 server origin。
- HTML 和 JS 均返回 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`、`Cross-Origin-Resource-Policy: same-origin`。
- HTML 额外返回 `Cross-Origin-Opener-Policy: same-origin` 和以下 CSP：

```text
default-src 'none';
script-src 'self';
style-src 'unsafe-inline';
connect-src ws://127.0.0.1:*;
base-uri 'none';
form-action 'none';
frame-ancestors 'none'
```

### Bootstrap URL

Browser URL 固定为：

```text
http://127.0.0.1:<port>/web/h/<handoff-id>
  #endpoint=<agent-ws>
  &token=<attachment-token>
  &attachment=<attachment-id>
  [&thread=<thread-id>]
```

- `thread` 缺失表示 `null`，其余 fragment 字段必须存在且非空。
- URL 不写入 TUI notice、日志或 Coordinator snapshot；`open` 只返回成功/失败。
- Browser 在执行任何 socket 操作前同步解析 fragment 并调用 `history.replaceState` 清除全部 hash。
- Browser 校验 Agent endpoint 必须是 `ws://127.0.0.1:<port>`，防止被篡改 fragment 引导连接其他主机。

## Browser bootstrap 与最小页面行为

Browser bootstrap 的固定顺序为：

```text
解析并清除 fragment
  → 注册 pagehide、lifecycle close/error 和统一 AbortController
  → 连接 lifecycle
  → 等待 accepted
  → 认证 Agent attachment
  → initialize（请求 host.control，不请求 host.attach/run.multithread）
  → host.control.acquire
  → 校验返回 holder.attachment_id
  → 有初始 Thread：threads.open；无 Thread：渲染空首页
  → 安装页面 handler
  → lifecycle ready
  → 启用 composer
```

- 在收到 `accepted` 前不得创建 Agent WebSocket。
- lifecycle 的 `shutdown`、close/error 或 `pagehide` 在任一 bootstrap 阶段触发同一个 AbortController；认证中和已建立的 Agent socket 都必须立即关闭。
- Browser initialize 至少请求 `host.control`、`run.cancel`、`threads.read` 及当前简单页面使用的只读能力，显式不请求 `host.attach` 和 `run.multithread`。
- 当前页面从 owner `ThreadWatch` 迁移为：初始历史用 `threads.open`，当前 Connection 自己的 Run 使用 `AgentRun.events`。CLI 不再为 Web 创建 owner watch，避免 TUI 镜像 Web 对话。
- 空首页第一条消息调用 `AgentClient.startRun` 且不传 `threadId`。`run.start` accepted 后，把 `run.ref.threadId` 保存为当前 Thread，并发送 `thread.changed`。
- 页面增加“返回 TUI”操作。只有没有 active Run 和 pending Interaction 时才调用 `host.control.release`；成功并确认 owner status 后发送 `released`。`CONTROL_RELEASE_BLOCKED` 只展示错误，不发送 `released`。
- 标签页关闭、刷新或 Browser 崩溃不尝试依赖异步 release；只关闭 lifecycle/Agent socket，由 CLI revoke attachment 并取消未收敛 Web Run。
- HC-102 只维持当前简单 composer、历史和流式展示所需的最小行为；完整 Thread、模型、Skill、MCP、Slash Command 和 React presentation 留给 HC-104。

## TUI 接管与恢复

Web lifecycle 不进入 `TuiController` 的业务状态。`runTui` 的 OpenTUI 根层订阅 `WebHandoffCoordinator` snapshot，并在交互 TUI 与静态接管页之间切换：

```text
tuiLocked=false
  → 挂载正常 TUI Controller

tuiLocked=true
  → 卸载 TUI Controller
  → 清除其 Agent event / Interaction 订阅
  → 只渲染 WebTakeoverView

owner confirmed + handoffVersion 递增
  → 重新创建 TUI Controller
  → restoreThreadId 为 string：调用 canonical threads.open
  → restoreThreadId 为 null：进入空首页
```

- `opening` 阶段继续显示正常 TUI；`active` 和已锁定的 `returning` 只显示静态接管/回收状态，不显示 Web Timeline、composer、picker 或 Interaction。
- `WebTakeoverView` 只显示 phase、当前 Thread 是否为空以及“在浏览器返回或关闭页面”的提示，不展示内部 Thread ID、handoff ID、URL、endpoint、token 或 attachment ID。
- 锁定期间 TUI Controller 被卸载，因此不会保留 global Agent event listener 或 owner Interaction handler。HC-101 保证 acquire 前 owner 没有 active Run/Interaction，卸载不会遗弃 owner 工作。
- `TuiControllerOptions` 增加一次性 `initialThreadId: string | null` 恢复输入；恢复实现复用现有 `threads.open` 和模型绑定读取逻辑，不通过 Thread picker，也不保留旧 Timeline。
- owner 恢复后 `threads.open` 失败时进入空首页并显示脱敏恢复错误，不回退到接管前的陈旧 Thread。
- `/web` Registry 去掉 `requiresThread`，仍要求 `host.attach`、`host.control` 和 idle；CommandResult/`openWeb` callback 改为传递 `string | null`。
- interactive CLI 初始化时同时请求 `Capability.HOST_CONTROL`；缺少 `host.attach` 或 `host.control` 时隐藏 `/web`。
- 接管页保留 CLI 终止快捷路径；退出会进入 Coordinator `close`，不能直接销毁 owner AgentClient 而跳过 Web cleanup。

## Timeout、恢复与关闭顺序

### Ready timeout

- 从系统 Browser 成功启动后开始 65 秒 ready deadline，覆盖 attachment 60 秒 TTL 和 5 秒收敛余量。
- deadline 前未进入 active，Coordinator 向 primary 发送 `shutdown: ready-timeout`，关闭 lifecycle，revoke attachment，再等待 owner。
- timeout 使用可注入 clock/scheduler，单元测试不得真实等待 65 秒。

### Owner 恢复

所有 returning 路径共享一个 single-flight cleanup：

```text
标记 returning，拒绝新 lifecycle/message
  → 向 primary 发送 shutdown（若仍连接）
  → host.attachment.revoke(attachment_id)
  → 使用 revoke result.control 或 host.control.status 检查 holder
  → 非 owner：每 100 ms 轮询，最多 30 秒
  → owner：关闭 primary，销毁 handoff 凭据，发布 idle
```

- 正常 release 后仍执行 revoke，确保已连接 attachment 和 token 不能被复用。
- revoke RPC 超时或失败时仍尝试 `host.control.status`；只有 status 明确为 owner 才解锁。
- 30 秒后仍无法确认 owner，snapshot 保持 `returning(error)`。用户只能重试关闭或退出 CLI；不得冒险恢复 composer。
- cleanup、lifecycle close、ready timeout 和 `close` 并发时共享同一个 Promise，attachment 只撤销一次。

### CLI close

固定资源顺序为：

```text
禁止新 Web handoff
  → lifecycle shutdown: cli-exit
  → abort Browser bootstrap / 关闭 lifecycle channel
  → revoke attachment并尽力确认 owner
  → 停止 Bun static server
  → 卸载 TUI root/controller
  → destroy owner AgentClient
  → 关闭 Python Host/sidecar
```

Coordinator `close` 可重复调用，单项失败记录脱敏错误但继续释放后续资源。CLI 终态关闭不因 owner 恢复超时而永久挂起。

## 实施步骤

1. 定义 `WebHandoffSnapshot`、lifecycle message guards、`LifecycleChannel` 和 `WebHandoffCoordinator` interface；先用 fake Host control、fake lifecycle 和 fake clock 建立状态机测试。
2. 新增 Bun server adapter 和 Browser opener adapter，迁移 `browserBundle`；使用 handoff path、精确 Host/Origin、路由白名单和安全 headers。删除旧 `WebLauncher` class，不保留透传 wrapper。
3. 接入 HC-101 Host interface：owner 创建/revoke attachment并查询 status；Browser initialize/acquire/release；interactive CLI 请求 `host.control`。
4. 重写 Browser bootstrap 的 fragment 清理、lifecycle-first、AbortController 和 Agent socket 关闭顺序；移除 `run.multithread` 和 owner `ThreadWatch` 路径。
5. 让简单 Web 页面支持 nullable Thread、第一条 Run 创建 Thread、`thread.changed` 和正常返回 TUI；不扩展 HC-104 的完整功能。
6. 改造 `/web` 命令和 OpenTUI 根层：空首页可调用，Host 确认后卸载 TUI Controller并渲染静态接管页，owner 恢复后按 `string | null` 重建。
7. 调整 CLI main 的创建和关闭顺序，使 Coordinator 生命周期覆盖 runTui，并确保退出时先回收 Browser/attachment，再关闭 owner AgentClient 与 sidecar。
8. 更新用户交互/故障排查、架构总览和 ADR 0002，说明单窗口、返回、刷新/关闭、超时与恢复语义；不描述尚未实现的 Web parity。

## 测试与可观察验收

### Coordinator interface

- 初始 idle、open 后 opening、重复 open 拒绝且不创建第二个 attachment。
- 第一个 lifecycle accepted；第二个收到 `already-open` 且不影响 primary。
- Browser ready 但 Host status 不是 matching attachment 时不得 active，并进入 cleanup。
- matching holder 后 snapshot 才 `active/tuiLocked=true`。
- `thread.changed` 能在具体 ID 与 `null` 间切换，返回后 `restoreThreadId` 保留最终值且 `handoffVersion` 只递增一次。
- release、primary close、ready timeout、invalid message 和 CLI close 都进入同一个 cleanup，revoke 只调用一次。
- owner status 未确认时保持锁定 returning；确认后才 idle。

### Static server 与 lifecycle

- 真实 loopback server 只接受白名单路由和 active handoff ID；旧/未知 handoff、路径遍历、额外资源和非 GET 请求被拒绝。
- 错误 Host、错误 Origin、二进制/超限/畸形 lifecycle frame 被拒绝。
- HTML/JS 的 CSP、no-store、no-referrer、nosniff、same-origin headers 精确存在。
- Browser opener 失败时 attachment 被撤销，server 可供下一次 handoff 复用。

### Browser bootstrap

- fragment 在任何 socket 创建前被清除；缺字段、非 loopback endpoint 和非法 Thread 失败关闭。
- lifecycle accepted 前不创建 Agent socket。
- accepted 后任意阶段 lifecycle shutdown/close 都 abort Agent auth、initialize、acquire 或初始 Thread 加载，并关闭 socket。
- acquire 成功且初始页面就绪后才发送 ready。
- 空首页第一条 Run accepted 后发送一次 `thread.changed`；返回按钮只在 release 成功后发送 `released`。
- active Run 下 release blocked 不解锁；tab close 由 CLI revoke 并取消 Run。

### TUI 与 CLI

- `/web` 在空首页和现有空闲 Thread 均可用；active Run/Interaction 或缺少 capability 时仍不可用。
- opening 时 TUI 保持可用；Host matching holder 后只显示静态接管页，TUI Controller 的 Agent/Interaction listener 已移除。
- owner 恢复后，具体 Thread 通过 `threads.open` 恢复最新历史；`null` 返回空首页，不恢复旧 Thread。
- CLI exit 的可观察关闭顺序为 lifecycle → attachment → server → owner AgentClient/sidecar，且不残留监听端口或 timer。

### 验证命令

```bash
cd packages/cli && bun test tests/web tests/tui/application/commands.test.ts tests/tui/application/controller.test.ts
bun run build
bun run typecheck
bun run test
bun run project:check
```

OpenTUI 接管页属于用户可见变更，交付 PR 需附空首页打开、active 接管和 returning 三种终端截图。真实 Browser 跨进程矩阵、双视口和视觉基线仍由 HC-105 完成。

## 非范围与交接

- 不实现 React DOM Web 工作台、完整 Thread 导航、模型/Skill/MCP 面板、完整 Slash Command 或视觉 parity；这些属于 HC-104。
- 不提取 Interactive Core、`InteractiveController` 或迁移 TUI 业务语义；这些属于 HC-103。HC-102 只在 OpenTUI 根层处理宿主接管。
- 不新增 Protocol v3 operation/capability/event，也不改变 HC-101 `ControlLease`、attachment 或 Run 语义。
- 不实现 active Run replay、页面刷新恢复、多 Browser 前台窗口、远程访问、daemon、登录或多用户。
- 不引入 Playwright；真实 Browser E2E、截图基线和完整安全矩阵属于 HC-105。
- 不新增第三方依赖；使用 Bun server/WebSocket、现有 AgentClient 和浏览器原生 API。
- 版本影响默认为“无根版本变更”；若执行 Thread 发现发布规则要求调整，只能通过 `bun run version:set`，并先修订任务与本方案。
