# HC-145：TUI 视觉与渲染重构

> 原始需求：[HC-145](../task/HC-145-TUI视觉与渲染重构.md)

## 通俗问题说明

现在终端里几乎所有东西都长一样：用户说话、模型思考、读文件、跑测试、要你点允许，全是左边一条线加一块底色。Build 和 Compose 是两种对等的干活方式，界面却看不出这次到底是哪一种。审批卡夹在聊天记录中间，记录一长就找不到。思考文字没有上限，一长终端会冻住。

这次只改 TUI 怎么画。Agent 还是那个 Agent，Web 还是现在的 Web。改完之后：

```text
用户说话     → 短竖条，颜色记住那一次的 Mode
模型回答     → 干净正文，不染色
正在思考     → 必须看见最新几行，但不能把屏幕画爆
读/搜        → 一行
跑命令       → 一块输出，默认有上限
改文件       → Diff
要你决定     → 底部操作栏换掉输入框
一轮结束     → 一行摘要
```

## 用语

| 说的是 | 用这个词 | 不要用 |
| --- | --- | --- |
| 底部打字的地方 | 输入栏 / `InputBar` | Composer（和 Compose 模式撞名） |
| 工作模式之一 | Compose 模式 / `workMode: "compose"` | 把输入栏叫 compose |
| 临时换掉输入栏的审批/问答 | Dock / `ApprovalDock` / `QuestionDock` | 审批 Composer |

用户文档写「输入栏」或「输入框」。Web 侧现有 Composer 名称本任务不改。

## 已确认现状

- TUI 遵守 ADR 0003：唯一 `InteractiveController`，TUI Adapter 只持有 draft / picker / 展开 / 滚动，Presentation 只消费 snapshot。
- `packages/cli/src/tui/presentation/timeline.tsx` 用同一套左轨卡片画 message / reasoning / tool / interaction。
- 审批和问答画在时间线里；`ThreadView` 在阻塞 Interaction 时藏起输入栏，底部没有承接层。代码里这个组件目前叫 `Composer`，与 Compose 模式撞名；本任务改名为 `InputBar`。
- 主题主强调是品牌蓝 `#3b82f6`，选择态是暖黄。Build / Compose 只在文案上区分。
- `ConversationMessage` 没有 Mode 字段。会话级 `workMode` 与 Thread 冻结后的 `threadMode` 已存在。`run.started` 已带 `mode`，但恢复历史的 `threads.open` 消息没有逐条 Mode。
- 思考是 `ReasoningCard.text` 无上限追加；进行中把全文交给 OpenTUI `<text>`，每个 delta 整段重绘。
- 工具输出折叠策略在 `presentation-shared/tool-output-policy.ts`；思考没有对应限额。
- Compose 对话页顶部挂 `WorkItemView` 阶段条，与「两套 Mode 共用一套对话界面」冲突。
- `apply_patch` 已下线。Skill 加载是 `skill.loaded` 事件，不是一种 Tool。`task` 是普通工具调用。

## 目标与非目标

### 目标

- TUI 使用一套 Mode / Semantic 色板。Build 与 Compose 等权，Mode 色只表达「这次执行属于谁」。
- 时间线按消息类型选组件，工具按名字分流，未知工具永远有 Generic 回退。
- 审批和问答占用底部 Dock，与输入栏互斥；时间线只保留事后结果。
- 思考和长工具输出有明确绘制窗口，超长不得卡死终端。
- Compose 与 Build 共用同一套对话页；本任务卸掉阶段顶栏。

### 非目标

- 不改 Web 外观、Web Adapter、Handoff、Gateway。
- 不做 Sidebar、子对话钻取、Plugin 专用 Renderer、独立 JSON 卡、独立 Skill 卡。
- 不重做 Compose 阶段顶栏（记入新功能候选）。
- 不改 Protocol / Transcript / SQLite 来为恢复历史按条存 Mode。
- 不改审批授权、文件 mutation、Agent 执行或 JSON-RPC 方法名。
- 不为旧卡片或旧色值保留 alias。

## 目标流程

```text
InteractiveSnapshot + TuiAdapter 本地状态
        │
        ▼
SessionPage / HomeView
        │
        ├─ Timeline
        │     ├─ 按 item.type 选组件
        │     └─ tool → ToolRendererRegistry → Inline / Block / Diff / Generic
        │
        └─ BottomArea（三者只居其一）
              ├─ 无 pending Interaction → InputBar
              ├─ approval pending      → ApprovalDock
              └─ question pending      → QuestionDock
```

