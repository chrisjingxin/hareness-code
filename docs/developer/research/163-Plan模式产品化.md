# 163-Plan模式产品化（竞品源码调研）

- 竞品：Qwen Code（`/Users/zhangjingxin/Code/OpenSource/qwen-code`）、Oh My Pi（`/Users/zhangjingxin/Code/OpenSource/oh-my-pi`）、Grok Build（`/Users/zhangjingxin/Code/OpenSource/grok-build`）
- 调研日期：2026-08-29；Grok Build 补充调研：2026-08-31（commit `ba76b0a683fa52e4e60685017b85905451be17bc`）
- 服务对象：HC-164（Plan 模式产品化：/plan 命令 + 计划产出闭环）。调研文件名保留 163；Task 编号因 HC-163 已用于「本地诊断日志体系」而改为 HC-164。grill 确认后的产品决策以 Task 为准，其中计划审批取 Grok Build 的文档+三动作，不取 Qwen 在卡上选下一档权限；假工具改为做成真门，而不是先删掉。
- 范围决策（用户，2026-08-29）：**第一版仅在 build 工作模式下提供 /plan；Compose 下不做**。
- 结论先行：**plan 保持为审批模式（我们已有正确的底座），产品化补四件事——命令入口、计划落盘、审批呈现、批准续跑**。两家的差异只在闭环的厚薄；我们第一版取 Qwen 的闭环厚度（最小可用），保留 Oh My Pi 的方向作后续演进。

## 一、我们现在有什么（调研基线）

| 能力 | 现状 | 位置 |
|------|------|------|
| plan 审批模式 | 已有，`PlanModeMiddleware` 白名单硬拒绝写/执行/task | `packages/agent/harness_agent/runtime/agent.py:840` |
| 模式切换 | 仅 Shift+Tab 五档循环（含 plan），无斜杠命令 | `packages/cli/src/interactive/runtime.ts:11` |
| Run 携带模式 | `RunStartParams.approval_mode` 每 Run 独立 | `packages/protocol` |
| 模型侧切模式工具 | `enter_plan_mode`/`exit_plan_mode` 假开关（状态无消费方） | `tools/harness_tools.py:207-228` |
| 计划落盘 / 呈现 / 批准续跑 | 均无 | - |

注意与 Compose 的 plan 阶段（需求工程流水线中的一个 gate）是两回事，本调研只针对会话内 Plan 模式。

## 二、Qwen Code 的实现

### 进入

- `/plan` 斜杠命令（`packages/cli/src/ui/commands/planCommand.ts`）：
  - 无参：进入 plan 模式，提示"agent 将只分析不执行"；
  - 带参：进入 plan 模式后**把参数直接作为第一条消息提交**（`submit_prompt`）——用户不会"进了 plan 不知道说什么"；
  - `/plan exit`：恢复进入前记录的 `prePlanMode`。
- 模型侧 `enter_plan_mode` 工具存在但受三重守卫（`packages/core/src/tools/enterPlanMode.ts`）：
  - `userRequested` 参数：只有用户本轮明确要求时才允许置 true；模型自发调用在 YOLO 下是 no-op（用户选 YOLO 是为了低摩擦执行，静默降权会"意外惊喜"）；
  - 非交互/无 ACP 会话直接拒绝（退出 plan 需要用户交互，无人可答）；
  - 进入是"降权"，不需要用户确认，因此权限层恒 allow。
- 进入时保存 `prePlanMode` 并递增 `approvalModeRevision`（版本号，后面防陈旧审批用）。

### 约束

- PLAN 是 `ApprovalMode` 枚举之一，权限引擎统一 deny 写类工具。
- shell 单独有 `plan-mode-shell-policy.ts`：用 AST 把命令分类为只读/写/未知。写→硬拒并提示"把该动作写进计划"；未知→**仅本次单条审批**（批准不改变模式）；模式或命令变化后旧审批判定 stale 失效。
- `getPlanModeSystemReminder`（`core/prompts.ts:1133`）**每轮**注入 `<system-reminder>`：声明只读要求 + "迭代规划工作流"——探索（read/grep/glob）→ 记录发现 → 有歧义就用 AskUserQuestion 问用户再回头探索；第一轮先扫几个关键文件就发起第一轮提问，不闷头探索完再问。

