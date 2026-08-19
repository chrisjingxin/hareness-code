# HC-155 重做 Compose 流程规格

关联任务：[HC-155](../task/HC-155-重做Compose流程.md)  
被替代的现行实现：[HC-140](../task/archive/HC-140-重构组合工作模式.md) 及其 [Spec](HC-140-重构组合工作模式.md)  
架构入口：[Compose 工作模式](../architecture/Compose 工作模式.md)

用户结果与验收以 Task 为准。本文只规定行为、公开 interface、错误语义和 invariant。Plan / Todo 确认后撰写，路径与 Task 同名。

## 1. 通俗目标

Compose 仍是 Tab 里和 Build 平级的工作模式：先问清楚、写出可确认的文档，再写代码，最后用内置 Review Skill 检视这一次改过的代码。

测试不是进度条上的一格，也不问用户写测试命令。实现按 TDD 写；实现结束后 Runtime 不再另跑验收，同一轮进入检视。

和现在不同：用户看到的是**这一条对话在说话**，不是后台 Work Item 引擎先分类、再静默访谈、再走一条会炸的反向 RPC。退出再进，看磁盘上的文档和「哪几份已确认」，不要从头问。

成功时：`写一个 jsondiff CLI，附测试和 README` 在几秒内出现 Grill 或 `ask_user`；进度条能看出阶段；确认 Plan 后同一轮开始改文件，随后进入检视；不再出现 `Unknown JSON-RPC response id: compose-task-interview-*`。

## 2. 已确认假设

来自已确认 Task，Spec 不得改写：

1. 保留 Tab 切 Build / Compose；不增加 Build 里的 `/compose`。
2. 流程为 Grill → Task → Spec → Plan → 实现 → 检视。测试不是用户可见阶段。
3. 删除 Work Item 引擎。进度 = `docs/compose/<slug>/` 文档 + 确认记录。
4. Grill / 写文档在主对话流式进行。Implement 与检视使用同一 Thread 内的 fresh execution；检视只带已确认文档和本套改过的文件。
5. Grill 用主对话 `ask_user`。禁止 `compose-task-interview-*` 专用提问通道。
6. 各阶段用原版内置 Skill；**只把提问改成 `ask_user`，其余 Skill 正文不动。** 检视阶段加载内置 `code-review-and-quality`。不另建双轴 Reviewer 引擎。
7. 简单需求不审 Spec；复杂需求要审。简单/复杂由 Grill 写入 Task，确认 Task 时可改。
8. 每道确认通过后同一轮自动进入下一阶段，全程不用打「继续」。确认 Plan 后同一轮自动实现；实现收工后同一轮自动检视。
9. 不问用户写或确认 `verify_command`。实现结束后 Runtime 不再另跑测试命令。
10. 中途普通输入跟当前走。`/abandon` 只废弃并空闲（须确认、禁止带新目标）。`/new-work <目标>` 只换题（必须带目标）。完成后的下一句才不用命令开新需求。
11. 退出再进只续文档和确认，不续半句 token，不对账未知写操作。
12. TUI 只读进度为中文五段「需求 → 规格 → 计划 → 实现 → 检视」；可打开已有文档，不能跳阶段。
13. 旧内部实现直接拆，不留兼容层、双写、迁移。
14. Compose 模型不看用户 Skill 目录；短系统提示 + 当前阶段原版内置 Skill；`AGENTS.md` 保留；用户同名不覆盖内置。
15. MCP 按需：`tool_search` 命中后 reveal，不预注入全部 schema；Compose 强制 defer=on。
16. 检视写出 `review.md` 后用户确认才结束。「按意见改」退回实现。检视 Agent 不改产品代码。

本规格补的实现假设（与 Task 不冲突；若要改先回写 Task）：