思考结束：

```text
reasoning.active = true 且强制展开（只画最后 12 行）
        │
        │ 同 scope 出现 assistant 正文 / tool.started / run 终态
        ▼
reasoning.active = false，折成一行
        │
        └─ 点击 → 最多再画约 40 行
```

不使用「两秒没 token」判断结束。现有 reducer 已在正文或工具到达时冻结思考，本任务保持该语义。

## 颜色

| Token | 色值 | 用途 |
| --- | ---: | --- |
| `mode.build` | `#EAB308` | Build 身份（沿用原先更亮的暖黄） |
| `mode.compose` | `#A9A5D4` | Compose 身份（与 Build 等权提亮，避免闷紫） |
| `thinking` | `#7EB6C9` | 思考标题/spinner，刻意避开 Build 金 |
| `bg` | `#0B0C0E` | 主背景 |
| `surface` | `#15171A` | 输入栏、BlockTool |
| `surfaceElevated` | `#1B1D21` | Dock、Overlay |
| `border` | `#2A2D33` | 普通分隔 |
| `textPrimary` | `#E8E9EC` | 主文本 |
| `textSecondary` | `#A0A4AE` | 次级 |
| `textMuted` | `#676C76` | 时长、辅助 |
| `success` | `#7FA37A` | 成功 |
| `error` | `#C56F6F` | 失败 |
| `warning` | `#C88758` | 风险 |
| `diffAdd` | `#6F9A72` | Diff 新增 |
| `diffRemove` | `#B96A6A` | Diff 删除 |

规则：

1. Mode 色只用于：该条用户消息的短竖条、输入栏 Mode 标签与焦点、Dock 选中项、当前 Overlay 选中项。思考标题用独立的 `thinking` 色，不用 Mode 金/紫。
2. 工具名、参数、成功/失败、Diff 红绿永远走 Semantic，不跟 Mode 变。
3. Logo 固定蓝白品牌色，不跟 Mode 变。星空不以 Mode 换肤。
4. 用户消息的 Mode 来自**创建它的那次 Run**，不是当前会话的 `workMode`。

语法高亮 scope 继续走离线 Tree-sitter，色值改到与上表协调，不新增依赖。

## 公开 interface

### Interactive Core（仅加性字段）

`ConversationMessage` 增加：

```ts
workMode?: "build" | "compose"
```

- 用户提交时写入当时的 `state.workMode`。
- `run.started` 若带 `mode`，以事件为准校正该 Run 下尚无值的用户消息。
- 恢复 Thread：消息本身没有 `workMode` 时，用 `threadMode`；`threadMode` 为空则用 `build`。
- 缺字段必须可渲染，不能把旧测试数据变成运行时错误。
- Web 继续忽略该字段，本任务不改 Web 组件。

思考标题固定用 `tuiTheme.thinking`，不跟 Run Mode 变色。

不在 Core 截断 `ReasoningCard.text`。绘制限额只发生在 TUI。

### TuiAdapter 本地状态

在现有 draft / picker / `expandedTools` 之外，Adapter 拥有：

- BottomArea 焦点：Dock 出现时输入栏失焦，Dock 关闭后焦点回到输入栏。
- 已完成思考的展开集合。
- 可选：思考从首次 active 到冻结的本地耗时，只用于折叠标题，不回写 Core。

不把像素、色值、行数窗口放进 Core。

### 绘制限额

在 `presentation-shared` 增加纯函数（具体文件名可并入现有 `tool-output-policy.ts`，也可新建；对调用方只暴露下面这个 interface）：

```ts
boundVisibleText(
  text: string,
  options: { maxLines: number; maxChars: number; keep: "head" | "tail" },
): { text: string; overflow: boolean; hiddenLines: number }
```

| 场景 | keep | maxLines | maxChars |
| --- | --- | ---: | ---: |
| 思考进行中 | tail | 12 | 4096 |
| 思考点开 | head | 40 | 8192 |
| BlockTool / Generic 默认预览 | head | 12 | 4096 |

超出时组件必须显示「还有 N 行」，不得把未窗口化的全文交给 OpenTUI。展开后仍受上表硬顶，不提供「在 TUI 里看完全文」。

### ToolRendererRegistry

TUI Presentation 私有 module。对 Timeline 只暴露：

```ts
resolveToolRenderer(name: string): "inline" | "block" | "diff" | "generic"
```