### 产出、呈现与退出

- 模型调研完调用 `exit_plan_mode(plan: markdown)` 提交计划（`packages/core/src/tools/exitPlanMode.ts`）。工具描述明确：研究类任务不要用；不要用 AskUserQuestion 问"计划行不行"——这个工具本身就是干这个的。
- CLI 弹出 `type: 'plan'` 确认对话框，四个选项：
  - `RestorePrevious`：恢复进入前的模式（回 plan 前状态，继续改计划）；
  - `ProceedAlways`：切到 auto-edit 继续；
  - `ProceedOnce`：切到 default 继续；
  - `Cancel`：留在 plan 模式。
- 批准后：`config.savePlan(plan)` **原子写**到 `~/.qwen/plans/<sessionId>.md`（可配置 `plansDirectory`，但必须在项目根内）；`setApprovalMode(targetMode, {fromApprovedPlanExit: true})`；给模型的回包是 "User approved. You can now start coding. Start with updating your todo list if applicable."
- 呈现：`PlanSummaryDisplay.tsx` 渲染计划 markdown（批准绿、拒绝黄）。无全屏评审。
- 陈旧保护：execute 时校验 revision，模式中途被人改过则拒绝执行审批。
- 团队场景另有 plan-required teammate 走 leader 审批（与单机会话无关，不展开）。

## 三、Oh My Pi 的实现

Oh My Pi 把 plan 做成了一个**重产品功能**（`packages/coding-agent/src/plan-mode/` + `modes/interactive-mode.ts`），闭环最完整。

### 进入

- `/plan` 三态切换：off → plan → paused → off；带初始 prompt 时进入后直接作为第一轮 plan 消息提交。
- 可配置 `plan.defaultOnStartup`（新会话直接进 plan）；resume 时从会话 journal 恢复 plan/plan_paused 状态（含 planFilePath）。
- 进入动作是"三换"：
  1. **换工具集**：切到只读集，但保留 `write`（写计划文件 + 提交审批都用 write）；
  2. **换模型**：切到 `plan` 角色配置的模型（规划用更强模型），退出时恢复（`model-transition.ts` 把决策做成纯函数，流式中则 defer 到轮末）；
  3. **换 prompt**：注入 plan-mode 系统 prompt；状态栏显示 plan 状态与计划文件路径。

### 约束（与 Qwen 思路不同）

- 不逐条 deny 工具，而是**工作树只读 + 沙箱例外**：`plan-mode-guard.ts` 对每个 write/edit 目标解析路径（含 hashline 头、`local://` scheme、realpath 归一化），落在 session 本地 artifact 沙箱内放行，沙箱外一律拒绝；改名/删除一律拒绝。
- 计划本身就是文件：模型把计划写到 `local://<slug>-plan.md`，用 edit 反复打磨，最后把标题写到 `xd://propose` 设备路径提交审批。**计划从草稿到批准始终是一个 markdown 文件**，不是对话里的临时文本。

### 呈现与审批（交互最重）

- `PlanReviewOverlay`（1226 行）全屏评审：滚动正文 + TOC 侧栏 + 按节批注（annotation，可在外部编辑器里写）+ 删除节 + undo + 复制 + 模型档位滑条。
- 四个选项：**Approve and execute / Approve and compact context / Approve and keep context / Refine plan**（refine 会把批注收集成反馈发回模型继续改）。

### 批准后（handoff 最完整）

