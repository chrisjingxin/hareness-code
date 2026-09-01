---
id: HC-155
title: 重做Compose流程
feature_area: Agent 工作模式与结构化研发流程
parent_task: -
decomposed_by: Grok
priority: P1
status: 待验收
owner: Grok
branch: feat/hc-155-compose-session
reviewed_at: 2026-09-01
review_due: 2026-09-15
scope: 按已确认意图重做 Compose：删除 Work Item 引擎，保留 Tab 独立 Mode 与 Grill→Task→Spec→Plan→实现→检视主路径；测试不是进度格，由实现内部 Runtime 推断并最多跑一次；检视用内置 code-review-and-quality 看本套 Compose 改过的代码并写出 review.md。主对话流式可见，Grill 用 ask_user；进度只靠 docs/compose 文档与确认；Implement / 检视用同一 Thread 的 fresh execution。不为 HC-138/140 内部实现保留兼容层。
acceptance: jsondiff 类需求几秒内开始 Grill 且不先跑意图分类器；TUI 只读进度为需求→规格→计划→实现→检视；简单需求不审 Spec、确认 Plan 后同一轮自动实现；不问用户写 verify_command；实现后进入检视 Skill；用户确认检视后结束；退出再进按文档和确认续上；中途普通输入跟当前走；/abandon 只废弃并空闲、/new-work 必须带新目标才开新需求；不再出现 compose-task-interview 类协议报错与数分钟 0 token；Build 无回归。
user_docs: docs/user/交互使用.md、docs/user/安全与沙箱.md
developer_docs: docs/developer/architecture/Compose 工作模式.md、docs/developer/architecture/架构总览.md、docs/developer/spec/HC-155-重做Compose流程.md、docs/developer/plan/HC-155-重做Compose流程.md、docs/developer/todo/HC-155-重做Compose流程.md
test_evidence: pytest -q tests/compose、bun test packages/cli/tests/presentation-shared/compose-progress-bar.test.ts、bun run protocol:check、bun run project:check
references: docs/developer/task/archive/HC-140-重构组合工作模式.md、docs/developer/task/HC-138-建立BuildCompose双.md、docs/developer/task/archive/HC-139-组合模式过程可观察.md、docs/developer/research/Compose Mode 可行性调研.md
completed_at: -
---

# HC-155 重做 Compose 流程

需求已用 `mattpocock:grill-me` 与用户确认（2026-08-17）。2026-08-18 修订：去掉用户可见的测试阶段与 `verify_command` 门禁；进度条最后一格改为检视，用内置 Review Skill 审本套 Compose 产生的代码。用户结果与验收以本文为准。

## 通俗说明

Compose 现在不是「在对话里结构化地做完一件事」，而是一套后台 Work Item 引擎：先跑意图分类，再静默拉起 Grill Agent，用 `compose-task-interview-*` 反向 RPC 提问。真实跑 `jsondiff` 时出现过 **332 秒、0 token、然后协议报错**，看起来像卡死。

根因是产品模型，不只是单个 bug：为弱模型把流程卡死，每个阶段却仍要弱模型吐完美 JSON，编排和脆弱性叠在一起。

本次重做：保留「完整研发流程」这个入口，拆掉 Work Item 账本。用户看见的是一条会说话的对话、几份可确认的文档、一条只读进度。退出再进按文档续，不从头问。

实现之后不再单独亮一格「测试」、也不问用户写测试命令。TDD 写代码时已经在测，实现结束后不再由 Runtime 再跑一轮。随后进入**检视**：用内置 `code-review-and-quality` 看这一次改过的代码，写出 `review.md`，你确认后整件事结束。

## 当前问题

- 进正事前至少一轮分类器 + 一轮后台 Grill，主界面几乎不流式，容易误以为卡死。
- Grill 走专用反向 RPC（`compose-task-interview-*`），客户端把无 `method` 的同 id 帧当成未知响应，整轮失败。
- 九项 readiness、Activity 账本、副作用对账让「继续」和「中途说一句话」都先经过重型路由。
- 把测试做成第五格、再让用户确认 `verify_command`，和 TDD 实现重复，还会把 exit code 泄漏成要人批准的命令。
- 没有正式的检视格时，架构和过大文件问题没有落点。
- HC-138 / HC-140 的内部表、类名、协议形状没有面客包袱，不必兼容。

## 用户最终得到什么

Tab 切到 Compose 后提交需求：

