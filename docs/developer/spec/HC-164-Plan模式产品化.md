# HC-164 Plan 模式产品化规格

关联任务：[HC-164](../task/HC-164-Plan模式产品化.md)  
调研：[163-Plan模式产品化](../research/163-Plan模式产品化.md)  
架构入口：[架构总览](../architecture/架构总览.md)

用户结果与验收以 Task 为准。本文只规定行为、公开 interface、状态、错误语义和 invariant。实施步骤见 [Plan](../plan/HC-164-Plan模式产品化.md)，勾选见 [Todo](../todo/HC-164-Plan模式产品化.md)。

## 1. 通俗目标

Build 里可以把「先想清楚再改代码」走完：用 `/plan` 进入计划模式，Agent 只调查并把计划写成一份文件；你滚动看完后，批准就开始按进计划前的权限改代码，打回就继续改计划，放弃则退出计划、不动项目。

进了 plan，还是原来那个 Agent（Skill、MCP、系统提示词主体都不换），只是手脚被权限绑住。这和 Compose 里确认 `docs/compose/.../plan.md` 不是同一件事。

## 2. 已确认假设

来自已确认 Task，Spec 不得改写：

1. 一个 Task 做完整闭环；`/plan` 是第一停点，不是独立功能。
2. 只在 Build；Compose 不提供 `/plan`。
3. plan 仍是五档审批模式之一，不另起独立模式体系（不换工具集、不换规划模型）。
4. `/plan` / `/plan <目标>` / `/plan exit`；已在计划中再打 `/plan` 只提示；当前 Run 不取消。
5. Shift+Tab 循环保留且仍含 plan。
6. 批准后恢复进入计划前的那一档权限，不在计划卡上再选 auto-edit / default。
7. 计划是磁盘文件，规划过程中就写；路径 `~/.harness/plans/<thread_id>.md`，不进仓库。
8. 审计划三个动作：批准并实现 / 打回（可带文字）/ 放弃；空计划也要能选。
9. TUI 与 Web 一起交付。
10. 进 plan 不卸 Skill、不断开 MCP、不换系统提示词主体；只多规划提醒，权限收紧。
11. `execute` 第一版硬拒；同一 Task 后段做只读命令判定。
12. 自然语言「先做计划」不做第一停点，同一 Task 后段补：模型提议、你点头才进（运行时开门，不结束当前 Run 重开）。
13. 行批注与「再打开计划」不做第一版审批面，同一 Task 后段补。
14. 计划模式下不派生子代理；不把计划注入子代理。
15. 子代理仍不得拥有 `enter_plan_mode` / `exit_plan_mode`（HC-161）。

规格内实现假设（与 Task 不冲突，实现按此做；若要改必须先回写 Task）：

- 计划审批**不**复用 `interaction.approval` 的 `approve_once` / `approve_thread` / `approve_project`。那是工具权限，不是「这份计划行不行」。新增 `interaction.plan`，比照 `directory_trust`。
- 模型看见的计划路径是虚拟文件 `/.harness/plan.md`；磁盘文件是 `~/.harness/plans/<thread_id>.md`。不把用户 home 绝对路径塞进模型工具参数。
- `exit_plan_mode` 保留工具名，改成「读计划文件并停住等人审」；无参。不要先删再加 `submit_plan`。
- `enter_plan_mode` 第一停点仍不切模式：调用则返回「请用户使用 `/plan`」。后段再变成真 HITL + 运行时计划门。
- 未知 MCP 工具 fail-closed：未声明只读则按会写处理，计划模式下拒绝。
- 批准或放弃之后，当前 plan Run 在写出工具结果后立刻终态，不再让模型在 plan 图里再想一轮（否则写文件仍会被拒）。打回则同一 Run 继续。
- 批准后的「开始实现」由 CLI 在 plan Run 终态后，用恢复后的审批模式自动 `run.start`。Host 不自己再开一条 Run。

## 3. 术语

