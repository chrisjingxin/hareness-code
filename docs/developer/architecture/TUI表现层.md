# TUI 表现层

本文是 TUI 视觉与渲染的长期架构入口。行为细节以对应 Task / Spec 为准；结论直接写在这里，不再另建 ADR。

当前实施任务：[HC-145](../task/HC-145-TUI视觉与渲染重构.md)（WP1–WP6 已落地，本文按落地路径维护）。

## 在整仓中的位置

```text
InteractiveController（共享领域状态）
        │
        ├─ TuiAdapter     本地：draft / picker / 展开 / 焦点 / 滚动
        │       │
        │       └─ TUI Presentation（本文）
        │
        └─ Web Adapter / Web Presentation   HC-145 不改
```

TUI 只消费 snapshot 并派发 Intent。像素、色值、绘制窗口、Dock 焦点都属于 TUI。Web 是另一套原生 Renderer，互不共享组件树。

底部打字面叫 **输入栏（`InputBar`）**，不要叫 Composer。Composer 和 Compose 模式只差几个字母，文档和代码里都容易读错。Compose 只表示工作模式。

## 两套颜色，互不污染

- **Mode 色**：Build `#EAB308`，Compose `#A9A5D4`。只说明「这次执行是谁」。用户消息竖条、输入栏、Dock/Overlay 选中可用它。思考标题用独立的青灰色，不跟 Mode。
- **Semantic 色**：成功、失败、警告、Diff 红绿。永远不跟 Mode 变。

历史用户消息记住创建时的 Run Mode。切换当前 Mode 不得回写旧消息。恢复 Thread 时若消息没有逐条 Mode，退回 `threadMode` 或 `build`。按条持久化 Mode 需要 Protocol / Transcript，单独立项。

Logo 是品牌色，不是 Mode 皮。

## 时间线按类型画，工具按注册表分流

时间线是记录，不是万能卡片槽。

```text
item.type
  message.user        → UserMessage
  message.assistant   → AssistantText
  reasoning           → Thinking
  tool                → resolveToolRenderer(name)
  interaction(终态)   → 一行结果
  系统事实            → SystemEvent
  run / LLM 失败      → ErrorBlock
  run 终态            → RunFooter
```

`resolveToolRenderer` 是 TUI 私有、深的小 interface：调用方只给工具名，得到 `inline | block | diff | generic`。未知名必须是 generic。

读/搜默认一行；命令有输出块；文件变更走 Diff。`task` 在本阶段按 generic 画，不提供进入子对话。

## 底部只有一个输入面

```text
无 pending Interaction → 输入栏（InputBar）
审批 pending           → ApprovalDock
问答 pending           → QuestionDock
```

三者互斥。操作发生在底部，时间线只留事后结果。这是现有 Interaction 的搬家，不是新的 Host 能力。

## 有界绘制

终端重绘整段无上限文本会卡死。思考与长输出只画窗口：

- 思考进行中：最后 12 行
- 思考点开：最多 40 行
- Block / Generic 默认：最多 12 行

窗口化是 Presentation 职责，不截 Core 里的原文。限额函数是纯的，可放在 `presentation-shared`，但 Web 是否采用另开任务。

## 与 Compose 的关系

Build 与 Compose 共用这一套组件。差别是 Mode 身份和背后的执行，不是两套 Presentation。

对话页不再挂 Compose 阶段顶栏。顶栏、Sidebar、子对话钻取、Skill 专用卡、Plugin 专用 Renderer 记在 [新功能候选](../project/新功能候选.md)，以后各自拆 Task。

## 落地路径

| 职责 | 位置 |
| --- | --- |
| Mode / Semantic token | `packages/cli/src/tui/presentation/theme.ts`（`modeAccent`、`thinking`） |
| 绘制限额 | `packages/cli/src/presentation-shared/paint-budget.ts`（`boundVisibleText`） |
| 输入栏 | `packages/cli/src/tui/presentation/input-bar.tsx`（无 `Composer` 标识符） |
| 底部 Dock | `packages/cli/src/tui/presentation/bottom-area.tsx` |
| 工具分流 | `packages/cli/src/tui/presentation/tools/registry.ts` 与 `renderers.tsx` |
| 对话页 | `packages/cli/src/tui/presentation/thread.tsx`：时间线 + BottomArea + 底栏；不挂 `WorkItemView` |

`work-item-view.tsx` 源文件保留作后续任务原料，对话页不得再挂载。Overlay / Picker 仍是现有 `SearchPicker` / `DialogShell`，选中色跟当前 Mode。

## 目录约定

主题 token 仍只有一处事实源。工具 Renderer 已拆到 `presentation/tools/`；时间线其余类型组件仍可按职责继续下沉，不必为拆文件而拆。