```text
主对话流式 Grill（ask_user 相关题可一次问，最多 5 题）
  → 写出 docs/compose/<短名>/task.md，你确认 Task（可改简单/复杂）
  → 写 Spec：简单需求不单独确认，复杂需求要确认
  → 写 Plan 和 todo.md，你一次确认（不单独确认 Todo）
  → 同一轮自动实现（fresh execution，只带已确认文档，按 Todo 勾选）
  → 同一轮进入检视（fresh execution + 内置 code-review-and-quality）
  → 写出 review.md，你确认后结束；「按意见改」退回实现
```

TUI 有只读中文进度：`需求 → 规格 → 计划 → 实现 → 检视`（进行中 / 等你 / 已确认 / 跳过 / 失败）。点已有文档可打开，不能跳阶段。

同一条会话做完一件事再提新需求，另起一套 `docs/compose/<短名>/`，旧文档保留。流程中途的普通输入跟当前走。`/abandon` 废弃当前并回到空闲（须确认一次，不带新目标）；`/new-work <目标>` 放下当前并立刻 Grill 新需求（必须带目标）。完成后的下一句也是新需求，不用命令。两者都不删文档、不回滚代码。

退出或进程结束后再进同一条 Compose 会话：已确认的不重问，实现与检视以当前仓库文件为准。不重放未完成的某次写文件或某条验证命令。

## 已确认产品决策

1. 保留 Tab 的 Build / Compose 独立 Mode；Compose 不再是第二套 Work Item Runtime。
2. 流程是 Grill → Task → Spec → Plan → 实现 → 检视。测试不是用户可见阶段，也不再要用户写或确认 `verify_command`。
3. 删除 Work Item 引擎：意图分类器、九项 readiness、Activity 账本、副作用对账。留下薄进度：文档 + 哪几份已确认 + 简单/复杂。`/abandon` 作为产品命令保留，只清当前薄进度，不做 Work Item CAS。
4. Grill / 写文档留在主对话并流式输出。Implement 与检视用**同一 Thread** 的 fresh execution；检视只注入已确认文档和本套改过的文件，加载内置 `code-review-and-quality`。不是新开 Thread，也不是每阶段压缩。
5. Grill 用主对话里的 `ask_user`，沿用现有提问组件；禁用 `compose-task-interview` 专用通道。
6. 各阶段仍加载原版内置 Skill；**先只改成用 `ask_user` 提问，其余 Skill 正文不动**，先看效果。检视阶段加载内置 Review Skill；不另建双轴 Reviewer 引擎。
7. 简单/复杂由 Grill 按三条规则写入 Task，确认 Task 时可改：只有一个可独立交付物、没有未决产品决策、不改已有架构/协议/迁移/跨模块 → 简单。简单不审 Spec；复杂要审。
8. 阶段自动衔接，不用打「继续」：确认 Task 后自动写 Spec；简单需求不审 Spec 并接着写 Plan；复杂需求确认 Spec 后自动写 Plan；确认 Plan 后同一轮自动实现；实现结束后同一轮自动检视。用户只在 `ask_user` 提问和确认门禁处停。
9. 实现过程里的 TDD 自己跑测试。实现结束后 Runtime 不再另跑验收，直接进检视。
10. 检视写出 `review.md` 后 Runtime 问是否可接受。确认 → 结束。「按意见改」不写确认，退回实现（带上 `review.md`），改完再自动回来检视。检视 Agent 只写 `review.md`，不改产品代码。
11. 中途普通输入默认修订/回答当前。`/abandon` 只停（确认后空闲，禁止带新目标）。`/new-work <目标>` 只换题（必须带新目标，禁止空跑）。完成后的下一句是新需求。不带目标的 `/new-work`、带目标的 `/abandon` 都拒绝并提示用另一个。
12. 文档目录为 `docs/compose/<短名>/`，可进 Git。
13. 退出再进只续文档和确认，不续模型半句，不对账未知写操作。
14. TUI 只读进度为中文五段「需求 → 规格 → 计划 → 实现 → 检视」；可打开已有文档，不能跳阶段。
15. 旧内部实现直接拆，不留 alias、双写、fallback、数据迁移。
16. Compose 模型上下文不包含用户自行安装的 Skill 索引，也不接受用户同名 Skill 覆盖内置阶段 Skill。系统提示与 Build 不同，只保留短 Compose 说明 + 当前阶段原版 Skill + `AGENTS.md`/项目规则。侧边栏仍可管理用户 Skill；Compose 补全不推选用用户 Skill。
17. MCP 不预注入全部 schema。Compose 强制 `tool_search` 延迟加载：模型先搜再 reveal，下一轮才出现该工具定义。Grill/规格/计划常驻为读仓库、写 `docs/compose/<slug>/`、`ask_user`、`tool_search`；实现再加写代码文件与 `execute`；检视只读仓库、只写 `review.md`。写入类 MCP 仍走审批。侧边栏仍可管理 MCP。