- Thread 首条有效 Run 仍冻结 `build|compose`，普通交互不能互切。
- `/btw` 行为不变，Compose 进行中仍可用。
- `/abandon` 与 `/new-work` 都保留，定位见第 5 节；实现共用「放下当前 slug」，命令语义不重叠。
- 一套需求要求 `task.md` / `spec.md` / `plan.md` / `todo.md`；检视后有 `review.md`。`todo.md` 与 Plan 一起生成、一起确认，不另设门禁。不要 `report.md`。
- Web 与 TUI 共用同一份进度投影；本任务不单独做 Web 视觉改版。
- 测试命令由 Runtime 推断（实现过程刚跑过的测试命令、Plan 正文里的测试命令、仓库一眼能看出来的命令），只作内部验收，不写进确认门禁，不要求 `plan.md` front matter。
- 侧边栏 Skill / MCP 管理在 Compose 下仍可用，但不把用户 Skill 或全部 MCP schema 预注入模型。

## 3. 术语

```text
Thread
├─ thread_mode: build | compose
└─ ComposeSession          当前这一套需求的薄进度（每 Thread 最多一个未完成）
   ├─ slug                 docs/compose/<slug>/
   ├─ complexity           simple | complex（来自 Task，确认时可改）
   ├─ confirmations        task / spec / plan / review 的已确认正文 digest
   └─ status               active | waiting_user | completed | abandoned

Run
└─ 一次用户 Turn。Grill/Spec/Plan 在该 Run 的主 Agent 对话里进行。

Fresh implement execution
└─ 同一 Thread、另一次 Managed Agent 调用：空上下文 + 已确认文档 + 实现 Skill。
   不是新 Thread，也不是 Handoff。

Fresh review execution
└─ 同一 Thread、另一次 Managed Agent 调用：空上下文 + 已确认文档 + 本套改过的文件 + 内置 Review Skill。
   只允许写 review.md，禁止改产品代码。
```

不要再使用作为主路径身份的词：Work Item、Activity 账本、readiness 九项、TurnIntent 分类器。不要再把 `verify` / `fix_rounds` 当成用户可见阶段或完成条件。

## 4. 顶层流程

```text
run.start(mode=compose)
  → RunCoordinator 受理（owner / cancel / Interaction / sequence / 终态）
  → ComposeRunAdapter
       → ComposeSession.execute_turn
            1. 命令优先：
               /abandon（确认后）放下当前 slug，session 空闲，不开始新 Grill
               /new-work <目标> 放下当前 slug（若有），立刻用新目标 Grill
            2. 无分类器。按「当前阶段 + 会话是否已完成」路由普通消息
            3. 派生阶段（只读文档 + 确认，不跑模型）
            4. Grill / Spec / Plan：主 Agent 流式执行，注入当前阶段原版 Skill
               提问只能走 ask_user（Build 同一条 Interaction 通道）
            5. 文档就绪后 Runtime 用 ask_user 做确认门禁（确认 / 我要改）
               用户点「确认」后同一 Turn 立刻进入下一阶段，禁止再等「继续」
            6. Task 已确认 → 自动写 Spec
               简单：Spec 不审，同一 Turn 接着写 Plan+Todo
               复杂：等确认 Spec；确认后再同一 Turn 写 Plan+Todo
            7. Plan+Todo 已确认：同一 Turn 启动 fresh implement。
               实现 Agent 返回后同一 Turn 启动 fresh review，不再另跑验收命令。
            8. 检视 Agent 写出 review.md 后 Runtime 用 ask_user 问是否可接受。
               确认 → session completed。
               「按意见改」→ 不写确认，退回 implement（上下文带上 review.md）。
  → 每次阶段变化发 compose.progress
  → RunCoordinator 唯一终态
```

阶段派生（代码，不是模型）：

```text
无当前 slug 或无 task.md          → grill
task 未确认                      → grill（继续）或等待确认 Task
task 已确认且 complex 且 spec 未确认 → spec，然后等待确认 Spec
否则且 plan 未确认               → plan，然后等待确认 Plan
plan 已确认且实现未收工          → implement
  内部验收失败                   → 停在 implement（失败）
实现已收工且 review 未确认       → review，然后等待确认 Review
review 已确认                    → completed
```

「实现已收工」= implement execution 已返回，且（没有可推断测试命令，或内部验收 exit 0）。内部验收失败不算收工。