| 工具名（大小写不敏感，忽略末尾非字母） | Renderer |
| --- | --- |
| `read` / `read_file` / `grep` / `glob` / `ls` / `list` / `webfetch` / `web_fetch` / `websearch` / `web_search` / `codesearch` / `view_image` | inline |
| `execute` / `bash` / `exec` / `shell` | block |
| `write_file` / `write` / `edit_file` / `edit` / `delete_file` / `delete` | diff |
| 其他，含 `task` | generic |

- 没有映射时必须是 `generic`，禁止「无法渲染」。
- Diff 预览优先用已有 `file_diff` / 输出里的 unified diff。`write_file` 没有 diff 时，从参数抽出 `file_path` 与 `content`，按路径做语法高亮展示正文。`edit_file` 用 `old_string`/`new_string` 生成 unified diff 做红绿高亮，禁止把参数或结果 JSON 铺开。默认 12 行，点击展开最多 100 行；再长仍提示还有 N 行。正文坐在 `surface` 底面上。解析失败再退回 Generic。
- 审批里的文件 Diff 仍走已有 OpenTUI `DiffRenderable` 与 120 列 split / 否则 unified，语义不变。

### BottomArea

`ThreadView` 底部同一时刻只渲染一个：

| 条件 | 渲染 |
| --- | --- |
| `interaction.type === "approval"` 且 pending | `ApprovalDock` |
| `interaction.type === "question"` 且 pending | `QuestionDock` |
| 其他 | `InputBar` |

时间线不得再挂可聚焦的审批/问答控件。pending 项可从时间线隐藏，或留一行不可操作的 muted 提示。决定完成后时间线只保留结果行，例如「已允许一次」/「已拒绝」/「已回答：…」，不保存整个 Dock。

键盘（Dock 内）：

- `↑` `↓` / `j` `k` 移动
- `Enter` 确认
- `Esc` 拒绝审批或取消问答（与现有 Interaction 取消语义一致，不发明新协议）
- 多选问答：`Space` 切换；`allow_other` 时保留现有「其他」输入

授权选项顺序与文案继续走 `presentation-shared/interaction-policy.ts`，本任务不改决策集合。

## 组件行为

本任务用户可见组件如下。Sidebar、SkillTool、StructuredOutput、Task 钻取不做。

### 时间线

**UserMessage**  
短 `▌` + 正文。`▌` 用该消息 `workMode`。正文 `textPrimary`。不要完整卡片，不要通栏长竖线。

**AssistantText**  
无框、无底、无 Mode 条。Markdown / 代码块 / 表 / 列表与现有离线语法一致。流式追加。空且未流式的 assistant 不渲染。

**Thinking**  
进行中：spinner + 标题 `Thinking`，颜色用独立 `thinking`（`#7EB6C9`），不用 Mode 金/紫。正文 `textSecondary`，用户不能折叠。只画最后 12 行。  
完成后：`+` 与 `Thinking` 分列，中间留空；折叠摘要和展开正文都与 `Thinking` 左对齐，不和 `+` 齐平。点击展开，最多 40 行。再次点击折回。

**InlineTool**  
一行：`icon/name + 摘要 + duration`。进行中 spinner 可用 Mode 色，其余中性。默认不展开。

**BlockTool**  
弱 surface，标题 + 输出。默认最多 12 行，可展开但仍受硬顶。成功/失败用 Semantic。

**DiffTool**  
文件名 + 增删统计 + Diff。颜色固定红绿。宽屏 split、窄屏 unified，阈值沿用现有 120 列。

**GenericTool**  
未知或 `task` 等：名称 + 有界 input/output。永远可画。

**SystemEvent**  
压缩、中断、切 Mode、恢复 Thread、`skill.loaded` 等。一行 `textMuted`。切 Mode 时只有 Mode 名称用对应 Mode 色。

**ErrorBlock**  
仅 Run / LLM 级失败（请求失败、超时、连接断开、模型不可用）。工具失败留在对应 Tool 组件。可展示简短 Retry 入口若现有 intent 已支持；不新增协议。

**RunFooter**  
一轮结束后一行 muted：模型 · 耗时 · 工具数/用量。无边框。

完成后的审批/问答结果按 SystemEvent 或同等弱结果行处理，不再是可操作卡。

### 交互区

**InputBar（输入栏）**  
用户打字、发消息的底部输入面。不要叫 Composer，以免和 Compose 模式混淆。用户文档写「输入栏」或「输入框」。Build / Compose 结构相同，只换 label 与 accent。`Tab` 空闲切 Mode 的现有行为不变。Home 与 Thread 共用组件，Thread 不再画整段左框卡片。实现时把 `composer.tsx` 重命名为 `input-bar.tsx`，删除 `Composer` 这个标识符，不留 alias。Web 的 Composer 名称本任务不动。