```text
Plan 约束
└─ 当前 Run 是否按计划模式收紧权限（项目文件不可写、会改系统的命令/MCP 拒绝）。
   第一停点来自 run.start 的 approval_mode=plan。
   后段点头进入时来自运行时 flag，不重开 Run。

prePlanMode
└─ 进入 plan 之前的那一档审批模式。/plan exit、放弃、批准 都恢复它。
   Shift+Tab 离开 plan 走循环下一档（default），不恢复 prePlanMode。

planRevision
└─ 进入/离开 plan 时递增的整数。计划审批必须带当时的 revision；中途换档则作废。

计划文件
├─ 虚拟路径（模型侧）：/.harness/plan.md
└─ 磁盘路径（本机）：{home}/.harness/plans/{thread_id}.md

计划交互
└─ interaction.plan：滚动预览 + approved / revise / abandoned
```

不要把「会话内 Plan 模式」说成 Compose 的 Plan 阶段，也不要把本功能说成 plan 子代理。

## 4. 模块与 seam

一项功能一份 Spec。下面是实现时的模块边界，不是平行文档树。

| 模块 | 职责 | 调用方必须知道的 interface | 依赖 |
|---|---|---|---|
| plan-command | `/plan` 解析、Build 可见性、切档或带参提交 | CommandDefinition + CommandResult | session-plan-mode |
| session-plan-mode | 记录 prePlanMode、当前是否 plan、revision；与 Shift+Tab 共存 | enter / exit / cycle / 当前值 | 现有 RunFeature 覆盖值 |
| plan-file | 虚拟路径与磁盘文件互相同步；覆盖写、不进仓库 | 读/写 `/.harness/plan.md` | 现有文件工具 + 虚拟后端 |
| plan-gate | 计划约束下谁能跑 | 工具名 + 目标路径 → allow / 硬拒说明 | session-plan-mode、plan-file |
| plan-submit | `exit_plan_mode` 读文件、停住、带 revision 发起计划交互 | 无参工具；结果是三选一后的工具消息 | plan-file、plan-review |
| plan-review | TUI/Web 滚动预览与三个动作 | 只消费 `interaction.plan` | protocol |
| plan-continue | 批准后恢复档位并自动开实现轮；放弃只恢复档位 | plan Run 终态 + 决策 | session-plan-mode、RunFeature |
| plan-enter-consent | 后段：`enter_plan_mode` 问一声，同意则打开运行时计划门 | HITL 是/否；拒绝则不进 | plan-gate |
| plan-annotate | 后段：行批注 + `/plan-view` | 打开已有计划预览 | plan-file、plan-review |
| plan-readonly-shell | 后段：只读命令放行 | execute 文本 → 只读 / 写 / 未知 | plan-gate |

深度原则：表现层只看到「当前是不是 plan」和计划交互。磁盘路径、revision 校验、批准后终态、MCP 是否只读，都藏在对应模块里。

禁止：为计划审批再做一套与 Host Interaction 平行的对话框通道；禁止把下一档审批模式塞进计划卡；禁止计划文件写进工作区。

建议实现顺序（供 Plan，不是平行 Task）：plan-command → session-plan-mode → plan-file + plan-gate → plan-submit + plan-review + plan-continue → plan-enter-consent → plan-annotate → plan-readonly-shell。

## 5. 顶层流程

```text
空闲、Build：
  /plan                  → 记下 prePlanMode，切到 plan，提示可以开始规划
  /plan <目标>           → 同上，并立刻把 <目标> 当作本轮用户消息提交（approval_mode=plan）
  /plan exit             → 恢复 prePlanMode
  已在 plan 再 /plan     → notice，不改 prePlanMode
  Compose 下 /plan       → 菜单隐藏；直接输入则本地提示不可用，不交给模型

规划中（plan 约束开启）：
  读/搜/Skill/只读 MCP/ask_user/write_todos/web_search/web_fetch/lsp 可用
  写项目文件、delete、execute、task、会写的 MCP、memory_save → 硬拒并说明
  写 /.harness/plan.md → 放行（创建或覆盖）
  模型调用 exit_plan_mode()
      → 读虚拟计划文件
      → interaction.plan（正文可空）
      → approved：工具结果「已批准」→ 本 Run 终态 → CLI 恢复档位 → 自动开实现轮
      → revise：工具结果带反馈 → 本 Run 继续留在 plan
      → abandoned：工具结果「已放弃」→ 本 Run 终态 → CLI 恢复档位 → 不开实现轮

正在跑的一轮里打 /plan 或 Shift+Tab：
  当前轮照旧；覆盖值只影响下一轮 run.start
```