正文 digest 与确认 digest 不一致 ⇒ 该份确认立即失效，退回对应阶段。用户或模型改了已确认文件后，不能继续拿旧确认往下走。实现又改了产品代码 ⇒ 检视确认失效，必须重新检视。

## 5. 输入路由（无分类器）

两个命令前半段相同（放下当前 slug、文件与代码保留），后半段切死：

| 命令 | 当前套件 | 接下来 | 参数 |
| --- | --- | --- | --- |
| `/abandon` | 标为 abandoned，不再是当前 | 空闲；进度条不再等你 | 禁止带新目标 |
| `/new-work <目标>` | 同样放下 | 立刻 Grill 新 slug | **必须**带非空目标 |
| 空闲 / 已完成 / 已废弃后的普通一句话 | 无当前 | 新需求 | 不用命令 |

| 当前状态 | 普通消息 | `/abandon` | `/new-work <目标>` |
| --- | --- | --- | --- |
| Grill / 等确认 | 回答或修订当前 | 确认后空闲 | 旧目录保留，新 slug 立刻 Grill |
| 正在实现或检视 | 取消当前 execution，ask_user：修订当前还是新开 | 取消并空闲 | 取消并立刻 Grill 新目标 |
| 已完成 / 已废弃 / 无 session | 新 slug，Grill | `COMPOSE_NOTHING_TO_ABANDON` | 新 slug，Grill |
| 命令用错 | — | 带了目标 → 拒绝，提示用 `/new-work` | 空目标 → 拒绝，提示用 `/abandon` |

`/abandon` 必须先经过命令确认（现有 `safety.confirmation: always`），确认前不改进度。不走 Work Item revision CAS。

禁止：每条消息先跑一轮意图分类模型。确定性短语（「继续」等）只当作「跟当前走」，不另开模型。

## 6. Module、interface 与 seam

按 deep module 设计：调用方只看到 Session 的小 interface；阶段选择、Skill 注入、确认、实现隔离、内部验收、检视都藏在实现里。

### 6.1 ComposeSession（外部 seam）

Host / Run adapter 只依赖：

```python
class ComposeSession:
    async def execute_turn(self, request: ComposeTurnRequest) -> ComposeTurnResult: ...
    async def inspect(self, *, thread_id: str) -> ComposeProgress | None: ...
    async def abandon(self, *, thread_id: str, reason: str | None = None) -> ComposeProgress: ...
```

- `execute_turn` 隐藏：slug 分配、阶段派生、Skill 绑定、确认门禁、fresh implement、内部验收、fresh review。
- `inspect` 只读，不写文档、不提问、不跑模型。
- 不暴露 `run_grill()` / `run_spec()` / `run_implement()` / `run_review()` 给 Host。
- 不拥有 SQLite 连接或 LangGraph；通过 ports 注入 store、documents、主 Agent、implement executor、review executor、verify、interaction。

`ComposeTurnRequest`：`thread_id`、`run_id`、`message`、`cancelled`、可选 `explicit_command`（仅 `/new-work` 或 `/abandon`）。没有 `TurnIntent` 字段。`/abandon` 也可经 `compose.abandon` 只读写进度（见 6.6），不创建 Run。

`ComposeTurnResult`：`progress`、`outcome`（`waiting_user` / `completed` / `failed`）、可选错误码。Blocked 不再作为第三套生命周期；内部验收失败或等待确认检视视为 `waiting_user`，基础设施失败才 `failed`。

### 6.2 ComposeDocuments

沿用现有路径契约，收窄文件集合：

- 根目录默认 `docs/compose/`，只允许配置覆盖根，不能指向 `.harness` 或工作区外。
- slug 规则保持 `document_paths.make_compose_slug`。
- 每套需求文件：`task.md`、`spec.md`、`plan.md`、`todo.md`；检视后有 `review.md`。不要 `report.md`。
- 读写真相是磁盘正文；数据库不复制全文。
- Runtime 是确认字段的唯一写入者。模型可以写正文和 `complexity`，不能自己把文档标成已确认。

