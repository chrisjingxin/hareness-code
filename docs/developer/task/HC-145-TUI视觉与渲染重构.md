---
id: HC-145
title: TUI视觉与渲染重构
feature_area: TUI 表现层
parent_task: -
decomposed_by: Grok
priority: P1
status: 进行中
owner: Grok
branch: feat/hc-145-tui-visual
reviewed_at: 2026-08-13
review_due: 2026-08-27
scope: 只改 TUI 视觉系统与渲染架构。对话记录按类型拆成独立组件，工具按名字分流到 Inline/Block/Diff/Generic，审批和问答改到底部 Dock，思考与长输出做有界绘制。Web UI、Protocol、Agent、Sidebar 和新产品面不在范围。
acceptance: 用户在 TUI 中能区分 Build/Compose 的 Mode 身份且互不等权染色整页；工具不再共用一张卡；审批/提问时底部输入框换成 Dock，对话区只留事后结果；思考进行中强制展开但只画最后 12 行，点开最多约 40 行，长内容不再把终端画死；Compose 不再单独占一块阶段顶栏；Web 外观与现有工作流不变；TUI focused 测试、typecheck、build 通过。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-145-TUI视觉与渲染重构.md、docs/developer/plan/HC-145-TUI视觉与渲染重构.md、docs/developer/todo/HC-145-TUI视觉与渲染重构.md、docs/developer/architecture/TUI表现层.md
test_evidence: 2026-08-13 WP7：bun run typecheck exit 0；cd packages/cli && bun test tests/tui → 140 pass；bun run project:check exit 0；git diff --check 无空白错误；无版本号变更
references: docs/developer/architecture/adr/0003-single-interactive-core-dual-renderer.md、docs/developer/task/archive/HC-118-TUIWeb思考摘要与运行进度.md、docs/developer/task/archive/HC-140-重构组合工作模式.md、docs/developer/project/新功能候选.md
completed_at: -
---

# HC-145 TUI 视觉与渲染重构

## 背景

当前 TUI 把用户消息、思考、工具、审批、问答都画成同一种「左轨 + 底色卡片」。Build / Compose 虽然是对等的执行模式，界面却仍用品牌蓝当主强调，看不出这次 Run 属于谁。工具不论是读一个文件还是跑一整段测试，都挤在同一张卡里。审批和问答夹在对话中间，对话一长就会滚出屏幕。思考正文没有绘制上限，内容一多终端会卡死。

这些问题出在 TUI 表现层，不是 Interactive Core 或协议坏了。本任务换一套用户看得见的画法，并拆开渲染架构，让以后加一种工具不必再改整张时间线。

需求已用 `mattpocock:grill-me` 确认，结论见下文与 [规格](../spec/HC-145-TUI视觉与渲染重构.md)。

## 用户最终得到什么

打开 TUI 后：

- 首页 Logo 仍是蓝白品牌色。输入框上的 Build / Compose 用金 / 紫标记当前模式，两者一样重。
- 发出去的用户消息是短 `▌` + 正文，颜色记住**那一次 Run** 的 Mode；切 Mode 不会把旧消息重新染色。
- Agent 回答仍是无框 Markdown，不跟 Mode 变色。
- 思考时标题带 Mode 色且必须展开，但屏幕只显示最后 12 行；想完收成一行，点开最多约 40 行。
- 读文件 / 搜索是一行；跑命令有输出区；改文件走 Diff。不认识的工具也能画出来。
- 需要审批或回答时，底部输入框换成操作栏。上面的对话只留下事后一行结果。
- Compose 和 Build 是同一套对话界面，不再单独占一块阶段顶栏。

## 范围

- `packages/cli/src/tui/presentation/` 的主题、时间线组件、首页、对话页、选择浮层。
- `packages/cli/src/tui/application/adapter.ts` 的本地焦点、展开、有界绘制所需状态。
- Interactive Core 仅允许补领域事实：用户消息带上该次 Run 的 `workMode`。不改 Web 外观。
- 纯绘制限额函数可放进 `presentation-shared`，供 TUI 调用；Web 本任务不消费、不改样式。
- 用户文档 `docs/user/交互使用.md`，以及 [TUI 表现层](../architecture/TUI表现层.md)。

## 非范围

- Web UI 任何外观或交互改动。
- Sidebar、点进 Subagent 子对话、Plugin 专用 Renderer、独立 JSON 结果卡、独立 Skill 卡。
- 重做 Compose 阶段顶栏（以后要做，已记入 [新功能候选](../project/新功能候选.md)）。
- 为恢复历史按条补 Mode 而改 Protocol / Transcript / SQLite。恢复时用已有 `threadMode`。
- 修改审批授权语义、文件 Snapshot、CAS、Agent 执行或 JSON-RPC 方法。
- 为旧 TUI 卡片、旧色值或旧审批位置保留兼容分支。

## 可观察验收

- Build 用户条与输入栏为 `#EAB308`，Compose 为 `#A9A5D4`；成功 / 失败 / Diff 红绿不随 Mode 变。输入栏组件名是 `InputBar`，不要再用 Composer，以免和 Compose 模式混淆。
- 同一 Thread 先 Build 再切 Compose 后，旧的用户条仍是金色。
- 审批出现时底部是 Dock 不是输入框；对话区没有可点的审批选择器；决定后 Dock 消失，时间线只留结果行。
- 超长思考流式到达时 TUI 仍可滚动、可取消，不会因全文重绘卡死。
- Thread 页不再渲染 Compose 阶段顶栏；`task` 工具走 GenericTool。
- `packages/cli` 的 TUI presentation / adapter focused 测试、`bun run typecheck`、`bun run build` 通过；Web 既有测试不因本任务而改外观断言。

## 文档与实现

- 行为与 interface：[规格](../spec/HC-145-TUI视觉与渲染重构.md)
- 步骤：[计划](../plan/HC-145-TUI视觉与渲染重构.md)
- 勾选清单：[Todo](../todo/HC-145-TUI视觉与渲染重构.md)
- 实现使用 `agent-skills:test-driven-development`；Review 使用 `agent-skills:code-review-and-quality`。
- 当前未发版，不改 `VERSION`。