## 6. `/plan` 命令

### 6.1 注册

| 字段 | 值 |
|---|---|
| id | `approval.plan` |
| name | `plan` |
| argumentHint | `[exit \| <目标>]` |
| presentation | `action` |
| workModes | `["build"]` |
| requiresIdle | 否（进行中也可切下一轮档位） |
| requiresThread | 否 |

Compose 因 `workModes` 为 hidden。直接输入时沿用现有 hidden 提示， canonical 文案：「`/plan` 仅在 Build 工作模式可用。」不把 `/plan ...` 当普通 Prompt 交给模型。

### 6.2 参数

参数保持原始大小写与空白语义（与 `resolveSlashCommand` 一致），仅先 `trim`：

| 输入 | 行为 |
|---|---|
| 空 | 进入 plan（若尚未在 plan） |
| 整段为 `exit`（忽略大小写） | 退出并恢复 prePlanMode |
| 其它任何非空文本，包括 `exit the login` | 视为规划目标，进入 plan 并提交该原文 |

已在 plan：

- 空 `/plan` → notice：「已在计划模式。改计划请直接发消息；离开请用 `/plan exit`。」
- `/plan exit` → 仍退出。
- `/plan <目标>` → 不重复记录 prePlanMode；若当前空闲则提交该目标（仍带 `approval_mode=plan`）；若正在跑则只提示已在计划模式，目标不排队（避免悄悄改下一轮消息）。

无 thread 时：空 `/plan` 只切档；带目标的 `/plan` 走与普通提交相同的建 thread 路径。

### 6.3 与 CommandResult

新增结果，由 Controller 解释，Handler 不直接调 RPC：

```text
{ type: "set-approval-mode"; mode: "plan"; restore?: false }
{ type: "restore-approval-mode" }
{ type: "set-approval-mode"; mode: "plan" } 然后 { type: "submit-prompt"; prompt }
```

带目标时必须先切档再提交，保证这次 `run.start` 的 `approval_mode` 已是 `plan`。

### 6.4 Shift+Tab

循环仍是 `plan → default → auto-edit → auto → yolo`。进入 plan 时若当前不是 plan，记下 prePlanMode。从 plan 用 Shift+Tab 离开时，到循环中的 `default`，并丢掉 prePlanMode（与 `/plan exit` 不同）。

## 7. 会话计划状态

CLI `RunFeature`（Web 共用）持有：

```text
approvalModeOverride     现有
prePlanMode              进入 plan 时的上一档；不在 plan 则为空
planRevision             从 0 递增的整数
```

`run.start` 继续只传 `approval_mode`。prePlanMode 与 revision 是会话内存，不写配置、不进 SQLite。进程退出后与今天的 Shift+Tab 覆盖值一样丢掉。

批准交互的 payload 带上当时的 `planRevision`。交互挂起期间若 revision 变化（用户又切档），Host 拒绝这次批准/打回/放弃中的「切模式」副作用：工具结果为过期，留在当时真实模式；CLI 收起预览。

## 8. 计划文件

### 8.1 路径

| 侧 | 路径 |
|---|---|
| 模型 / 工具 | `/.harness/plan.md` |
| 磁盘 | `Path.home() / ".harness" / "plans" / f"{thread_id}.md"` |

`~/.harness/plans/` 权限与现有 `~/.harness` 一致（目录 `0700`）。同一 thread 再次写入则覆盖，v1 不保留历史版本。没有 thread_id 时不能写计划文件；规划 Run 必有 thread。

### 8.2 模型怎么改这份文件

走现有 `read_file` / `write_file` / `edit_file`，目标必须是 `/.harness/plan.md`。首次不存在用 `write_file` 创建。`edit_file` 仍要先读再改（Snapshot 约束不为此路径作废）。禁止 `delete_file` 这份计划。

虚拟后端对这一路径在计划约束开启时允许写；计划约束关闭时写拒绝。读在任何模式下允许（后段 `/plan-view` 要用）。