`task.md` front matter 至少包括：`slug`、`complexity: simple|complex`。  
`plan.md` **不再要求** `verify_command` front matter，缺这条不能挡确认 Plan，也不能拿来问用户。  
确认 Plan 时 `todo.md` 必须存在且含至少一项未完成或已列出的任务；缺 Todo 不得确认。确认文案是一次「确认 Plan 和 Todo」，没有第二道 Todo 门。勾选变化不使 Plan 确认失效；增删条目或改条目正文以磁盘最新为准，Implement 按最新清单做。清空或删除 `todo.md` 则不得继续实现，回到 waiting_user 补清单，但不自动重开 Plan 确认（除非 `plan.md` 正文也变了）。

`review.md` 由检视 Agent 写入 `docs/compose/<slug>/review.md`。确认检视以该文件 digest 为准。

内部验收命令推断顺序（不问用户）：

1. 本轮实现过程里刚跑过、且像测试的命令
2. `plan.md` 正文里已经写明的测试命令
3. 仓库里一眼能看出来的（例如存在对应测试入口）

推断不到就跳过内部验收，直接进检视。

### 6.3 ComposeProgressStore（薄进度）

每个 Compose Thread 一行当前 session：

- `thread_id`、`slug`、`complexity`、`task|spec|plan|review` 的 `confirmed_digest`、`status`（Plan 确认以 `plan.md` digest 为准；Todo 不单独存确认 digest）
- 不再保存 `fix_rounds`
- 历史套件不建账本：旧目录留在 `docs/compose/` 即历史
- 同一 Thread 同时最多一个未完成 slug（唯一约束）

删除并停止读写：`harness_compose_work_items`、`*_activities`、`*_effects`、`*_evidence`、`*_confirmations`（Work Item 版）、`*_run_bindings`。Schema 直接升到新版本并 DROP 旧表，不做数据迁移。

Thread mode 仍由现有 `harness_thread_modes` 承担。

### 6.4 主 Agent、fresh implement 与 fresh review

| 阶段 | 谁执行 | 上下文 | Skill |
| --- | --- | --- | --- |
| Grill / 写 Task | 本 Run 主 Agent | 本 Thread 对话 | 原版 `grill-me`（仅改 ask_user） |
| Spec | 同上，同一对话 | 同上 + 已确认 Task | 原版 `spec-driven-development`（仅改 ask_user，若它提问） |
| Plan | 同上 | 同上 + 已确认 Task（及已确认 Spec） | 原版 `planning-and-task-breakdown`（仅改 ask_user，若它提问）；须同时写出 `plan.md` 与 `todo.md` |
| Implement | 新的 Managed execution | **只有**已确认文档（含当前 `todo.md`）+ 上次 `review.md`（若因检视退回）+ 内部验收失败日志（若有）+ 仓库工具 | 原版 `test-driven-development`（提问则 ask_user） |
| 内部验收 | Runtime，不跑模型 | 推断出的测试命令 | 无；不是用户阶段 |
| Review | 新的 Managed execution | **只有**已确认文档 + 本套改过的文件（及 diff） | 原版 `code-review-and-quality`（提问则 ask_user） |

主 Agent 必须能流式打出思考、正文、工具和 `ask_user`。禁止为 Grill 再开一个无流式输出的 child execution。

检视的「本套改过的文件」由 Runtime 列出：`plan.md` / `todo.md` 点名的路径，以及实现阶段实际写入的工作区文件。不把整个仓库当检视范围。检视工具面：读仓库、`ask_user`、`tool_search`、只允许写 `docs/compose/<slug>/review.md`。禁止 `execute` 改产品代码，禁止写 `docs/compose/<slug>/` 以外的文件。

### 6.4.1 Compose 系统提示与 Skill 可见性

与 Build 拆开，不共用「完整系统提示 + 全量 Skill 索引」：

```text
Compose 模型上下文
  ├─ 短 Compose 系统说明（当前阶段、用 ask_user、不要自己跳阶段）
  ├─ 当前阶段一份原版内置 Skill 正文（按 builtin identity 加载）
  ├─ AGENTS.md / 项目规则
  └─ 工具定义（阶段需要的工具；Grill 以只读 + ask_user 为主）
不包含
  ├─ <harness_available_skills> 用户/项目/市场 Skill 索引
  ├─ 用户同名覆盖后的 grill-me / spec / plan / tdd / code-review-and-quality
  └─ Build 那份通用「按需挑选 Skill」说明
```

