# HC-155 重做 Compose 流程实施计划

关联任务：[HC-155](../task/HC-155-重做Compose流程.md)  
规格：[HC-155 Spec](../spec/HC-155-重做Compose流程.md)  
清单：[HC-155 Todo](../todo/HC-155-重做Compose流程.md)

不新增范围。实现一次只做到下一个可演示停点，勾选本段 Todo 后停下。

## 通俗怎么拆

先让 Compose 重新「会说话」：主对话 Grill + `ask_user` + 中文进度，分类器和 `compose-task-interview` 消失。  
再接上确认门禁和自动写 Spec/Plan。  
再接上确认 Plan 后自动实现、内部一次验收、自动检视。  
最后补 `/abandon`、`/new-work`、退出再进，拆掉 Work Item 残骸并改文档。

```text
停点 1  主对话 Grill（能看见、能提问、不报协议错）
  │
停点 2  确认 Task 后自动写 Spec/Plan（不用「继续」）
  │
停点 3  确认 Plan 后自动实现 + 内部一次验收 + 检视 Skill
  │
停点 4  命令切分、退出再进、拆旧引擎、用户/架构文档
```

跨 Protocol / Agent / CLI 的同一行为留在同一步，不按包横着切。

## 已锁定的做法（不在实现时重开）

- `ComposeSession` 替换 `ComposeWorkItemEngine`；阶段由文档 + 确认派生。
- Grill/Spec/Plan 走本 Run 主 Agent；实现与检视是同一 Thread 的 fresh execution。
- 提问只走 `ask_user`。原版 Skill 只改提问通道。检视加载内置 `code-review-and-quality`。
- 测试不是 UI 阶段；不问 `verify_command`；失败不自动再实现。
- Compose 短系统提示；不注入用户 Skill；MCP 强制 `tool_search` reveal。
- 协议用 `compose.progress` 替换 work_item / activity / 旧五阶段 state。UI 五段为需求 → 规格 → 计划 → 实现 → 检视。
- 旧 Work Item 表 DROP，不迁移。

## 停点 1 — 主对话能 Grill

**改什么：** 新增薄进度存储与 `ComposeSession`；`ComposeRunAdapter` 改为 `execute_turn` 驱动主 Agent（grill-me + 短 Compose 提示），禁止 TurnIntent 分类器。协议改为 `compose.progress`。`grill-me` 提问改为 `ask_user`。TUI 画中文进度（此时停在「需求」）。Grill 常驻工具：读仓库、写 `docs/compose/<slug>/`、`ask_user`、`tool_search`；强制 defer=on。

**为什么：** 用户最先痛的是 332 秒 0 token 和 `compose-task-interview`。先把「几秒内看见访谈」做出来。

**怎么验证：**

```text
cd packages/agent && .venv/bin/python -m pytest -q tests/compose tests/host/test_compose_adapter.py
cd packages/cli && bun test tests/interactive tests/tui
bun run protocol:generate && bun run protocol:check
```

假模型路径：首条 Compose 消息不调用 classifier；wire 上有 `interaction/question` 且无 `compose-task-interview-`；有 `compose.progress`。

**可演示：** `bun run dev`，Tab 到 Compose，提交 jsondiff 类需求。数秒内时间线出现思考或 `ask_user`；进度为「需求 · 进行中」；不再协议报错。本停点可以先不写完 Spec/实现。

## 停点 2 — 确认后自动往下写文档

**改什么：** Runtime 在 `task.md` / `spec.md` / `plan.md`+`todo.md` 就绪后用 `ask_user` 做确认（确认 / 我要改；Task 可改简单/复杂）。确认写入 digest。同一 Turn 按 Spec 4/6 节自动灌下一阶段 Skill 并写文件。简单需求不弹 Spec 确认。`complexity` 三条规则由 Grill 写入 front matter。确认 Plan **不**要求 `verify_command`。

**为什么：** 没有门禁和自动衔接，停点 1 只是聊天，不是流程。

**怎么验证：** fake 确认序列：simple 确认 Task 后同一次 `execute_turn` 恢复写出 Spec 与 Plan+Todo，且无第二道 Spec 确认；complex 有 Spec 确认；「我要改」不写确认；改已确认 Task 正文后确认作废。

**可演示：** 答完 Grill、确认 Task（保持简单）后，无需打「继续」，对话里接着出现 Spec/Plan 草稿，再出现「确认计划」。进度：需求已确认，规格跳过，计划等你。

## 停点 3 — 确认计划后自动实现并检视

**改什么：** Plan 确认后同一 Turn 启动 fresh implement（只带已确认文档 + `todo.md`，TDD 原版 Skill，无 Grill 原文、无用户 Skill 索引）。实现返回后同一 Turn 启动 fresh review（内置 `code-review-and-quality`，只写 `review.md`），不再由 Runtime 另跑验收。`review.md` 就绪后 `ask_user` 确认结束或按意见退回实现。协议与 TUI 把第五格从「测试」改为「检视」。

**为什么：** 这是「确认后自动干活」的后半段：代码写完要有人（Skill）审这一次的改动，而不是再亮一格测试。

**怎么验证：** Plan 确认后必调 implement port；ContextPack 断言无 Grill 原文；内部验收 0 或无命令 → 调用 review port；验收非 0 → 不调用 review；确认 review → completed；「按意见改」→ 再 implement 且带 `review.md`；wire 上无索要 verify_command 的提问。

**可演示：** 确认计划后同一轮开始改文件。实现结束后进度走到「检视 · 进行中」，随后出现 `review.md` 确认。不出现「测试」格，也不问测试命令。

## 停点 4 — 命令、恢复、拆旧、文档

**改什么：** `/abandon` 确认后空闲，禁止带目标；`/new-work` 必须带目标。空闲/完成后的下一句开新 slug。`compose.abandon` 去掉 work_item_id。进程重启后 inspect 仍记住确认，不重 Grill。删除 `work_item_engine`、`turn_intent` 分类主路径、Activity 账本、旧投影与测试。改 `docs/user/交互使用.md`、`安全与沙箱.md`、`Compose 工作模式.md`、架构总览。

**为什么：** 停点 3 已能做完一件事；没有命令切分和拆旧，旧引擎还会把人带回去。

**怎么验证：** 路由与 abandon 单测；重启 fixture；`rg work_item_engine` 主路径无引用；`bun run project:check`。

**可演示：** 做到一半 `/abandon`，进度空闲，旧目录还在；`/new-work 写 HTTP 服务` 立刻新 Grill。确认 Task 后重启 `bun run dev`，不会重新访谈。

## 风险

| 风险 | 处理 |
| --- | --- |
| 主 Agent 换 Skill 后仍带着 Grill 长对话，128k 吃紧 | 实现与检视已隔离；Grill/Spec/Plan 先观察，不在本任务做每阶段压缩 |
| 原版 grill-me 问太久 | 已约定先看效果，不改 95% 仪式 |
| 检视把整个仓库当范围 | Runtime 只注入本套改过的文件；工具面禁止写产品代码 |
| 旧 compose 测试面大 | 按停点删/改，不为旧 Work Item 行为留兼容测试 |
| Interactive Core 仍认 work_item 帧或第五格「测试」 | 停点 3 必须同时改 reducer 与中文标签 |

## 不在本计划里

双轴并行 Reviewer、每个 Todo 单独派审、检视改产品代码、每阶段新 Thread、改原版 Skill 除 ask_user 外的正文、旧数据迁移、实现阶段 MCP 白名单（按需 reveal 已包含）。