不得把 `{home}/.harness/plans/...` 当作工作区路径绕过目录信任去写。工作区 Git 不出现该文件。

### 8.3 `exit_plan_mode` 读什么

无参。从虚拟路径读，trim 后空或文件不存在 ⇒ `has_plan=false`，`plan_markdown=""`。UI 仍打开，用占位说明「还没有写出计划」，三个动作照常。

工具不接受计划正文参数，避免模型和文件各写各的。

## 9. 权限门（plan-gate）

判定输入：是否处于 Plan 约束 + 工具名 + 参数里的路径（若有）。

| 调用 | Plan 约束下 |
|---|---|
| `ls` / `read_file` / `glob` / `grep` / `lsp` / `web_search` / `web_fetch` / `tool_search` / `memory_search` / `ask_user` / `write_todos` | 允许（目录信任规则不变） |
| `read_file`/`write_file`/`edit_file` 且目标为 `/.harness/plan.md` | 允许 |
| 其它 `write_file` / `edit_file` / `delete_file` | 硬拒 |
| `execute` | 第一版硬拒；后段见第 15 节 |
| `task` | 硬拒 |
| `memory_save` | 硬拒 |
| `enter_plan_mode` / `exit_plan_mode` | 允许出现在 schema；行为见第 10、12 节 |
| MCP 声明只读（LangChain `is_read_only` 或等价 Read scope） | 允许 |
| MCP 未声明或非只读 | 硬拒 |
| 未知工具 | 硬拒（与现有 `get_tool_kind` fail-closed 为 EXECUTE 一致） |

硬拒是工具错误结果，不弹审批卡。文案必须写明：当前是计划模式、为何拒绝、应改用只读调查或把动作写进计划。写项目文件时点出唯一可写路径 `/.harness/plan.md`。

`interrupt_on_for_approval_mode` 在 plan 下仍只为目录信任服务只读文件工具；**必须**把 `exit_plan_mode`（后段还有 `enter_plan_mode`）纳入可停住的工具。今天 plan 分支忽略 `extra_interrupt_tools` 的行为要改掉，否则提交门接不上。

Skill：目录仍注入系统提示词；Skill 正文继续经 `/.harness/skills/...` 只读虚拟文件读取。不在进 plan 时卸 Skill。

系统提示词主体不变。Plan 约束开启时保留现有审批模式后缀，并补一句：把计划写到 `/.harness/plan.md`，写完调用 `exit_plan_mode`，有歧义用 `ask_user`。不为此改造成每轮独立 reminder 通道（那是后续优化）。

## 10. 提交计划与协议

### 10.1 工具

`exit_plan_mode`：常驻 schema（`RESIDENT_TOOL_NAMES`），不随进出 plan 改变工具块形状。参数为空对象。仅在 Plan 约束开启时真正停住；若当前不是 plan，返回错误「当前不在计划模式，不要调用 exit_plan_mode」，不停交互。

### 10.2 新交互 `interaction.plan`

在 `packages/protocol/schema/v3.json` 增加 interactions 项，生成 TS/Python。交互客户端（TUI/Web）的 `handles` 增加 `"plan"`。无头/非交互不声明；此时 `exit_plan_mode` 直接错误返回「当前会话不能审批计划」。

**Request payload（概念形状）：**

```text
thread_id, run_id, timeout_ms
payload:
  interrupt_id: string
  tool_call_id: string
  revision: integer
  has_plan: boolean
  plan_markdown: string        # 无正文时为空串
  plan_virtual_path: "/.harness/plan.md"
  plan_display_path: string    # 给 UI 的短路径，如 ~/.harness/plans/<id>.md
  decisions: ["approved", "revise", "abandoned"]
```

**Response：**

```text
decision: "approved" | "revise" | "abandoned"
feedback?: string            # 仅 revise 使用；空白当没写
```

第一版没有行批注字段。超时、owner 断开、fail-closed 视为 `abandoned` 的工具结果语义，但**不**恢复 prePlanMode（避免一次掉线把用户踢出 plan）；CLI 收起预览，模式仍是 plan。

`approved` / `abandoned` 且 revision 有效：Host 写入对应工具结果后**结束本 Run**（`run.completed`）。  
`revise`：工具结果含用户反馈（无反馈也要写明「用户要求继续打磨计划」），Run 继续。