- 阶段 Skill 只走内置 bundle 的 reserved identity；`project/`、`user/`、`market/` 同名记录不得替换 Compose 注入。
- 用户 Skill 仍出现在侧边栏与 `skills.list`（HC-154 行为），供安装管理；Compose 输入栏补全与模型 tool 列表不提供「选用某用户 Skill」。
- Implement 与 Review 的 fresh execution 同样不带用户 Skill 索引。
- Build Run 的 Skill 索引与系统提示本任务不得改行为。

### 6.4.2 MCP 与工具可见性

复用已有 `DeferredToolMiddleware` + `tool_search`，Compose **强制** `defer_tools=on`（不受 Build 的 `auto`/`off` 影响，避免全量 MCP schema 回流）。

```text
第一轮绑定（模型看得见的 schema）
  Grill / Spec / Plan：
    ls, read_file, glob, grep, ask_user, tool_search
    写文件仅允许 docs/compose/<slug>/
  Implement：
    上述读工具 + 工作区写文件 + execute + write_todos + ask_user + tool_search
  Review：
    读工具 + ask_user + tool_search
    写文件仅允许 docs/compose/<slug>/review.md

不在第一轮绑定
  全部 MCP 工具的参数 schema
  execute / task / enter_plan_mode / exit_plan_mode（Grill/Spec/Plan/Review）
  task 委派、Plugin Agent

摘要（无 schema）
  「有 N 个已连接 MCP，需要时 tool_search」
  无 MCP 时摘要为空，行为等同未配置

tool_search 命中
  → reveal 该工具名
  → 下一轮 bind_tools 才含其完整 schema
  → 模型再调用
  → 写入类 MCP / execute 仍走 Policy 与审批
```

执行入口仍注册已允许的 MCP（审批与能力视图不变）；中间件只挡模型可见性。未配置 MCP 时不占 schema。产品决策仍只能 `ask_user`，不能用 MCP 代替确认。

Implement / Review 的 ContextPack 不得包含 Grill 对话原文、分类痕迹、未确认草稿、用户 Skill 索引。这就是「fresh execution + 已确认文档隔离」，**不是新 Thread**。

内部验收复用现有安全执行入口（Policy、Approval、workspace、sandbox、锁）。无用户批准门禁：推断出的常见测试命令不再二次弹审批。内部验收失败不得进入检视，也不得把 session 标 completed。

### 6.5 提问与确认
 
唯一用户提问通道：Build 已有的 `ask_user` → LangGraph interrupt → `interaction/question`（或现有等价 Interaction）。

Invariant：

- 任何 Compose 提问的 JSON-RPC `id` 必须是客户端能按 inbound request 处理的 Interaction id，且客户端曾按 request（带 `method`）接收。
- 禁止生成 `compose-task-interview-*`、`compose-*-<timestamp>` 这类仅引擎知道、却以 **response** 帧出现的 id。
- 相关决策可以一次 `ask_user` 问几题，最多 5 题；TUI 用问答 Dock 展示，逐题作答后一次提交。禁止一次问十几题。文本题同样走 Dock，不改成输入栏占位。
- Task / Spec / Plan / Review 确认由 Runtime 发起，选项至少：`确认`、`我要改`。Plan 确认覆盖当时的 `plan.md` + `todo.md`，不另弹 Todo 确认。Review 确认覆盖当时的 `review.md`，选项为 `确认`（结束）与 `按意见改`（退回实现）。Task 确认额外允许改 `complexity`（或提供「按简单，不审 Spec」「按复杂，要审 Spec」）。
- `我要改` / `按意见改`：确认不写入，阶段留在当前文档或退回实现，主路径继续。
- 禁止为了补测试命令再弹 `ask_user`。

### 6.6 进度投影

删除协议事件 `compose.work_item`、`compose.activity` 以及旧五阶段 `compose.state`（understand/plan/build/verify/review）。