- 计划文件**永不重命名**（保证链接稳定）；标题从"显式提供 → H1 标题 → 文件名 → 'plan'"逐级回退（`approved-plan.ts`）。
- 三种批准路径对应三种上下文策略：清空会话重开（拷贝 plan artifacts 过去）、压缩上下文（plan-mode-compact-instructions 把 plan 轨提炼成摘要作为新 cache anchor）、原样保留。
- 设 `planReferencePath` 后，计划的后续使用有三条通道：
  1. 主 agent 注入 `plan-mode-approved` 合成 prompt（含计划文件路径，作为新 cache anchor）直接开始执行轮；
  2. 子代理 handoff：`loadOverallPlanReference` 把计划全文带给子代理（明确要求 plan 模式探索期间不注入，草稿不算数）；
  3. 压缩保护：`plan-protection.ts` 注册 matcher，保证对计划文件的读取结果**不会被 compaction 修剪掉**。
- 会话名自动用计划标题 seed（未命名时）。

## 四、横向对比

| 维度 | Qwen Code | Oh My Pi | 我们现状 |
|------|-----------|----------|----------|
| 模式本质 | plan = ApprovalMode 之一 | 独立模式（换工具集+换模型+换 prompt） | plan = approval_mode 之一（同 Qwen） |
| 进入入口 | /plan（可带首条消息）+ Shift+Tab + 受守卫的模型工具 | /plan 三态 + 启动配置 + resume 恢复 | 仅 Shift+Tab |
| 只读约束 | 权限 deny + shell AST 分类 + 每轮 system-reminder | 工具集替换 + 写路径守卫（沙箱例外） | PlanModeMiddleware 白名单 |
| 计划形态 | 工具参数里的 markdown，批准时落盘 | 全程是文件（草稿→批准同一文件） | 无 |
| 呈现 | 审批对话框 + markdown 组件 | 全屏评审 overlay（TOC/批注/refine） | 纯文本消息 |
| 退出 | /plan exit 或审批选项直达执行模式 | 三态 toggle 或审批四选项 | 手动 Shift+Tab |
| 批准续跑 | 切模式 + 提示更新 todos | 合成 prompt 直接开跑（可选压缩/新会话） | 无 |
| 计划后续使用 | 存档文件 | 引用注入 + 子代理 handoff + 压缩保护 | 无 |

## 五、对我们的设计建议

我们的底座判断是对的（plan 属于审批模式体系，Qwen 同构），**不需要**另起独立模式体系；产品化差距按四个能力补齐，厚度取 Qwen（最小可用），Oh My Pi 的重型评审与 handoff 列为后续阶段方向：

1. **`/plan` 命令**（CLI 单包）：无参进入（记录进入前模式，对齐现有 `approvalModeOverride` 语义）；`/plan <目标>` 进入并把目标作为第一条消息提交；`/plan exit` 恢复；状态栏/图标展示 plan 进行中。Shift+Tab 循环保留。
2. **删除 `enter_plan_mode`/`exit_plan_mode`**（假开关）。注意：计划审批门仍需要一个**工具形状的触发器**——我们的 HITL interrupt 以工具名为 key（`interrupt_on_for_approval_mode` 的 `extra_interrupt_tools`），所以"提交计划"应做成一个 schema-only、由 contract 接管的常驻工具调用（类似文件工具的模式），调用即触发 interrupt 弹出 CLI 计划卡片，审批结果作为工具结果回给模型。它与 Qwen 的 exit_plan_mode 职责相同，但不动任何真实状态。常驻声明（进 `RESIDENT_TOOL_NAMES`）意味着进出 plan 模式**不改变工具块形状**，避免额外的 cache 断点（Qwen 在 issue #5210 里也是因为同类原因让 exit_plan_mode 永远声明、不走延迟加载）。
3. **计划提交与呈现**（协议 + CLI）：新协议载荷（计划 markdown + 元数据）→ CLI 渲染计划确认卡片（markdown），选项第一版三个：继续打磨（留在 plan，反馈发回）/ 本次继续（切 default 执行）/ 取消。
4. **批准续跑与落盘**：批准后 host 以 default 模式自动发起下一次 Run，注入"计划已批准 + 计划文件路径"上下文；计划 markdown 落盘（建议放 thread 持久化侧而非用户工作树，避免污染仓库；具体位置在 Spec 定）。每轮 plan 模式 system prompt 注入迭代规划工作流（探索→记录→提问，我们已有 ask_user）。
5. **后续阶段方向**（不在第一版）：plan 独立模型角色（规划/执行分模型）、计划引用注入子代理、压缩保护、"批准+压缩/新会话"路径、全屏评审与批注 refine。

