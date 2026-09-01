# HC-164 Plan 模式产品化执行清单

关联：[Task](../task/HC-164-Plan模式产品化.md) · [Spec](../spec/HC-164-Plan模式产品化.md) · [Plan](../plan/HC-164-Plan模式产品化.md)

每项写清改什么、跑哪条测试、期望看见什么。一个停点做完即停，勾选并写 `tmp/handoff.md`，等用户看过再做下一段。

---

## 停点 1：`/plan` 能进出

用户怎么看：`bun run dev`，Build 下输入 `/plan`，右下角变成 plan；发一条只读问题，Agent 不应改项目文件。`/plan exit` 回到进来前的档位。Tab 到 Compose，菜单没有 `/plan`。`/plan 给登录做个方案` 应直接开跑且模式是 plan。

- [x] 会话档位：`RunFeature` 增加 `prePlanMode`、`planRevision`。进入 plan（`/plan` 或 Shift+Tab）时记下上一档并递增 revision；`/plan exit` 恢复上一档；Shift+Tab 离开 plan 走到 `default` 并清空 `prePlanMode`。focused：`tests/interactive/run-feature.test.ts`（若无则新建）。
- [x] 命令结果：`CommandResult` 增加 `set-approval-mode` / `restore-approval-mode`；Controller 先切档再 `submit-prompt`。进行中切档不取消当前 Run。
- [x] 注册 `/plan`：id `approval.plan`，`workModes: ["build"]`，不要 `requiresIdle`。空参切档；trim 后整段 `exit`（忽略大小写）恢复；其它参数当目标提交。已在 plan 的空 `/plan` 只 notice。Compose hidden，输入提示「`/plan` 仅在 Build 工作模式可用。」`commands.test.ts` + dispatcher/controller 测试。
- [x] `enter_plan_mode` 立即返回请用 `/plan` 或 Shift+Tab，不切 Host 模式。删除 `PlanModeState` 及假 enter/exit 实现，测试与展示文案改指向「提示去 `/plan`」。Python focused：原 `test_plan_mode_enter_exit` 一类改为新语义。
- [x] `docs/user/交互使用.md` 写 `/plan` / `/plan <目标>` / `/plan exit`，并写明与 Compose Plan 不是一回事。Shift+Tab 说明保持，只补命令。
- [x] 完成信号：`cd packages/cli && bun test tests/interactive`；相关 Python tools 测试；`bun run typecheck`。手动 `bun run dev` 待用户按「用户怎么看」走一遍。

---

## 停点 2：交计划、审完、开干

用户怎么看：`/plan 给登录做个方案`，等预览弹出。批准 → 开始改代码，档位是进来前那档。再来一次打回 → 仍是 plan。放弃 → 离开 plan、不写代码。`/web` 同样三个按钮。`git status` 工作区没有计划文件；`~/.harness/plans/` 下有对应 md。

- [x] 计划文件：虚拟路径 `/.harness/plan.md` ↔ `{home}/.harness/plans/{thread_id}.md`。Plan 约束下允许 write/edit（Snapshot 仍要先读），禁止 delete。目录 `0700`，覆盖写。测试：落盘成功、工作区无该文件、约束关闭时写拒绝。
- [x] 权限门：`PlanModeMiddleware` 按路径放行计划文件，其它 mutation / `execute` / `task` / `memory_save` / 非只读 MCP 硬拒并说明唯一可写路径。审批模式后缀补写计划和 `exit_plan_mode`。`test_approval_policy.py`。
- [x] 协议：`v3.json` 增加 `interaction.plan` 与 handle `"plan"`。`bun run protocol:generate && bun run protocol:check`。TUI/Web initialize `handles` 含 `plan`；无头不含。
- [x] 提交门：plan 下 interrupt 集合包含 `exit_plan_mode`（修正忽略 extra 的行为）。工具无参、读虚拟文件、空也弹卡、带 revision。非 plan 或不支持 handle 则工具错误、不停交互。常驻 schema。
- [x] Host：`approved`/`abandoned` 且 revision 有效 → 工具结果后结束 Run；`revise` 带反馈继续；过期 revision 不切档；超时/断开收起预览但仍留在 plan。Python host 测试。
- [x] 共享展示：`presentation-shared` 三选项中文标签（批准并开始实现 / 继续打磨 / 放弃计划）。禁止出现 auto-edit 等档位选项。
- [x] TUI：底部操作栏可滚动预览、空计划占位、打回可输入 feedback。Web：同一 payload 画交互卡。两端 focused 测试。
- [x] 续跑：CLI 在 plan Run 终态后，批准则 restore + 自动 `run.start`（固定实现提示、含 `/.harness/plan.md`）；放弃只 restore；打回不动。已有 activeRun 则不抢。controller / run-feature 测试。
- [x] 文档：`交互使用.md` 三个动作；`安全与沙箱.md` 计划文件例外与命令/MCP 边界；`架构总览.md` Slash/审批各补一句。
- [x] 完成信号：上列 focused tests；`bun run typecheck`。手动 `bun run dev` 待用户按「用户怎么看」走批准/打回/放弃各一次。

---

## 停点 3：点头才进计划

用户怎么看：不要先 `/plan`。default 下说「先别写代码，做个计划」。先问是否进入；同意后只能规划；拒绝后与现在一样。

- [x] Plan 约束判定收成一处：`approval_mode==plan` **或** 本 Run 运行时 flag。构图不再是唯一来源。
- [x] `enter_plan_mode` 改为 HITL 是/否（现有 `interaction.approval`）。同意：置 flag、种子空计划文件（已有不截断）、返回规划步骤。拒绝：不进。无审批 handle 则拒绝。禁止结束 Run 重开。
- [x] 测试：同一 Run 同意后写项目失败、写计划文件成功；拒绝后不打开 Plan 约束。child 仍无该工具。
- [x] 完成信号：相关 Python + CLI 交互测试；按「用户怎么看」走同意/拒绝各一次。

---

## 停点 4：行批注与 `/plan-view`

用户怎么看：计划预览里对某行写意见再打回，模型下一轮看得到。空闲 `/plan-view` 打开已写计划。

- [x] 预览支持行范围批注；`revise` 的 feedback 含行号、摘录、意见；批准时若有批注，实现轮提示附上。
- [x] `/plan-view`（id `approval.plan-view`，无别名）：Build；无文件 notice；挂起的计划交互则回到该预览。`commands.test.ts`。
- [x] TUI/Web focused 测试覆盖批注编入 feedback。
- [x] `交互使用.md` 补 `/plan-view` 与行批注。
- [x] 完成信号：上列测试；按「用户怎么看」走一遍。

---

## 停点 5：只读命令

用户怎么看：plan 下 `git status` 能跑；会改文件的命令被拒并提示写进计划。

- [x] `execute` 在 Plan 约束下：只读放行、会写硬拒、未知走现有审批单次问（批准不改变约束和档位）。复用现有只读判定，不够再补。
- [x] `test_approval_policy.py`（或邻近 execute 测试）：只读成功、破坏性失败、未知弹窗且批准后仍不能写项目文件。
- [x] `安全与沙箱.md` 更新 plan 行：只读 Shell 不再整行「拒绝」。
- [x] 完成信号：上列测试；按「用户怎么看」走一遍。