**ApprovalDock / QuestionDock**  
见 BottomArea。选中/焦点用**当前这次 Run** 的 Mode。Reject / 危险项用 warning 或 error。Diff 用 Semantic。

**Overlay**  
Model / Thread / Command / Skill / 确认框共用现有 `SearchPicker` / `DialogShell` 壳，只换 token 与选中色。不写五套 UI。

### 页面

**HomeView**  
保留 Logo 与星空。变化只发生在输入栏的 Mode 身份。

**SessionPage**  
现有 `ThreadView` 的布局名：时间线 + BottomArea + 底栏。不引入 Sidebar。卸掉 `WorkItemView` 挂载。`work-item-view.tsx` 源文件可留作后续任务原料，本任务不得再把它画进对话页。

## 关键 invariant

1. Presentation 不得直接调用 AgentClient / JSON-RPC method string。
2. 成功/失败/Diff 色不得读取 `workMode`。
3. 切换当前 `workMode` 不得改写已有用户消息的 `workMode`。
4. BottomArea 同时最多一个可聚焦输入面。
5. TUI 不得把超限思考或工具全文交给 OpenTUI 文本节点。
6. 未知工具必须落到 Generic，禁止空白或「Unsupported」。
7. Web 源码本任务零外观改动；Core 加字段必须保持缺省可解析。
8. 审批决定仍只回写已有 Intent，展示失败不能吞掉 Allow / Reject。

## 错误与降级

- 缺 `workMode`：用户条用 `threadMode` 或 `build`，不报错。
- Diff 解析失败：该工具按 Generic 文本画，审批仍可用通用选项。
- 思考文本为空：进行中只显示标题 + spinner；完成后若无正文则整项可省略或只留标题。
- 主题 token 缺失：禁止静默回退到旧蓝主强调。测试锁定上表色值。
- 渲染抛错：继续走现有 `TuiErrorBoundary`，不回显原始异常。

## 模块与 seam（codebase-design）

| Module | Interface（调用方需要知道的） | 深度放在哪 |
| --- | --- | --- |
| `tuiTheme` | token 名与 `modeAccent(mode)` | 全部色值与语法 scope |
| `boundVisibleText` | 一行函数 + 上表限额 | 折行、Unicode、overflow 计数 |
| `resolveToolRenderer` | 工具名 → 四种 kind | 别名表、大小写、未知回退 |
| `BottomArea` | snapshot → 唯一槽位 | 焦点、Dock 与输入栏替换 |
| Timeline 子组件 | 各收自己的 view model | 样式与有界绘制 |

测试缝：theme token、`boundVisibleText`、`resolveToolRenderer`、BottomArea 槽位选择、思考窗口、用户消息 Mode 不回写。不要测 OpenTUI 内部 layout。

Timeline 从单文件拆到 `presentation/timeline/`（或同等目录），禁止继续把所有类型堆在一个 500+ 行文件里作为终态。

## 可观察验收

- 截取或测试帧中：Build 用户条 / 输入栏为 `#EAB308`，Compose 为 `#A9A5D4`。
- 构造「先 Build 用户消息，再把会话 `workMode` 切到 Compose」：旧消息仍是金色。
- 超长思考（> 40 行）进行中只出现 ≤ 12 行正文；点开 ≤ 40 行且有「还有 N 行」。
- pending 审批时输入栏不聚焦、Dock 可见；时间线没有 `<select>` 审批控件。
- `task`、未知工具名走到 Generic；`read_file` 为一行；`execute` 为 Block；`edit_file` 为 Diff。
- Thread 页查询不到 Compose 阶段顶栏文案（理解/规划/实现…那条专用顶栏）。
- Web presentation 测试不因本任务修改外观期望。

## 测试策略

- 框架：`cd packages/cli && bun test`，优先 `tests/tui/presentation/`、`tests/tui/application/`、`tests/interactive/state.test.ts`（仅 `workMode` 字段）。
- 色值与窗口用字面量断言，不要「渲染后再读实现函数算一遍」。
- 禁止真实模型凭据。OpenTUI `testRender` 用现有 80×24 / 130×40 帧做 Dock 与 Mode 色抽检。
- Web 测试只跑回归，确认无本任务 diff；不要为 TUI 色板去改 Web 断言。

## 命令

```bash
cd packages/cli && bun test tests/tui tests/interactive/state.test.ts tests/presentation-shared
bun run typecheck
bun run build
bun run project:check
```