### 风险与边界

- 陈旧保护必须做：批准动作携带当时的模式版本，中途用户手动换档后审批作废（Qwen revision 方案值得抄）。
- shell 未知命令的"单条审批"策略：我们的 `execute` 在 plan 模式当前是硬拒（白名单不含），第一版维持硬拒更简单安全；是否放开到"AST 判定只读才放行"列为 Spec 决策点。
- Compose 的 plan gate 与本功能的命名/文案需要区分（feature_area 分开，避免看板混淆）；第一版 /plan 仅在 build 工作模式下生效。

## 六、补充分析：Qwen 为什么保留 enter/exit_plan_mode

（回应"我们删掉这两个工具是否亏了功能"——结论：不亏，但 exit 的审批门职责要以 schema-only 工具承接。）

### enter_plan_mode 的存在意义（源码证据：`enterPlanMode.ts:94-105` 注释）

**先澄清一个关键事实：这两个工具不是只给无头/ACP 用的**。在交互会话里，用户说"别写代码，先做计划"这类自然语言请求时，Qwen 的设计路径就是靠这两个工具完成进入和退出：模型识别出用户显式要求 → 调 `enter_plan_mode(userRequested=true)` → `config.setApprovalMode(PLAN)` 即时生效（live config，运行中可切）→ 工具结果带回 plan-mode system-reminder → 模型在只读约束下调研 → 调 `exit_plan_mode(plan)` → 交互界面弹计划确认框 → 批准后 `setApprovalMode(目标模式)` 同样即时生效 → "User approved. You can now start coding"，同一会话继续执行。`/plan` 命令和 Shift+Tab 是**用户手动**路径，两个工具是**模型驱动**路径；无头/ACP 只是"只有工具这一条路"的场景（斜杠命令 `supportedModes: ['interactive']`、无键盘循环）。YOLO 的守卫也印证这一点：模型**自发**进入是 no-op，但用户本轮明确要求（`userRequested=true`）即使在 YOLO 下也放行——说明工具路径就是为承接自然语言请求而设计的。

1. **它是无头/ACP 会话进入 plan 模式的唯一门**：`/plan` 斜杠命令声明 `supportedModes: ['interactive']`，无头会话和 ACP（Zed 集成、stream-json）里没有 Shift+Tab。用户在这些会话里说"先做计划"，模型必须有真实入口把模式切过去，否则只能"口头计划"，只读强制不会生效。代码注释原话："This tool is ALSO the only door into plan mode in headless/ACP sessions"。
2. **把自然语言请求映射成真实模式切换**：多轮对话中用户中途说"先规划再动手"，工具调用让这次切换有 UI 反馈、触发 system-reminder、记录 prePlanMode。
3. **守卫防止滥用**：YOLO 下模型自发进入是 no-op（用户选 YOLO 就是要低摩擦执行，静默降权是"惊喜"）；必须 `userRequested=true`（用户本轮明确要求）才生效；无头且无 ACP 直接拒绝——因为退出 plan 需要用户交互，无人可答的门不该进。

### 对我们的含义：进入侧存在一个 Qwen 没有的架构差异