单一事件 `compose.progress`（`revision` 单调递增，迟到帧拒绝）：

```text
thread_id
slug
complexity: simple | complex
status: active | waiting_user | completed | abandoned
current_stage: grill | task | spec | plan | implement | review
waiting: none | task_confirm | spec_confirm | plan_confirm | review_confirm | ask_user | implement_choice
stages[]: { id, state: pending | current | confirmed | skipped | failed }
documents[]: { kind: task|spec|plan|todo|review, path, confirmed }
revision
```

UI `stages[].id`：`requirement` | `spec` | `plan` | `implement` | `review`。不再有 `verify`。

- `simple` 时 Spec 阶段为 `skipped`（文件仍可生成，门禁跳过）。
- 内部验收进行中：`current_stage=implement`，对应 UI 格为进行中，不另亮「测试」。
- 内部验收失败：实现格为 `failed`，`waiting=ask_user` 或普通输入跟当前。
- `compose.inspect` 返回同一形状。`compose.abandon` 保留，但参数只剩 `thread_id` 与可选 `reason`，不再要 `work_item_id` / `expected_revision`；结果为更新后的 `compose.progress`（无当前 slug 或 `status=abandoned`）。没有未完成 session 时返回 `COMPOSE_NOTHING_TO_ABANDON`。
- `threads.open` 不再返回 `work_item` / `compose_activities`；改为可选 `compose_progress`。
- Interactive Core 用 `composeProgress` 取代 `composeState` + work item 投影。TUI 只读渲染该投影；点击已存在 `path` 打开文件；忽略跳阶段 intent。
- 协议不再包含 `fix_rounds`、`status=verifying`。

## 7. 错误语义

| 码 | 何时 | 用户可见 | Run 终态 |
| --- | --- | --- | --- |
| `THREAD_MODE_LOCKED` | Build Thread 上跑 Compose 或反之 | 说明 Mode 已锁 | failed |
| `COMPOSE_VERIFY_FAILED` | 内部验收非 0 | 验收卡展示命令与日志 | 不失败，waiting_user，停在实现 |
| `COMPOSE_IMPLEMENT_FAILED` | implement execution 基础设施失败 | 短诊断，无内部栈 | failed，retryable |
| `COMPOSE_REVIEW_FAILED` | review execution 基础设施失败 | 短诊断，无内部栈 | failed，retryable |
| `COMPOSE_DOCUMENT_PATH_INVALID` | 路径穿越或非法 slug | 文档路径不合法 | failed |
| `COMMAND_MODE_UNAVAILABLE` | 在 Build 打 `/new-work` 或 `/abandon` | 命令仅 Compose 可用 | 不启动 Run |
| `COMPOSE_NOTHING_TO_ABANDON` | 没有未完成当前套件时 `/abandon` | 没有进行中的需求可废弃 | 不启动 Run / RPC 错误 |
| `COMPOSE_NEW_WORK_GOAL_REQUIRED` | `/new-work` 未带目标 | 开新需求请带目标；只要停用 `/abandon` | 不启动 Run |
| `COMPOSE_ABANDON_TAKES_NO_GOAL` | `/abandon` 后面跟了新目标 | 换题请用 `/new-work` | 不启动 Run |

删除 `COMPOSE_VERIFY_COMMAND_MISSING`：缺测试命令不是错误，直接进检视。

模型把 Skill JSON 写坏、或 `ask_user` 参数不合法：留在对话里可见，允许重试；不得把 `json.loads` 原文当作协议错误打到 Timeline。

`protocolError: Unknown JSON-RPC response id: compose-task-interview-*` 视为本任务回归失败，不论是否另有文案包装。

## 8. Invariant