revision 无效：工具结果说明审批过期，Run 继续（仍在调用时的真实约束下），不切档。

### 10.3 UI

TUI 与 Web 都渲染 markdown 预览（可滚动）。空计划显示明确占位，三个动作仍在。

建议文案（共享 `presentation-shared`，两端自绘）：

| decision | 标签 |
|---|---|
| approved | 批准并开始实现 |
| revise | 继续打磨 |
| abandoned | 放弃计划 |

打回：焦点可落到输入，把文字放进 `feedback`；空反馈也允许。批准、放弃不使用反馈。

禁止在这张卡上放 auto-edit / default / yolo 选项。

## 11. 批准后续跑

CLI 在对应 plan Run 终态后：

| 决策 | CLI |
|---|---|
| approved | `restore-approval-mode`，再 `run.start`：消息为固定实现提示（含虚拟路径），`approval_mode=prePlanMode` |
| abandoned | 只 `restore-approval-mode`，不自动开跑 |
| revise | 不改档位，不新开 Run |

实现提示大意固定、可测：「用户已批准计划。请读取 `/.harness/plan.md` 并开始实现。先更新任务清单。」不要在用户消息里贴计划全文（文件和上一轮上下文已有）。

prePlanMode 缺失时（例如只 Shift+Tab 进了 plan 却没记下）回退到 `default`。

## 12. 进入侧 `enter_plan_mode`（分阶段）

工具常驻，子代理仍不可见（HC-161）。

**第一、二停点：** 调用立即返回说明，不切档、不停交互：「要进入计划模式，请让用户使用 `/plan` 或 Shift+Tab。」不要再写无人读的 `PlanModeState`。

**后段停点：** 调用触发普通审批交互（是/否即可，不必用 `interaction.plan`）。同意：打开**当前 Run** 的 Plan 约束（运行时 flag，`PlanModeMiddleware` 读 flag 而不是只看构图枚举），种子空计划文件（已有内容不截断），工具结果给出规划步骤。拒绝：工具结果「用户不同意进入计划模式」，约束不变。无头不声明审批则直接拒绝进入。

后段不得用「结束当前 Run、再以 plan 重开」冒充点头进入。

## 13. 后段：行批注与 `/plan-view`

在 `interaction.plan` 预览里可选中行范围写意见。`revise` 的 feedback 把行号、摘录和意见编在一起；`approved` 若已有批注，实现轮提示里附加「批准时的审阅意见」。

`/plan-view`（id `approval.plan-view`，无别名）：Build 下打开当前 thread 计划文件预览。无文件则 notice「还没有计划」。不切档。若计划交互正挂起，则回到那次预览而不是另开只读层。

## 14. 后段：只读命令

计划约束下对 `execute`：

| 判定 | 行为 |
|---|---|
| 只读 | 放行 |
| 会写 / 破坏性 | 硬拒，提示写进计划 |
| 未知 | 仅本次单条审批；批准不改变 Plan 约束、不改变档位 |

复用现有只读 Shell 判定能用的用现成的；不够再补 AST。未知命令的单次批准走现有 `interaction.approval`，不是 `interaction.plan`。

## 15. 错误语义

| 情况 | 用户可见结果 |
|---|---|
| Compose 下 `/plan` | 本地提示仅 Build；不当 Prompt |
| 已在 plan 再空 `/plan` | notice 已在计划模式 |
| `/plan exit` 但当前不是 plan | notice 当前不在计划模式 |
| 规划中改项目文件 | 工具错误，指出只能写 `/.harness/plan.md` |
| 规划中 `execute`（第一版） | 工具错误，不要弹审批 |
| 规划中会写的 MCP / `task` | 工具错误 |
| 非 plan 调 `exit_plan_mode` | 工具错误，不停交互 |
| 客户端不能处理 `plan` | 工具错误，不停交互 |
| 审批 revision 过期 | 预览关闭；工具结果过期；不切档 |
| 计划交互超时 / 断开 | 预览关闭；仍留在 plan |
| 磁盘写计划失败 | 工具错误；不假装已落盘 |

