---
id: HC-164
title: Plan模式产品化
feature_area: 会话内 Plan 模式
parent_task: -
decomposed_by: Grok
priority: P1
status: 进行中
owner: Grok
branch: feat_hc_164_Plan模式产品化
reviewed_at: 2026-08-31
review_due: 2026-09-14
scope: 在 Build 工作模式把已有 plan 审批模式做成可走完的产品闭环：/plan 进出与带目标开跑、规划中只写会话计划文件、滚动审计划后批准/打回/放弃、批准后恢复进入前权限并开始实现；TUI 与 Web 一致。同一 Task 后段再补点头才进计划、行批注与再打开计划、计划模式下只读命令。
acceptance: Build 下 /plan、/plan <目标>、/plan exit 可用且 Compose 不出现；Shift+Tab 仍含 plan；规划中项目文件不可改、计划文件可写；交出来可滚动预览并三选一；批准后回到进入前审批档位并开始改代码；打回留在 plan；放弃退出 plan 且不写代码；正在跑的一轮不被 /plan 取消；进 plan 不卸 Skill/MCP、不换系统提示词主体。
user_docs: docs/user/交互使用.md、docs/user/安全与沙箱.md
developer_docs: docs/developer/spec/HC-164-Plan模式产品化.md、docs/developer/plan/HC-164-Plan模式产品化.md、docs/developer/todo/HC-164-Plan模式产品化.md、docs/developer/research/163-Plan模式产品化.md、docs/developer/architecture/架构总览.md
test_evidence: -
references: docs/developer/research/163-Plan模式产品化.md、docs/developer/task/HC-161-内置子代理可用化.md、docs/developer/task/archive/HC-163-本地诊断日志体系.md
completed_at: -
---

# HC-164 Plan 模式产品化

需求已用 `mattpocock:grill-me` 与用户确认（2026-08-31）。用户结果与验收以本文为准。调研原文编号为 `163`（research 不强制 `HC-`）；Task 编号不能用 HC-163，该号已是归档任务「本地诊断日志体系」。

## 通俗说明

现在切到 plan，Agent 确实不能改项目文件，但用户没有正经入口，也没有「计划写完你看一眼再动手」这一步。只能靠 Shift+Tab 切档，计划停在对话里的一段话，看完还得自己再切回去写代码。

本任务把会话内 Plan 做成一条能走完的路：

```text
/plan（或以后点头同意 Agent 的提议）
  → Agent 只调查、把计划写成一份文件
  → 你滚动看这份计划
  → 批准：回到进计划前的权限，开始改代码
    打回：继续改计划，还是不能动项目
    放弃：退出计划，不写代码
```

这和 Compose 里「确认 plan.md 再实现」不是一回事。Compose 是结构化研发流程的一扇门；本功能是 Build 会话里的审批模式产品化。第一版 `/plan` 只在 Build 出现。

## 当前问题

- 进入只有 Shift+Tab 五档循环，没有 `/plan`，也没有「带着目标直接开始规划」。
- `enter_plan_mode` / `exit_plan_mode` 只改一块没人读的本地状态，切不了 Host 审批模式，也交不出计划。
- 没有计划文件、没有计划预览、批准后也不会自动按原权限开干。
- 用户随口说「先做计划」时，系统既不会切到只读规划，也不会问一句要不要进。

## 用户最终得到什么

在 **Build** 工作模式：

1. 空闲时 `/plan` 切到计划模式，下一条消息才开始规划；`/plan 给登录做个方案` 立刻带着这句话开跑；`/plan exit` 回到进计划前的审批档位。已经在计划里再打 `/plan` 只提示已在计划中。**正在跑的那一轮不受影响**，新模式从下一轮生效（与现在的 Shift+Tab 相同）。Compose 下菜单不出现 `/plan`，输入了也拒绝。
2. Shift+Tab 仍按 `plan → default → auto-edit → auto → yolo` 循环，与 `/plan` 并存。
3. 进了 plan，还是原来那个 Agent：系统提示词主体、Skill 目录、已连接的 MCP 都不换，只多一句「正在规划」。真限制在权限上——项目文件不能改（只能写那份计划）、会改系统的命令不能跑、会写东西的 MCP 拒绝；Skill 仍可当资料读，只读 MCP 能用。
4. 计划写在 `~/.harness/plans/<thread_id>.md`（不进仓库）。规划过程中就写这份文件，同一 thread 再次批准则覆盖。交出来时 TUI 和 Web 都是可滚动预览，三个动作：批准并开始实现 / 打回继续改计划 / 放弃并退出计划。空计划也要能做出这三个选择，不能卡死。
5. 点批准：关掉计划模式，**恢复进入计划之前的那一档权限**（进 plan 前是默认确认，批准后还是默认确认），并开始改代码。计划卡不负责让你改选 auto-edit 或 yolo。
6. 点打回：可附自由文本意见；Agent 留在 plan 里改计划。点放弃：退出 plan，不开始写代码。

同一任务后段用户还能：

- 随口说「先做计划」时，Agent **先问你要不要进**；同意才进入只读规划，拒绝则保持当前模式。第一版这条路做成运行时计划门，不靠结束当前 Run 再重开。
- 对计划某一行写意见，以及随时用命令再打开这份计划。
- 计划模式下可以跑判定为只读的命令；会改系统的命令仍拒绝。

## 已确认产品决策