Qwen 的审批模式是 **live config**（`setApprovalMode` 运行中即时改变权限引擎行为）；我们的 `approval_mode` 在构图时烧进 graph（PlanModeMiddleware、白名单、系统 prompt 后缀、interrupt 配置），**run 进行中切不了**。因此"批准后续跑"完全对得上（计划门 interrupt → CLI 卡片 → 结果回给模型 → 结束 plan run → host 以目标模式自动发起下一次 Run，thread 上下文天然携带）；但"自然语言进入"要做实，意味着"进入 = 优雅结束当前 run + host 以 plan 模式重启"，比 Qwen 的原地切换重。v1 三个候选（Spec 决策点）：

- (a) 不做模型侧进入：自然语言请求时模型就在当前模式下给计划（default 模式写操作本就要 HITL 审批，风险低）；要硬约束由用户按 `/plan`；
- (b) 做进入门：新增协议动作，模型调用后 host 结束当前 run 并以 plan 模式重启（thread 上下文保留）；
- (c) prompt 引导：模型收到此类请求时提示用户使用 `/plan`。

对我们的含义：我们当前两个可交互入口（TUI 斜杠命令 + Shift+Tab）都在 build 工作模式覆盖范围内；无头 Run 连审批门都无法应答（Qwen 也因此在无头禁用进入），所以 enter 工具对我们没有不可替代的价值，**不保留**。将来若接 IDE/ACP 等自然语言驱动的新表面，再按 Qwen 的守卫形态引入。

### exit_plan_mode 的存在意义

1. **它是审批门的载体**：Qwen 的 HITL 以工具调用确认为中心，`ToolPlanConfirmationDetails(type: 'plan')` 必须挂在一次工具调用上才能暂停执行、弹出计划卡片、带回结构化结果（RestorePrevious/ProceedAlways/ProceedOnce/Cancel）。工具参数 `plan` 就是提交的计划正文——模型"说完计划"和"请求批准"是同一个动作。
2. **结果即工具结果**：批准/拒绝以 ToolMessage 回给模型（"User approved. You can now start coding..."），模型在下一轮有明确依据转入执行；拒绝时留在 plan 模式继续改。
3. **陈旧保护挂在它身上**：revision 校验、模式中途被手动改档后审批作废，都在 execute 里收口。
4. **永远声明、不延迟加载**（issue #5210）：plan 模式指示模型直接调用它，延迟加载会多一轮 tool_search 且引入工具块形状变化。

对我们的含义：**审批门职责必须保留，但载体不是"真执行工具"**。我们的 interrupt 同样以工具名为 key，所以做一个 schema-only、contract 接管、触发 interrupt 的常驻 `submit_plan` 类工具即可；删除的只是那两个改不动任何状态的假开关。

## 七、补充分析：切换 prompt/工具/模型对 prompt cache 的影响

### 机制

provider 的 prompt cache 按**请求前缀**命中（system prompt → tools → messages 逐段前缀匹配）。前缀上任何一处变化，其后所有内容全部 miss。因此：

- **换系统 prompt**：整个会话缓存失效；
- **换工具集（bind_tools 形状）**：tools 段之后全部失效；
- **换模型**：不同模型的缓存命名空间不同，等于从零开始。

### 两家的实际处理（源码证据）

**Oh My Pi 承认并接受这个代价**：`interactive-mode.ts:2602` 注释原话——"Suppress cache-miss marker on the next turn: plan mode changes the system prompt, which predictably invalidates the cache."（进 plan 模式换系统提示词必然打掉缓存，UI 上的 cache-miss 标记都懒得报）。它是**每次模式转换一次性全额重放**，转换后新前缀成为新 cache 锚点，后续轮次正常命中。它还在批准路径上**主动利用**重锚：批准后的 `plan-mode-approved` 合成 prompt 被刻意安排为"fresh cache anchor"（压缩后以计划为起点重建缓存）。

**Qwen 的架构则刻意避免动前缀**：

