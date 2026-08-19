# HC-155 重做 Compose 流程执行清单

关联：[Task](../task/HC-155-重做Compose流程.md) · [Spec](../spec/HC-155-重做Compose流程.md) · [Plan](../plan/HC-155-重做Compose流程.md)

每项写清改什么、跑哪条测试、期望看见什么。一个停点做完即停，等用户看过再做下一段。

---

## 停点 1：主对话 Grill

用户怎么看：`bun run dev`，Tab 到 Compose，提交「写一个 jsondiff CLI，附测试和 README」。数秒内出现思考或 `ask_user`；右上/进度为「需求 · 进行中」；不得出现 `Unknown JSON-RPC response id: compose-task-interview-*`。

- [x] 协议：删除 `compose.work_item` / `compose.activity` 与旧五阶段 `compose.state`；新增 `compose.progress`（Spec 6.6）。`compose.abandon` 参数改为 `thread_id` + 可选 `reason`。`threads.open` 改为可选 `compose_progress`。运行 `bun run protocol:generate && bun run protocol:check`，TS/Python 类型一致。
- [x] 薄进度存储：新表 `harness_compose_sessions`（schema v17）。旧 Work Item 表本停点未 DROP（避免持久化校验大面积失败），停点 4 再拆。测试：首轮创建 slug 与 grill 进度。
- [x] `ComposeSession`：`execute_turn` / `inspect` / `abandon`。首条消息分配 slug、写空目录、派生阶段为 grill。不调用 TurnIntent 分类器。
- [x] `ComposeRunAdapter` 改为只调 Session；Grill 走主 Agent 流式输出。提问走 ask_user。测试断言 progress 事件且无 compose.work_item / compose-task-interview。
- [x] 短 Compose 系统提示已注入用户消息；用户 Skill 索引与 Grill 工具裁剪（不含 execute、强制 defer）已在构图层完成。
- [x] 原版 `grill-me`：凡提问改为调用 `ask_user`，其余正文不动；manifest digest 已更新。
- [x] Interactive Core + TUI：用 `compose.progress` 画中文进度。第五格文案在停点 3 从「测试」改为「检视」。
- [x] 聚焦测试：session / adapter / protocol / interactive state 与 run-feature 相关用例已通过。

---

## 停点 2：确认门禁与自动写 Spec/Plan

用户怎么看：答完 Grill 后出现「确认 Task」；确认（简单）后**不要再打字**，自动写出 Spec 与 Plan+Todo，再出现「确认计划」。进度：需求已确认，规格跳过，计划等你。

- [x] 用户说「推进/进入下一阶段/确认」时不再 Grill，写入 task.md 并进入规格阶段（session 测试已覆盖）。
- [x] 阶段 Agent 本轮结束后 Runtime 用 ask_user 问「这份产出是否符合预期」：确认进入下一阶段，「我要改」不写确认。需求访谈结束后即使只有 task.md 也要弹确认，不能把用户丢回输入栏。
- [x] `ask_user` 相关题可一次问、最多 5 题；TUI Dock 逐题作答后一次提交，不再给未答题填 `(no answer)`。
- [x] 已有 spec.md 时再确认进入计划，不得再跑 Spec（`test_confirming_existing_spec_enters_plan_not_spec`）。
- [x] Runtime 确认选项支持 complexity（简单跳过 Spec 确认 / 复杂要审 Spec）。
- [x] 自动衔接写完整 Spec/Plan 文档；确认后自动进入下一阶段。
- [x] 删除「缺 `verify_command` 就 ask_user 补问」门禁。确认 Plan 不再要测试命令。
- [x] digest 变化使对应确认失效。Todo 勾选不使 Plan 确认失效。
- [x] 聚焦测试：`pytest -q tests/compose` 覆盖 simple/complex/digest 过期。

---

## 停点 3：自动实现与检视

用户怎么看：确认计划后同一轮开始改代码。实现结束后不亮「测试」、不问测试命令；进度走到「检视」，出现 `review.md` 确认。

- [x] Plan 确认后同一 `execute_turn` 启动 implement：独立 checkpoint namespace，输入只拼已确认 `task.md`/`spec.md`/`plan.md`/`todo.md`，时间线应开始吐思考或写文件。
- [x] 实现 Skill 为内置 `test-driven-development`（提问则 ask_user）。工具含工作区写文件、`execute`、`write_todos`、`tool_search`；仍强制 MCP defer。
- [x] 实现结束后不再 Runtime 验收：不推断、不跑第二次测试、不画验收卡；直接进入检视。
- [x] 检视：fresh execution，注入已确认文档 + 本套改过的文件，加载内置 `code-review-and-quality`。只写 `docs/compose/<slug>/review.md`。就绪后 ask_user：确认结束 / 按意见改（退回实现并带上 review.md）。
- [x] 协议与 TUI：`stages` 第五格为 `review` / 「检视」。确认 review 后 session completed。
- [x] 聚焦测试：实现后调用 review、不调用 Runtime verify；确认 review → completed；按意见改 → 再 implement。

---

## 停点 4：命令、恢复、拆旧、文档

用户怎么看：`/abandon` 确认后进度空闲、旧目录还在；空 `/abandon` 才合法。`/new-work 写 HTTP 服务` 立刻新 Grill；空 `/new-work` 被拒绝。确认 Task 后退出再进，不重新 Grill。

- [x] 路由表按 Spec 第 5 节：中途普通输入跟当前；`COMPOSE_ABANDON_TAKES_NO_GOAL` / `COMPOSE_NEW_WORK_GOAL_REQUIRED` / `COMPOSE_NOTHING_TO_ABANDON`。`/abandon` 先走命令确认。
- [x] 重启后 `inspect` / 下一 Turn 从确认进度派生，不进 Grill。
- [x] 主路径全面切到 `ComposeSession` 语义，旧协议与事件完全移除。
- [x] 更新 `docs/user/交互使用.md`、`docs/developer/architecture/Compose 工作模式.md`。`安全与沙箱.md` 无需改权限语义。
- [x] `bun run protocol:check && bun run project:check`；agent compose 聚焦 + cli 相关测试通过。整任务交付前已跑 `bun run typecheck` 与 `bun run test`。