1. 一个完整功能 = 一个 Task（HC-164）；`/plan` 是第一个可演示停点，不是独立任务。
2. 只在 Build；Compose 的 Plan 阶段不挂本命令。
3. plan 仍是现有五档审批模式之一，不另起「换工具集 / 换规划模型」的独立模式。
4. `/plan` 行为取 Q10-A：空闲切档、带参即开跑、`exit` 恢复、已在计划中提示、当前 Run 不取消。
5. 批准后的权限取 Q9-A：恢复进入前档位，不在计划卡上再选下一档（不用 Qwen 那张权限菜单）。
6. 计划是磁盘上的文档（规划中就写），不是工具参数里临时一段字、批准时才落盘。
7. 进 plan 后身份保留、权限收紧（Q11-A）：不卸 Skill、不断开 MCP、不换系统提示词主体。
8. `execute` 第一版维持硬拒；同一 Task 后段再做只读命令判定（AST：只读放行、写硬拒、未知单条审批）。
9. TUI 与 Web 一起交付，走共享 Interactive Core。
10. 自然语言进入不做第一停点，但必须在同一 Task 补上（模型提议 + 用户点头）。
11. 行批注与「再打开计划」不做第一版审批停点，同一 Task 后段再补。

## 建议的可见停点（供后续 Plan/Todo，不在本阶段实现）

1. 先能 `/plan` 进出和带目标开跑，Shift+Tab 仍可用。
2. 再能写出计划文件、滚动审、三选一、批准后按原权限开干（含 Q11-A 的权限边界）。
3. 同一任务后段再补：点头才进计划、行批注 + 再打开计划、只读命令。

实现按停点交付；未经用户要求不得一次做完整份 Todo。

## 范围

1. Build 下 `/plan` / `/plan <目标>` / `/plan exit` 的命令、切档、带参提交与可用性（含 Web）。
2. 规划中唯一可写路径为会话计划文件；项目内写入硬拒；会改系统的命令与会写的 MCP 硬拒；Skill 与只读 MCP 保留。
3. 计划提交后的滚动预览与三个结果（批准 / 打回 / 放弃），TUI 与 Web 一致；批准后续跑并恢复进入前审批档位。
4. 将现在空转的退出侧工具改成「读计划文件并弹出审批」的门；进入侧工具第一停点可仍不生效，后段做成真正的点头确认，不要先删再加。
5. 用户文档写清 `/plan`、与 Compose Plan 的区别、计划文件位置和三个动作。
6. 同一 Task 后段：点头才进计划、行批注、再打开计划、只读命令判定。

## 非范围

- 规划 / 执行分模型、批准后压缩会话或新开会话。
- 把计划注入子代理；计划模式下派生子代理。
- 全屏批注评审（Oh My Pi 量级）作为第一版审批面。
- 无头 / ACP 专用进入门。
- Compose 下提供 `/plan`。
- 把计划写进项目目录或 Compose 的 `docs/compose/`。
- 进 plan 时卸掉 Skill、断开 MCP、或换成另一份系统提示词。
- 改 Shift+Tab 循环顺序，或从循环中拿掉 plan。

## 可观察验收

- Build 输入 `/plan` 后，状态栏/模式显示为 plan；再发一条消息，Agent 进入只读规划（改项目文件会被拒）。
- `/plan 给登录做个方案` 一次完成切档并提交该句。
- `/plan exit` 恢复进入前的审批档位。
- Compose 下 `/plan` 不可用（菜单隐藏或输入后提示仅 Build）。
- Shift+Tab 仍能切到 plan；正在执行时切档或打 `/plan`，当前轮继续，下一轮才用新模式。
- 规划过程中 `~/.harness/plans/<thread_id>.md` 出现或更新；工作区 Git 不因此多出计划文件。
- 计划交出来后，TUI 与 Web 都能滚动看正文，并选择批准 / 打回（可带文字）/ 放弃。
- 批准后：模式回到进入 plan 前的那一档，Agent 开始按计划改代码；打回后仍为 plan，项目文件仍不能改；放弃后离开 plan 且不开始改代码。
- 进 plan 后仍能读已启用 Skill、调用只读 MCP；调用会写的 MCP 或会改系统的命令失败且有说明。
- 后段验收（可晚于前两停点）：说「先做计划」会先问要不要进；可对某一行写意见；可用命令再打开计划；只读 shell 能跑、写命令仍拒。

## 与其它任务的关系

- [HC-161](./HC-161-内置子代理可用化.md) 禁止子代理拥有 `enter_plan_mode` / `exit_plan_mode`。本任务只把主 Agent 这两扇门做成真能力，**不**把它们下放给子代理。
- Compose 流程见 [HC-155](./HC-155-重做Compose流程.md)；本任务不改 Compose 的 Plan 确认门。
- 调研：[163-Plan模式产品化](../research/163-Plan模式产品化.md)（含 Qwen / Oh My Pi）。grill 中补充对照了 Grok Build 的计划文件、三动作审批与「身份不动、权限收紧」。产品取 Grok 的审计划方式，不取 Qwen 在卡上选下一档权限。

## 文档与架构

用户可感知变更写入 `docs/user/交互使用.md`（命令与三个动作）和 `docs/user/安全与沙箱.md`（plan 模式下计划文件例外、命令/MCP 边界）。规格见 [Spec](../spec/HC-164-Plan模式产品化.md)，步骤见 [Plan](../plan/HC-164-Plan模式产品化.md)，勾选见 [Todo](../todo/HC-164-Plan模式产品化.md)。架构结论增量写入 `docs/developer/architecture/` 中审批模式或交互相关文档，不另起平行 ADR。