1. 系统提示词**永远不变**；plan 模式的指令不进 system prompt，而是每轮以 `<system-reminder>` 块**prepend 在用户消息前**注入（`client.ts:2890-2935`）——消息流本来就是追加式的，前缀缓存不受影响；
2. 只读强制放在**权限层**（deny 写工具 + shell AST 分类），不靠 prompt；
3. 唯一的缓存断点是进出 plan 时 `setTools()` 改变工具声明（enter 时 reveal exit_plan_mode），一次性代价；
4. 模式被手动改档时补一条 one-shot `getManualPlanExitSystemReminder`，纠正模型的过期认知（#7671：模型注意不到 reminder 悄悄消失，所以模式激活期间每轮都重注入）。

### 对我们的影响判断

1. **我们当前架构的模式切换本来就在打缓存**：`create_harness_agent` 把 `approval_mode_prompt(approval_mode)` 烧进系统 prompt（`agent.py:820`），模式一换（含 plan↔default 往返）系统 prompt 就变。这不是 /plan 新引入的问题，而是现状。
2. **第一版建议接受转换期失效**：模式切换是用户刻意的低频动作，且 /plan 闭环里"批准"天然是重锚点（批准后续跑本来就是新一轮执行上下文）。为此把 plan 指令改造成 Qwen 式消息流注入（需要新做 per-turn reminder 注入机制）属于提前优化，不符合最小可用原则。
3. **但要做到"能省则省"**：计划审批门工具**常驻声明**（不随模式进出改变工具块形状）；plan 模式的 prompt 增量控制在与审批模式 prompt 同级的小段后缀（现状已是如此），避免大段重写。
4. **后续阶段**（若规划/执行双模型落地）：换模型本身就是全新缓存命名空间，届时“plan 角色模型”的缓存代价与 Oh My Pi 相同，属于该阶段的已知成本；可用“压缩后重锚”（Oh My Pi 的 compact-approve 路径）把代价转化为结构收益。

## 八、Grok Build：行批注与再打开计划

本节只调研 HC-164 停点 4 需要的三条链路：行/范围批注、打回 feedback 的组装、以及空闲时再打开当前计划。

### 8.1 批注是客户端本地状态，不是线上协议字段