## 范围

- 拆掉 Compose Work Item 引擎及相关协议/SQLite/命令中仅服务该引擎的部分；Build 行为不变。
- 用文档 + 确认驱动阶段；主对话 Grill / 写 Task·Spec·Plan；确认后自动实现；实现后自动检视。
- 原版 Skill 仅改提问通道为 `ask_user`。Compose 使用独立短系统提示，不把用户 Skill 编入模型上下文。
- TUI（及与之共享状态的 Web 进度投影）只读阶段进度。
- 用户文档与 `Compose 工作模式` 架构文档改到与上述行为一致。
- 覆盖分类器不再先行、ask_user 提问、确认门禁、自动实现、内部一次验收、检视 Skill、退出再进、`/abandon` 与 `/new-work` 切分、Compose 不注入用户 Skill 索引、协议不再出现 `compose-task-interview` 未知响应的测试。

## 非范围

- 双轴并行 Reviewer、每个 Todo 单独派审。
- 检视 Agent 改产品代码。
- 用户可见的「测试」进度格，以及让用户撰写或批准 `verify_command`。
- 测试失败后自动再实现（自修循环）。
- 每阶段静默新开 Thread 或先做上下文压缩。
- 精确重放未完成的写文件或验证命令。
- 每轮先跑意图分类模型。
- 现在就改原版 Skill 里除 `ask_user` 以外的仪式（95% 盘问等先观察）。
- 进度条跳阶段。
- 为 HC-138/140 的表、类、协议形状做兼容或迁移。
- 自动 commit / PR / 发布。

## 验收项

- [x] 空闲 Tab 仍切 Build / Compose；Compose 会话提交 `写一个 jsondiff CLI，附测试和 README` 后，数秒内主对话出现 Grill（思考或 `ask_user`），不先出现一轮「只做意图分类」的独立模型调用。
- [x] Grill 问题走 `ask_user` 组件；不再发送 `compose-task-interview-*` 反向 RPC；该路径的协议报错不再出现。
- [x] 简单需求确认 Task 后不出现「请确认 Spec」门禁；复杂需求（或用户在 Task 上改为复杂）会确认 Spec。
- [x] 确认 Task 后自动开始写 Spec（简单需求不出现 Spec 确认并自动写 Plan）；确认 Spec 后自动写 Plan；确认 Plan 后同一轮开始改代码。全程无需用户再发「继续」。
- [x] 实现结束后不出现「测试」进度格，也不询问 `verify_command`，也不再由 Runtime 另跑一轮验收；同一轮进入检视。
- [x] 检视使用内置 `code-review-and-quality`，只审本套 Compose 改过的代码，写出 `review.md`；确认检视后 session 结束。「按意见改」退回实现，不让检视 Agent 改产品代码。
- [x] 确认过 Task 后退出再进同一 Thread，不会重新 Grill；实现从当前仓库接着做。
- [x] 流程中途输入普通句子不会另起一套文档。`/abandon` 确认后进度空闲、旧文档仍在、下一句才是新需求；`/new-work <目标>` 保留旧文档并立刻 Grill 新需求。空 `/new-work` 与带目标的 `/abandon` 被拒绝。
- [x] TUI 显示中文五段进度「需求 → 规格 → 计划 → 实现 → 检视」，能区分进行中 / 等你 / 已确认 / 跳过 / 失败；点击不能把流程跳到未达阶段。
- [x] Work Item 引擎（分类器先行、九项 readiness、Activity 账本）不再是 Compose 主路径；Build 回归通过。
- [x] Compose 主 Agent、Implement 与检视的系统提示不含 `<harness_available_skills>` 用户 Skill 索引；阶段 Skill 只解析内置 identity。Build 的用户 Skill 行为不变。侧边栏仍能管理用户 Skill。
- [x] Compose 强制 MCP/`tool_search` 延迟加载：首轮绑定不见 MCP 完整 schema；`tool_search` 命中后下一轮才出现该工具。Grill 常驻不含 `execute`。写入 MCP 仍审批。Build 的 defer 配置不被本任务改掉默认行为。
- [x] `docs/user/交互使用.md`、`docs/user/安全与沙箱.md` 与 `docs/developer/architecture/Compose 工作模式.md` 与上述行为一致。

## 后续文档

Task 确认后写同名 Spec / Plan / Todo。实现按可演示停点交付，不得一次做完整份 Todo。
