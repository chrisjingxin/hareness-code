# HC-145 TUI 视觉与渲染重构执行 Todo

- 关联任务：[HC-145](../task/HC-145-TUI视觉与渲染重构.md)
- 行为规格：[HC-145 Spec](../spec/HC-145-TUI视觉与渲染重构.md)
- 实施计划：[HC-145 Plan](../plan/HC-145-TUI视觉与渲染重构.md)
- 架构：[TUI 表现层](../architecture/TUI表现层.md)

## 使用规则

- 按 WP1→WP7 的依赖执行；Plan 写明可并行的才并行。
- 每个工作包走 TDD：先提交能证明目标行为缺失的失败测试，再写最小实现。
- 不得改 Web 外观、Protocol、授权语义；不得把 Sidebar / 阶段顶栏 / 子对话做进来。
- 若必须改公开 interface 或把 Core 改动扩大到 `workMode` 以外，停止并先改 Task/Spec/Plan。
- 勾选前必须同时满足：代码、focused test、完成信号。

## 开工前

- [x] 运行 `git status --short`，保留用户已有改动。
- [x] 读完 HC-145 Task / Spec / Plan / Todo 与 `packages/cli/src/tui/presentation/` 邻近测试。
- [x] `bun run task:claim -- HC-145 --owner Grok --branch feat/hc-145-tui-visual`。

## WP1：主题 token 与绘制限额

- [x] 先写失败测试：断言 `tuiTheme` / `modeAccent("build"|"compose")` 等于 Spec 色表；当前蓝主强调测试应不再代表 Mode。
- [x] 先写失败测试：`boundVisibleText` 用固定长文例子覆盖 tail 12 行、head 40 行、maxChars、`hiddenLines`。
- [x] 实现 token 与纯函数；输入栏 / Home 的 Mode 标签和焦点改用 `modeAccent`；Logo 不改品牌色。将 TUI `Composer` 重命名为 `InputBar`，文件改为 `input-bar.tsx`，删除旧名。
- [x] 运行 focused tests（见下）。

**完成信号：** 首页在 Build 下 Mode 文案为金色、Compose 下为紫色；`boundVisibleText` 例子全绿。

证据（2026-08-13）：`cd packages/cli && bun test tests/tui/presentation/theme.test.ts tests/presentation-shared/paint-budget.test.ts tests/presentation-shared/architecture.test.ts tests/tui/presentation/views.test.ts tests/tui/presentation/harness-logo.test.ts tests/tui/presentation/overlays.test.ts tests/tui/app-interaction.test.ts tests/tui/architecture.test.ts` → 相关文件全部通过（views 19、theme 3、paint-budget 5、architecture/shared 2、logo/overlays/app-interaction/tui architecture 合计 25）。TUI `composer.tsx` 已删除，无 `Composer` 标识符；Web Composer 未改。

## WP2：用户消息 Mode

- [x] 先写失败测试：用户消息在 submit / `run.started` 后带有 `workMode`；之后 `setWorkMode` 不改已有消息。
- [x] 先写失败测试：恢复 Thread 时无字段消息得到 `threadMode` 或 `build`；缺字段不抛错。
- [x] 实现字段写入与 TUI `UserMessage` 短 `▌`、`AssistantText` 无框。
- [x] 运行 focused tests（见证据）。

**完成信号：** 「Build 消息 + 切到 Compose」的帧或属性断言里，旧 `▌` 仍是 `#EAB308`。

证据（2026-08-13）：`cd packages/cli && bun test tests/interactive/state.test.ts tests/interactive/timeline-feature.test.ts tests/interactive/thread-feature.test.ts tests/tui/presentation/theme.test.ts tests/tui/presentation/views.test.ts` 全部通过。用户条为短 `▌`；切到 Compose 后 `userMessageAccent` 仍为 `#EAB308`。

## WP3：Thinking

- [x] 先写失败测试：active 思考可见文本是原文 *tail* 最多 12 行；用户折叠意图被忽略。
- [x] 先写失败测试：冻结后默认一行；展开后最多 40 行且 `hiddenLines > 0` 时有「还有 N 行」。
- [x] 接上 `boundVisibleText` 与 Mode 色标题；本地可选记录耗时。
- [x] 运行 focused tests（见证据）。

**完成信号：** 80 行思考 fixture 进行中可见 ≤12 行，展开 ≤40 行；终端测试不超时。

证据（2026-08-13）：`cd packages/cli && bun test tests/presentation-shared/paint-budget.test.ts tests/tui/presentation/views.test.ts` → 30 passed。80 行进行中只出现 T69–T80 与「还有 68 行」，不含 T01/T68；进行中点击保持展开。耗时未接，按 Plan 可选，本停点不做。