- 客户端用 `PlanComment { id, line_range: Range<usize>, text }` 表示一条批注，`PlanApprovalViewState` 同时保存 `comments`、下一个 id、正在编辑的 comment id 和当前选区。`line_range` 内部是半开区间，但表示的是 **1-based 原始 Markdown 行号**；单行 `2..3` 显示为第 2 行，范围 `3..5` 显示为 3–4 行。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/views/plan_approval_view.rs:37-72` 的 `PlanApprovalFocus` / `PlanComment` / `PlanApprovalViewState`。
- 选区不直接用渲染后的可见行索引。`LineViewerState::selected_line_range` 会从可见选区的首尾向内找到真实 source line，再返回原始行号范围；因此 Markdown 换行、空行、折行和已插入的批注行都不会让引用偏移。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/views/file_search/line_viewer.rs:943-969` 的 `selected_line_range`；软换行映射回归见同文件 `1815-1831`。
- 交互上，`c` 或 `Enter` 对当前行/可视选区建批注；若光标在已有批注上则进入编辑。保存时仅修改/追加 `PlanComment`，再用 `rebuild_with_comments` 把批注行插回对应原文范围末尾；`x` 删除光标下的批注。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/viewer.rs:121-183`、`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/plan.rs:396-503`，以及 `/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/views/file_search/line_viewer.rs:875-940`。

**对 HC-164 的直接启示：** 互动协议不必边输入边同步批注；TUI/Web 都可以在本地维护统一的 `{id, startLine, endLine, text}` 视图状态，但行号必须锨定原始 Markdown，不能锨定视觉折行。

### 8.2 发给模型的 feedback 是由批注压成的可读文本

- `PlanApprovalViewState::format_feedback` 遍历批注，将单行编成 `Proposed plan line N:`，范围编成 `Proposed plan lines N-M:`，紧接用 `> ` 引用所选原文，然后加 `Comment:` 和意见。若还有整体打回文字，则作为 `Additional feedback:` 追加；多条批注以空行分隔。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/views/plan_approval_view.rs:112-146`，预期完整文本的回归见 `435-455`。
- Grok Build 保留两种来源：内联计划把当时原文摘录进 feedback，文件计划则只发 `@plan.md:N-M` 和意见。其分支位于同文件 `112-145`、`194-229`，文件引用格式回归见 `474-488`。HC-164 的 `interaction.plan` 已携带计划正文，按 Spec 的“行号 + 摘录 + 意见”实现就应固定使用内联格式，避免模型还要额外读文件才知道批注指什么。
- 打回时，客户端只在 wire response 中发 `{ outcome: "cancelled", feedback?: string }`，批注数组本身不过线；空白 feedback 会被折叠成缺省。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/views/plan_approval_view.rs:149-187`；协议定义见 `/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-tools/src/implementations/grok_build/exit_plan_mode/types.rs:6-24`。
- Shell 收到 `cancelled` 后把该字符串包成 `The user wants to revise the plan. The user said:\n{feedback}` 的工具结果，留在 Plan 模式继续本轮；无 feedback 时改为让模型询问用户想怎么改。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-shell/src/session/acp_session_impl/tool_calls.rs:221-254` 与 `1277-1297`。
- 批准时的批注不是打回 feedback：客户端先回 `approved`，如果存在批注，另外产生 `Action::Interject`，内容为 `The user approved the plan with the following review comments:` 加同一份格式化批注。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/plan.rs:178-215`。这与 HC-164 “批准时批注附在实现轮提示”同构，应用同一个 formatter 生成一份稳定文本，再分流给 `revise.feedback` 或批准后的实现提示。

### 8.3 “再打开”复用同一预览，但状态分两类

- `/view-plan` 是 session-scoped 命令，别名正是 `/show-plan` 和 `/plan-view`，唯一动作是派发 `Action::ShowPlan`。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/slash/commands/view_plan.rs:1-32`。
- `dispatch_show_plan` 不切换模式：如果当前还有 `plan_approval_view`，则调 `reopen_plan_approval()` 回到那次待决策交互；否则只调 `show_plan_preview()` 打开已保存计划。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/dispatch/modes.rs:12-25`。
- 预览正文的优先级是：当前审批请求携带的非空正文 → 最新内联计划 → 当前 session 的 `plan.md`。如果是挂起的空计划审批，仍打开带操作按钮的占位预览；真没计划且没有挂起审批时才提示 `No plan written yet.`。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/plan.rs:69-155`。
- 重开挂起审批时，`reopen_plan_approval` 先 stash 用户当前输入，把审批焦点放回 Preview，并恢复 `feedback_active`；审批完成后再把原输入还回编辑器，避免“打开看一眼计划”丢掉半写的 prompt。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/plan.rs:272-285`；恢复草稿的回归测试见 `/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/acp_handler/tests/interactions.rs:341-385`。
- 除了命令，状态栏的 plan chip / 待批准状态也可点击，复用同样的“有挂起审批则 reopen，否则普通 preview”分支。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/mouse.rs:76-93`。
- 普通（非挂起审批）计划预览也支持“casual comments”：用户可继续对行批注，按 `s` 或 `Ctrl+Enter` 后组成 `Plan feedback:\n\n{body}` 并作为新 Prompt 发给模型。来源：`/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/viewer.rs:125-169` 与 `/Users/zhangjingxin/Code/OpenSource/grok-build/crates/codegen/xai-grok-pager/src/app/agent_view/plan.rs:505-674`。

**对 HC-164 的直接启示：** `/view-plan` 不应创建第二个独立审批对象。挂起 `interaction.plan` 时要恢复原交互及未提交批注；没有挂起交互时才是只读预览。停点 4 的明确范围是“空闲再打开”，是否像 Grok Build 一样允许从闲看预览另起一条 plan feedback Prompt，属于额外行为，不应在 HC-164 未修订 Spec 前顺带实现。