1. Compose 主路径不调用 TurnIntent 分类器，不创建 Work Item，不写 Activity/Effect 账本。
2. 主对话在 Grill 开始后必须产生用户可见的流式帧（reasoning / content / tool / ask_user 之一），不得先空转一轮仅分类。
3. 所有提问走 `ask_user` Interaction；服务端不得对未发出的 request id 回 response 帧。
4. 确认只能由 Runtime 在用户明确选择「确认」后写入；digest 变化立即作废。
5. 简单 + Task 已确认 ⇒ 不得出现 Spec 确认门禁。
6. 确认成功后必须在同一 Turn（或同一次 Interaction 恢复）进入下一阶段，不得等待用户再发「继续」。顺序：确认 Task → 写 Spec（简单则不审并立刻写 Plan）→ 确认 Spec（仅复杂）→ 写 Plan+Todo → 确认 Plan → implement →（可选内部验收）→ review → 确认 Review。确认 Plan 时必须已有非空 `todo.md`；勾选变化不得把流程退回 Plan 确认。
7. Implement 与 Review 的模型上下文不含 Grill 原文。
8. 不得在未取消的情况下因测试失败自动再实现。
9. session completed 的充分条件是检视确认已写入且 digest 仍匹配；内部验收失败或检视未确认不得 completed。
10. `/abandon`、`/new-work` 与完成后的新消息都不得删除或覆盖旧 `docs/compose/<旧 slug>/`。`/abandon` 不得开启新 Grill；空 `/new-work` 与带目标的 `/abandon` 必须拒绝。
11. 进度条不能把 `pending` 阶段变成当前阶段。不得再渲染「测试」格。
12. Compose 不扩大 Policy / sandbox / 审批权限。
13. 不为旧 Work Item 表或旧 projection 保留 alias。
14. Compose 主路径、Implement 与 Review 不得把用户 Skill 编入系统提示或开放为可调用 Skill；阶段方法只加载内置 identity。
15. Compose 不得在第一轮把 MCP 完整 schema 交给模型；必须经 `tool_search` reveal。不得因 Build 的 `defer=off` 而在 Compose 全量注入。
16. 检视不得写产品代码；用户未确认 `review.md` 不得结束。
17. 不得向用户索要 `verify_command`。

## 9. 原版 Skill 微调范围

允许改动的唯一行为：凡是向用户提问，必须调用 `ask_user`，不得只在 assistant 正文里提问后结束本轮。

禁止在本任务中改：95% confidence、一次一问的原版节奏、Spec 模板结构、Plan 拆步规则、TDD 步骤、Review 五轴清单正文。这些留待看效果后再开 Task。

缺 required 原版 Skill 时 fail closed（与现打包/digest 校验一致）。

## 10. UI

- Tab / Shift+Tab 正交关系不变；运行中不可切 Mode。
- 时间线按 Build 同款渲染主 Agent、实现 Agent、检视 Agent 的思考、工具、`ask_user`。内部验收若发生，画「验收」工具卡，不属于进度格。
- 只读进度对用户只显示中文五段：`需求 → 规格 → 计划 → 实现 → 检视`。状态词只用：进行中 / 等你 / 已确认 / 跳过 / 失败。
- 映射：Grill+确认 Task → 需求；写/确认 Spec → 规格（简单需求为跳过）；Plan+Todo 一次确认 → 计划；实现 + 内部验收 → 实现；Review+确认 Review → 检视。协议 `current_stage` 仍可以是 `grill|task|spec|plan|implement|review`，UI 负责合并与翻译，不增加门禁。
- 点「需求」打开 `task.md`（若已有），点「规格」打开 `spec.md`，点「计划」打开 `plan.md`（Todo 可从计划进入或同级打开 `todo.md`），点「检视」打开 `review.md`。不能跳阶段。
- `ask_user` 一律走底部问答 Dock（单选选项、文本输入、多题逐题）；不再把文本题藏进输入栏。
- 窄终端缩成一行中文，例如 `计划 · 等你确认`，不得因此改协议形状。

## 11. 测试策略

框架与现仓库一致：Python `packages/agent/tests/compose/`，CLI `packages/cli/tests/`，协议 `bun run protocol:check`。禁止真实模型凭据。

至少覆盖：