不引入「成功假象」的 fallback：写盘失败不得改用对话正文充数。

## 16. Invariants

1. Plan 约束开启时，除 `/.harness/plan.md` 外没有任何文件 mutation 能成功。
2. 进出 plan 不增删模型可见工具名单（enter/exit 常驻）。
3. 进 plan 不更换系统提示词主体、不卸 Skill catalog、不断 MCP 连接。
4. 计划卡不能改变「下一档审批模式」；只恢复 prePlanMode 或保持 plan。
5. `/plan` 与 Shift+Tab 都不取消当前 Run。
6. 工作区与 Git 不因本功能多出计划文件。
7. 子代理拿不到这两个计划工具，也不能因为父在 plan 就获得写权。
8. TUI 与 Web 对 `/plan` 和计划交互是同一套 Controller 语义。
9. 未发版：删除空转 `PlanModeState`，不留假开关、双写或 alias 工具名。

## 17. 可观察验收

对应 Task 验收，规格层可测信号：

- Build：`/plan` 后面板审批模式为 plan；再发消息，改项目文件的工具调用失败，写 `/.harness/plan.md` 成功并出现 `~/.harness/plans/<thread_id>.md`。
- `/plan 给登录做个方案` 一次 `run.start` 且 `approval_mode=plan`、message 为该句。
- `/plan exit` 后审批模式回到进入前；从 yolo `/plan` 再 exit 回到 yolo。
- Compose 菜单无 `/plan`；输入后本地提示。
- 进行中 `/plan`：当前 Run 事件继续，下一轮才是 plan。
- `exit_plan_mode` 弹出两端可滚动预览；三选一。
- 批准：plan Run 终态，接着一条实现 Run，`approval_mode` 为 prePlanMode。
- 打回：仍为 plan，可再写计划文件。
- 放弃：plan Run 终态，模式恢复，无自动实现 Run。
- 进 plan 后仍能读 Skill 虚拟文件、调用只读 MCP；会写 MCP 失败。
- 后段：`enter_plan_mode` 先问再在同一 Run 收紧权限；`/plan-view` 打开文件；只读 `execute` 成功、写命令失败。

## 18. 非范围

与 Task 非范围相同，并明确：

- 不为无头/ACP 做进入门（无 handle 则不能进、不能交计划）。
- 不把 Qwen 的 ProceedAlways/ProceedOnce 做成产品选项。
- 不在第一版做每轮 `<system-reminder>` 注入机制。
- 不把计划写入 Transcript 之外的 SQLite artifact 表。

## 19. 测试与验证命令

协议变更必须同时覆盖 Python 派发与 TypeScript 帧处理。禁止真实模型凭据。

| 层 | 建议焦点 |
|---|---|
| CLI 命令 | `packages/cli/tests/interactive/commands.test.ts`、command-dispatcher、controller：Build/Compose、exit、带参、进行中 |
| 会话档位 | run-feature：prePlanMode、revision、Shift+Tab 与 `/plan exit` 差异 |
| 权限门 | `packages/agent/tests/policy/test_approval_policy.py`：计划文件放行、项目写拒绝、MCP、execute、task |
| 计划文件 | 虚拟路径往返、覆盖写、无工作区污染 |
| 交互 | Host `interaction.plan` 三分支、过期 revision、无 handle |
| 续跑 | 批准后自动 `run.start` 的档位与提示词；放弃不开跑 |
| 两端 UI | TUI 与 Web 对 `kind=plan` 的选项与空计划占位 |

```text
cd packages/cli && bun test
cd packages/agent && .venv/bin/python -m pytest -q
bun run protocol:check
bun run typecheck
```

改协议后先改 `packages/protocol/schema/v3.json` 再生成类型。沙箱内依赖 loopback 的 E2E 按仓库测试规范跳过并记录原因。

## 20. 文档

用户：`docs/user/交互使用.md`（命令、三个动作、与 Compose Plan 的区别）、`docs/user/安全与沙箱.md`（plan 行：计划文件例外、命令/MCP 边界；Shift+Tab 行为不变处只补 `/plan`）。

实现完成后增量更新 `docs/developer/architecture/架构总览.md` 中 Slash Registry 与审批相关段落，不另起 ADR。
