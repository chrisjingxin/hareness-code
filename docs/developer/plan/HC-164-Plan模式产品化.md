# HC-164 Plan 模式产品化实施计划

关联：[Task](../task/HC-164-Plan模式产品化.md) · [Spec](../spec/HC-164-Plan模式产品化.md) · [Todo](../todo/HC-164-Plan模式产品化.md)

不新增范围。一次只做到下一个可演示停点。行为以 Spec 为准。

## 通俗怎么拆

先让用户在 Build 里能 `/plan` 进出，下一轮真的是 plan。  
再打通「写出计划文件 → 滚动审 → 批准后按原权限开干」。  
后段三件各自能看见再做：点头才进、行批注/`/plan-view`、只读命令。

```text
停点 1  /plan、/plan <目标>、/plan exit；Compose 没有；当前轮不取消
  │
停点 2  计划文件 + 权限门 + 交计划三选一 + 批准后续跑（TUI 与 Web）
  │
停点 3  模型提议，你点头才在同一轮进入计划
  │
停点 4  行批注 + /plan-view
  │
停点 5  计划模式下只读命令可跑
```

停点 2 必须纵向一次做完（协议、读文件提交、两端预览、Host 终态、CLI 续跑）。拆开用户会卡在「有文件没审批」或「有卡但不能开干」。

## 已锁定的做法（不在实现时重开）

- 新交互 `interaction.plan`（`approved` / `revise` / `abandoned`），不复用工具审批那五个 decision。
- 模型路径 `/.harness/plan.md`；磁盘 `{home}/.harness/plans/{thread_id}.md`。
- 工具名仍是 `enter_plan_mode` / `exit_plan_mode`；常驻 schema；删除空转 `PlanModeState`。
- `/plan exit` 恢复 prePlanMode；Shift+Tab 离开 plan 走到循环的 `default` 并丢掉 prePlanMode。
- 批准/放弃：Host 写完工具结果后结束该 plan Run；CLI 再决定是否自动 `run.start`。
- 进 plan 不换系统提示词主体、不卸 Skill、不断 MCP。
- 第一版 `execute` 硬拒；未知 MCP 未声明只读则拒。

## 停点 1 — `/plan` 能进出

**改什么：** `RunFeature` 增加 `prePlanMode` / `planRevision`；Shift+Tab 进入 plan 时记下上一档，离开 plan 时清掉并走到 `default`。注册 `approval.plan`：Build 可见、不要 `requiresIdle`。`set-approval-mode` / `restore-approval-mode` 由 Controller 解释。空参切档；整段 `exit` 恢复；其它参数先切档再 `submit-prompt`。已在 plan 的空 `/plan` 只 notice。进行中只改下一轮覆盖值。Compose hidden，输入提示「仅在 Build 可用」。`enter_plan_mode` 改为立即返回「请用 `/plan`」，删 `PlanModeState`。用户文档先写命令。

**为什么：** 没有入口就谈不上闭环；假状态必须在第一停点清掉，避免后面还当它是真切档。

**怎么验证：**

```text
cd packages/cli && bun test tests/interactive/commands.test.ts tests/interactive/run-feature.test.ts tests/interactive/controller.test.ts
cd packages/agent && .venv/bin/python -m pytest -q tests/tools/test_tools_memory.py tests/policy/test_tool_risk.py
```

断言：Build `/plan` 后下一次 `run.start` 的 `approval_mode=plan`；`/plan 给登录做个方案` 同一次提交；yolo 下 `/plan` 再 `exit` 回到 yolo；Compose 不出现该命令；进行中 `/plan` 不取消当前 Run；`enter_plan_mode` 不改审批模式。

**可演示：** `bun run dev`，Build 下 `/plan`，右下角变成 plan；发一条「看一下仓库结构」，应只读不改文件。`/plan exit` 回到进来前的档位。Tab 到 Compose，菜单没有 `/plan`。

## 停点 2 — 交计划、审完、开干

**改什么：**

1. 虚拟后端对 `/.harness/plan.md` 在 Plan 约束下允许 `write_file`/`edit_file`（先读再改），映射到 `~/.harness/plans/<thread_id>.md`；`delete_file` 拒绝；工作区不出现该文件。
2. `PlanModeMiddleware`：放行计划文件路径；其它写入/`execute`/`task`/`memory_save`/非只读 MCP 硬拒并说明。审批模式后缀补一句：写到 `/.harness/plan.md`，写完调 `exit_plan_mode`。
3. `packages/protocol/schema/v3.json` 增加 `interaction.plan` 与 handle `"plan"`；生成类型。TUI/Web `handles` 带上 `plan`；无头不带。
4. `interrupt_on_for_approval_mode` 在 plan 下也要能停住 `exit_plan_mode`。工具无参，读虚拟文件，空正文也发起交互，带 `planRevision`。
5. Host：`approved`/`abandoned` 且 revision 有效 → 工具结果后 `run.completed`；`revise` 回反馈并继续；revision 过期不切档；超时/断开不当成退出 plan。
6. 共享文案与选项顺序；TUI 底部操作栏、Web 交互卡：可滚动 markdown、空计划占位、三选一；打回可带文字。
7. CLI：批准则恢复 prePlanMode（缺省 `default`）并自动 `run.start` 固定实现提示；放弃只恢复；打回不动。
8. 用户文档补三个动作、计划文件位置、与 Compose Plan 的区别；安全文档补计划文件例外。架构总览 Slash / 审批段落增量一句。