- 路由：中途普通消息不新开 slug；`/abandon` 确认后空闲且旧目录仍在；`/new-work <目标>` 开新 slug 且旧目录仍在；空 `/new-work` 与带目标 `/abandon` 失败；完成后下一句开新 slug。
- 无分类器：`execute_turn` 在首条 jsondiff 类消息上不调用任何 classifier port。
- `ask_user`：Grill 提问走 Interaction request（带 method）；测试断言 wire 上不出现 `compose-task-interview-` 前缀，也不出现「无 method、id 为该前缀」的 response。
- 门禁：simple 跳过 Spec 确认；complex 或用户改为 complex 则有 Spec 确认；改已确认 Task 正文后确认作废。
- 自动衔接：确认 Task 后同一恢复路径写出 Spec（简单则继续写出 Plan）；确认 Spec 后写出 Plan；确认 Plan 后调用 implement。测试断言这些步骤都不依赖用户再提交「继续」。
- 自动实现：Plan 确认后同一 `execute_turn` 调用 implement executor。
- 内部验收：能推断命令且 exit 0 → 调用 review executor；非 0 → 不调用 review、不 completed；推断不到 → 直接 review。不得出现索要 `verify_command` 的 `ask_user`。
- 检视：review 写出 `review.md` 后等待 `review_confirm`；确认 → completed；「按意见改」→ 再次 implement 且 ContextPack 含 `review.md`；review ContextPack 无产品写权限意图。
- 隔离：implement / review 的 ContextPack 只有已确认文档（含 `todo.md`，review 另含本套改动），无 Grill 原文。
- Todo：无 `todo.md` 不能确认 Plan；勾选变化后 Plan 确认仍有效。
- 恢复：写入确认后重启进程，inspect 仍显示已确认且下一阶段不是 Grill。
- TUI：进度投影渲染五段含检视、不含测试；不存在跳阶段 intent。
- 上下文：Compose 组装出的系统提示不含 `<harness_available_skills>`；即使用户安装了同名 `grill-me`，阶段仍加载内置文件。Build 回归仍含用户 Skill 索引。
- MCP：首轮可见工具名不含任何 MCP 工具；`tool_search` 命中后下一轮才包含该名；Grill 首轮不含 `execute`。Compose 在 `defer=off` 的全局配置下仍强制延迟加载。
- Build：现有 Run / ask_user / 审批回归。

命令：

```text
cd packages/agent && .venv/bin/python -m pytest -q tests/compose
cd packages/cli && bun test
bun run protocol:generate && bun run protocol:check
bun run typecheck
```

整任务交付前再跑 `bun run test` 与 `bun run project:check`。

## 12. 非范围

与 Task 一致：双轴并行 Reviewer；每个 Todo 单独派审；检视改产品代码；用户可见测试格与 `verify_command` 门禁；测试失败自修循环；每阶段新 Thread 或压缩；精确重放未完成写/验证；意图分类器；进度跳阶段；现在改原版 Skill 除 ask_user 外的仪式；旧数据迁移；自动 commit/PR。

## 13. 成功标准（可观察）

对照 Task 验收项。额外技术信号：

- 协议与生成物中不再存在 `compose.work_item`、`compose.activity`、Work Item snapshot；`compose.abandon` 不再携带 `work_item_id` / `expected_revision`。
- `compose.progress` 的 UI 阶段含 `review`、不含 `verify`；无 `fix_rounds`。
- agent 包 Compose 主路径不再导入 `turn_intent` 分类器或 `work_item_engine`。
- 架构文档 `Compose 工作模式.md` 改为 Session + 文档确认模型。

## 14. 代码风格与边界

- 跟随邻近 Python / TypeScript 风格；生产源码中文模块说明与公开方法 docstring。
- Always：TDD；协议两端同改；Compose 提问只走 ask_user。
- Ask first：再引入新的公开 RPC 方法（本规格已列的 `compose.progress` 替换除外）；把 `report.md` 加回必选；给 Todo 单独加确认门禁。
- Never：兼容旧 Work Item API；给分类器留 fallback；在 Grill 阶段再开无流式 child agent；把完成判定改成模型自报；向用户索要测试命令；让检视 Agent 写产品代码。

## Open Questions

无。产品决策已在 Task 确认；上列实现假设若被否定，先改 Task 再改本文。
