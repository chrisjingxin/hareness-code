# HC-145 实施计划：TUI 视觉与渲染重构

关联任务：[HC-145](../task/HC-145-TUI视觉与渲染重构.md)  
规格依据：[HC-145 Spec](../spec/HC-145-TUI视觉与渲染重构.md)  
架构依据：[TUI 表现层](../architecture/TUI表现层.md)  
执行清单：[HC-145 Todo](../todo/HC-145-TUI视觉与渲染重构.md)

## 文档职责

- Task 是用户结果、范围和整体验收事实源。
- Spec 是颜色、组件、interface、限额和 invariant 事实源。
- 本 Plan 只安排真实依赖与可验证竖切，不新增范围。
- 发现需要改 Protocol、授权语义、Web 外观或新产品面时，停止并先改 Task/Spec。

## 交付概览

```text
WP1 主题 token + 绘制限额纯函数
WP2 用户消息 Mode 字段 + UserMessage / AssistantText
WP3 Thinking 窗口与折叠
WP4 ToolRendererRegistry + Inline / Block / Diff / Generic
WP5 BottomArea：ApprovalDock / QuestionDock
WP6 页面收口：卸顶栏、SystemEvent / ErrorBlock / RunFooter、Overlay / Home
WP7 用户文档、架构检视、项目检查
```

每一步结束后 TUI 仍能启动；不允许「拆完文件但看不见任何新行为」的纯搬迁步单独存在。WP1 必须先让输入栏 / 首页用上新 token，这样后面组件有色可用。

## 依赖图

```text
WP1 token + boundVisibleText
  ├─→ WP2 用户消息 Mode 与正文组件
  │     └─→ WP3 Thinking
  │           └─→ WP4 工具分流
  └─→ WP5 Dock（可与 WP3/WP4 在 token 稳定后并行，但合入前要与 WP2 共用 BottomArea 壳）
WP2～WP5 → WP6 页面收口
WP6 → WP7 文档与检查
```

WP5 依赖 WP1 的 Mode 色，不依赖 WP4。若两人接力：一人 WP2→WP3→WP4，一人在 WP1 后做 WP5，最后在 WP6 接线。

## WP1：主题 token 与绘制限额

**说明：** 把 Spec 色表写入 `tuiTheme`，提供 `modeAccent(workMode)`。新增 `boundVisibleText`。输入栏与 Home 的 Mode 标签、焦点改走 Mode 色；Logo 保持品牌色。删除把品牌蓝当「当前 Mode」的用法。把 TUI `Composer` 重命名为 `InputBar`（文件 `input-bar.tsx`），不留旧名。

**验收条件：**

- [ ] 测试字面量锁定 Build `#EAB308`、Compose `#A9A5D4` 及 Semantic 表。
- [ ] `boundVisibleText` 对 tail/head、行数、字数、hiddenLines 有独立于实现的例子。
- [ ] Home/Thread 输入栏在两种 Mode 下使用对应 accent；Logo 色不变。

**验证：** `cd packages/cli && bun test tests/tui/presentation/theme.test.ts tests/presentation-shared tests/tui/presentation/views.test.ts`

**可能改动：** `presentation/theme.ts`、`presentation/composer.tsx` → `input-bar.tsx`、`presentation/home.tsx`、`presentation-shared/tool-output-policy.ts` 或新纯函数文件、对应测试。

**规模：** M

## WP2：用户消息记住 Run Mode

**说明：** `ConversationMessage` 增加可选 `workMode`。提交时写入当前 Mode；`run.started` 校正；恢复时用 `threadMode` 或 `build`。TUI `UserMessage` 改为短 `▌`，`AssistantText` 保持无框 Markdown。构造「先 Build 再切 Compose」证明旧条不改色。

**验收条件：**

- [ ] 缺字段的旧 fixture 仍能进 reducer。
- [ ] 切换会话 `workMode` 不修改已有用户消息。
- [ ] 用户条不再是完整左轨卡片。

**验证：** `cd packages/cli && bun test tests/interactive/state.test.ts tests/interactive/timeline-feature.test.ts tests/tui/presentation/views.test.ts`

**可能改动：** `interactive/state.ts`、thread restore、`presentation/timeline*`、对应测试。Web 文件不改外观。

**规模：** M