**为什么：** 这才是用户能走完的闭环。协议和两端 UI 必须同停点，否则一边能审一边不能。

**怎么验证：**

```text
bun run protocol:generate && bun run protocol:check
cd packages/agent && .venv/bin/python -m pytest -q tests/policy/test_approval_policy.py tests/tools tests/host
cd packages/cli && bun test tests/interactive tests/presentation-shared tests/tui tests/web
bun run typecheck
```

断言：plan 下写 `/.harness/plan.md` 落盘到 `~/.harness/plans/<id>.md`，写项目文件失败；`exit_plan_mode` 弹出三选一；批准后下一条 Run 的 `approval_mode` 为 prePlanMode；放弃无自动 Run；无 `plan` handle 时工具错误不停交互。

**可演示：** `bun run dev`，`/plan 给登录做个方案`。等 Agent 写出计划并弹出预览：批准后应开始改代码且档位是进来前那档；再开一次，打回后仍是 plan、项目文件仍不能改；放弃则退出 plan、不写代码。`/web` 同一套三个按钮。工作区 `git status` 没有计划文件。

## 停点 3 — 点头才进计划

**改什么：** `PlanModeMiddleware` 读「当前 Run 的 Plan 约束」（构图 `approval_mode=plan` **或** 运行时 flag），不要只靠构图枚举。`enter_plan_mode` 走现有 `interaction.approval`（是/否）。同意：打开本 Run flag、种子空计划文件（已有不截断）、返回规划步骤。拒绝：不进。无审批 handle 则拒绝进入。不得结束 Run 再以 plan 重开。

**为什么：** `/plan` 解决不了对话中途「先做计划」；必须同一轮收紧权限。

**怎么验证：** Python：同一 Run 内同意后写项目文件被拒、写计划文件成功；拒绝后仍可按原档位写（受原审批模式约束）。CLI：审批卡文案是进入计划，不是审计划正文。

**可演示：** 不要先 `/plan`。在 default 下说「先别写代码，做个计划」。弹出是否进入；同意后只规划；拒绝后行为与现在一致。

## 停点 4 — 行批注与再打开计划

**改什么：** 计划预览可选行范围写意见；`revise` 的 feedback 带行号与摘录；批准时若有批注，实现轮提示附上。新命令 `approval.plan-view`（`/plan-view`，无别名）：打开当前 thread 计划；无文件 notice；交互挂起则回到该预览。

**为什么：** 打回要能指着某一段说；规划中途也要能再看那份文件。

**怎么验证：** CLI 命令测试；预览把批注编进 feedback 的单测。TUI/Web 各一条 focused 测试。

**可演示：** 计划弹出后对某行写「这段不要动」，打回；模型下一轮应看得到。空闲时 `/plan-view` 打开已写的计划。

## 停点 5 — 只读命令

**改什么：** Plan 约束下 `execute`：只读放行、会写硬拒、未知走现有 `interaction.approval` 单次问（批准不改变 Plan 约束和档位）。复用现有只读 Shell 判定，不够再补解析。

**为什么：** 规划有时需要 `git status` / `ls`；第一版一刀切会逼模型瞎猜。

**怎么验证：** `tests/policy`：只读成功、`rm` 失败、未知弹审批且批准后仍不能写项目文件。

**可演示：** plan 下让 Agent 跑 `git status` 应成功；跑会改文件的命令应被拒并提示写进计划。

## 风险

| 风险 | 处理 |
|---|---|
| plan 下 `extra_interrupt_tools` 被忽略，提交门弹不出 | 停点 2 改 `interrupt_on_for_approval_mode`；测试断言 `exit_plan_mode` 在 interrupt 集合中 |
| 虚拟后端只读，计划文件写不进去 | 停点 2 按路径放行写，仍走 Snapshot；测试落盘与工作区无该文件 |
| 批准后模型仍在 plan 图里再想一轮，写文件全被拒 | Host 在 approved/abandoned 后结束 Run，由 CLI 开实现轮 |
| 自动续跑与用户新输入抢 Run | 实现轮只在 plan Run 终态且空闲时启动；已有 activeRun 则只恢复档位并 notice |
| 后段运行时 flag 与构图枚举分叉 | 停点 3 把门的判定收成一个函数：枚举 plan **或** flag |
| 子代理误拿到计划工具 | 不停 HC-161 排除；停点 2/3 回归 child 无这两个工具 |