## WP4：工具分流

- [x] 先写失败表驱动测试：`read_file`→inline、`execute`→block、`edit_file`→diff、`task`/未知→generic。
- [x] 实现 registry 与四个组件，删除单一 `ToolRow` 终态路径。
- [x] Block/Generic 默认预览走 12 行窗口。
- [x] 运行 focused tests（见证据）。

**完成信号：** 四种名字在测试里落到四种组件；未知名仍能渲染。

证据（2026-08-13）：`cd packages/cli && bun test tests/tui/presentation` 通过。`ToolRow` 已删除；`resolveToolRenderer` 表驱动覆盖 inline/block/diff/generic。

## WP5：底部 Dock

- [x] 先写失败测试：approval pending 时 BottomArea 为 Dock、输入栏失焦；时间线不含审批选择控件。
- [x] 先写失败测试：决定提交后 Dock 消失、结果行存在、焦点回输入栏。
- [x] 实现 ApprovalDock / QuestionDock 与 Thread 槽位切换；pending 从时间线去交互。
- [x] 运行 `cd packages/cli && bun test tests/tui/app-interaction.test.ts tests/tui/presentation/views.test.ts`。

**完成信号：** 审批过程中底部是操作栏，对话记录不可点选允许/拒绝。

证据（2026-08-13）：`cd packages/cli && bun test tests/tui/presentation/bottom-area.test.ts tests/tui/presentation/views.test.ts tests/tui/app-interaction.test.ts` → 47 pass。pending 审批/单选问答替换输入栏；完成后时间线只留「已允许」；开放式/多选/允许其他项的问答仍走输入栏。

**怎么看：** `bun run dev`，发一条会触发审批的消息（写文件或执行命令）。底部输入栏应换成「需要审批」操作栏，对话记录里没有允许/拒绝。选一项后 Dock 消失，时间线出现「已允许」，输入栏回来。

## WP6：页面收口

- [x] 先写失败测试：`ThreadView` 在 Compose + 有 workItem 时不再出现阶段顶栏文案。
- [x] 实现卸载 `WorkItemView`；补 SystemEvent（含 skill.loaded）、ErrorBlock、RunFooter；Overlay 换 token。
- [x] 运行 `cd packages/cli && bun test tests/tui/presentation/work-item-view.test.tsx tests/tui/presentation/overlays.test.ts tests/tui/presentation/views.test.ts`。

**完成信号：** Compose 对话页与 Build 同一套骨架；顶栏测试改为断言「未挂载」。

证据（2026-08-13）：`cd packages/cli && bun test tests/tui/presentation/work-item-view.test.tsx tests/tui/presentation/overlays.test.ts tests/tui/presentation/views.test.ts` → 46 pass。`ThreadView` 不再挂载 `WorkItemView`；`skill.loaded` 显示「已加载 Skill」；Run 失败走 ErrorBlock，工具失败不走；RunFooter 一行 muted；Picker 选中色为当前 Mode。

**怎么看：** `bun run dev`。Compose 对话页顶部不应再出现 Work Item 阶段条（「已锁定 / 进行中 / 活动：」）。发完一轮后底部时间线应有一行模型·耗时·用量；`/model` 或 `/skills` 打开后选中行应是当前 Mode 色（Compose 紫、Build 金）。

## WP7：文档与检查

- [x] 更新 `docs/user/交互使用.md`：底部 Dock、思考窗口、三种工具画法、Mode 色，删除「审批卡留在触发位置」。
- [x] 按落地路径增量更新 `docs/developer/architecture/TUI表现层.md` 与 `架构总览.md`。
- [x] 运行 `bun run typecheck`、`cd packages/cli && bun test tests/tui`、`bun run project:check`、`git diff --check`。
- [x] 把命令与结果写入 Task `test_evidence`；说明无版本号变更。

**完成信号：** 用户文档与架构和实现一致；项目检查通过。

证据（2026-08-13）：`bun run typecheck` 通过（并修了 `WriteFilePreview` 上非法 `backgroundColor`）；`cd packages/cli && bun test tests/tui` → 140 pass；`bun run project:check` 通过；`git diff --check` 通过。未改 `VERSION`。用户文档已区分 TUI 底部 Dock 与 Web 时间线交互卡。

## 检查点

- [ ] WP1 后：强模型或实现者确认色表与限额数字未走样。
- [ ] WP5 后：走一遍「发消息 → 思考 → 读文件 → 审批 → 回答」对照 Spec 组件表。
- [ ] WP7 后：按 `agent-skills:code-review-and-quality` 对照 Task/Spec 做 review，结论写回 Task。