## WP3：Thinking 有界展开

**说明：** 进行中强制展开且只画最后 12 行；完成后折一行，点开最多 40 行。结束继续靠现有语义事件，不掐秒。超长 fixture 断言不会把全文塞进渲染内容。

**验收条件：**

- [ ] 进行中不可折叠；完成后默认折叠。
- [ ] >40 行思考的可见行数符合 Spec 表，并出现「还有 N 行」。
- [ ] 标题/spinner 用该 Run 的 Mode 色。

**验证：** `cd packages/cli && bun test tests/tui/presentation/views.test.ts`（及本步新增的 thinking 测试）

**依赖：** WP1、WP2  
**规模：** M

## WP4：工具分流

**说明：** 实现 `resolveToolRenderer` 与四个组件。删掉「所有工具一张 ToolRow」的终态路径。`task` 与未知名为 generic。Diff 复用已有解析与 `DiffRenderable`。

**验收条件：**

- [ ] 名称表与未知回退有表驱动测试。
- [ ] 读类为一行，execute 为块，edit/write/delete 为 Diff。
- [ ] Block/Generic 默认可见行 ≤ 12。

**验证：** `cd packages/cli && bun test tests/tui/presentation`  
**依赖：** WP1  
**规模：** L（若超过 5 个文件，先落地 registry + inline，再补 block/diff，仍算本 WP，不要另开范围）

## WP5：底部 Dock

**说明：** Thread 底部按 Spec 互斥渲染 InputBar / ApprovalDock / QuestionDock。时间线去掉可聚焦 Interaction 控件。完成后只留结果行。键盘与现有决策集合不变。

**验收条件：**

- [ ] pending 审批时输入框不聚焦，Dock 可见。
- [ ] 时间线帧中没有审批 `<select>`。
- [ ] 提交决定后 Dock 消失、输入栏恢复、结果行存在。
- [ ] 问答多选 / other 与现有 Intent 一致。

**验证：** `cd packages/cli && bun test tests/tui/app-interaction.test.ts tests/tui/presentation/views.test.ts`  
**依赖：** WP1  
**规模：** M

## WP6：页面收口

**说明：** `ThreadView` 卸载 `WorkItemView`。补 SystemEvent（含 `skill.loaded`）、ErrorBlock（仅 Run/LLM 级）、RunFooter。Overlay 壳换 token。确认 Compose 与 Build 走同一对话页。

**验收条件：**

- [ ] 对话页不再出现阶段顶栏。
- [ ] 工具失败不走进 ErrorBlock。
- [ ] Overlay 选中色为当前 Mode，结构仍是现有 Picker。

**验证：** `cd packages/cli && bun test tests/tui/presentation/work-item-view.test.tsx tests/tui/presentation/overlays.test.ts tests/tui/presentation/views.test.ts`  
**依赖：** WP2～WP5  
**规模：** M

## WP7：文档与检查

**说明：** 更新 `docs/user/交互使用.md`（审批在底部、思考窗口、工具三种画法、Mode 色）。检视并按落地结果增量改 [TUI 表现层](../architecture/TUI表现层.md) 与 [架构总览](../architecture/架构总览.md)。跑项目检查。不改 `VERSION`。

**验收条件：**

- [ ] 用户文档与真实 TUI 行为一致，不再写「审批卡留在时间线中间」。
- [ ] `bun run typecheck`、TUI focused + 必要回归、`bun run project:check` 通过。

**验证：** 见 Todo WP7  
**依赖：** WP6  
**规模：** S

## 风险

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| 超长思考即使窗口化仍因每个 delta 更新 snapshot 而卡 | 高 | 先保证不把全文交给 `<text>`；若帧测仍卡，只在 Adapter 对思考做 ≤100ms 节流，不改 Core |
| OpenTUI Diff 与新 surface 色对比不足 | 中 | 沿用 HC-144 语义底色，只换外围 chrome |
| Core 加 `workMode` 弄破 Web 测试 | 中 | 字段可选；Web 测试失败只修类型，不改 Web JSX/CSS |
| 卸顶栏后 Compose 用户失去阶段感 | 低 | 已确认本次不做；记在新功能候选 |

## 不在本计划决定的事

无。grill-me 已关闭范围。实现中不得把 Sidebar 或阶段顶栏「顺便做回来」。
