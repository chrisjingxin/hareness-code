---
id: ZC-112
title: 建立跨端共享展示策略
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-112-presentation
scope: 建立 presentation-shared 共享模块（language-catalog、semantic-tone、formatters、timeline-presenter、command-menu-policy、interaction-policy、tool-output-policy），仅提取 TUI/Web 已存在的重复纯逻辑；引入 Snapshot Selector 输出 FeatureAvailability，Presentation 不再直接检查协议 Capability。
acceptance: presentation-shared/ 下 7 个模块全部落地且只含纯函数/稳定语义（架构测试断言零 react/opentui/platform/ipc import）；TUI 与 Web 对重复逻辑（duration/usage 格式化、tool 摘要截断、命令菜单过滤、语言 alias 解析、语义 tone 映射、Timeline 展示语义）统一消费同一实现；Presentation 源码不再 import @za38/protocol 的 Capability 做可用性判断（grep 验证）；`bun run typecheck`、`bun run test`、`bun run project:check` 全绿。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: "presentation-shared 7 模块落地（language-catalog 复用 ZC-108 产物，activity-presenter 并入 timeline-presenter）；tests/presentation-shared 40 用例全绿（含零平台 import 架构断言与两端语义键收敛断言）；interactive/selectors 5 个 Selector + FeatureAvailability 12 用例全绿（含 JSON 往返相等可序列化断言、纯 capability 门 run 期间保持 true）；grep Capability src/tui src/web 仅剩 web/app.tsx 启动层 clientCapabilities 声明，presentation 层面板可见性与 skills/mcp manage 门全部经 selectNavigationView 消费（CAPABILITY_GATE 白名单有真实消费方）；TUI/Web 各自重复实现已删除（collapseToolOutput/argumentSummary/truncateSingleLine/APPROVAL_LABELS 等，grep 单一定义）；cd packages/cli && bun test 389+ pass，4 fail 为 web server happy-dom 测试间干扰预存问题（单独运行全绿，属 ZC-115 领域）；code review（code-reviewer agent）REQUEST-CHANGES 已闭环：panels manage 门迁移、可序列化断言加固、未知 decision 过滤、语义键收敛测试；bun run typecheck、bun run project:check 通过"
references: docs/developer/tasks/ZC-108.md
completed_at: 2026-08-05
---

## 背景

最终架构方案决策 D-10（**跨端只共享纯展示策略，不共享 React/OpenTUI 组件和具体颜色体系**）与阶段 5（完成条件：跨端行为一致，平台 UI 仍保持原生；A-08：Presentation 不直接检查协议 Capability，统一消费 FeatureAvailability）。

方案 §10 表格定义七个共享模块：command-menu-policy、interaction-policy、timeline-presenter、semantic-tone、tool-output-policy、language-catalog、formatters。ZC-108 已首建 `presentation-shared/language-catalog.ts`。

当前重复逻辑位置：

- duration/usage/context 格式化：`interactive/runtime.ts`（formatDuration/formatUsage/runtimeStatusSummary）+ Web presentation 侧重复实现。
- tool 摘要/截断/折叠阈值：`tui/application/adapter.ts` 的 collapseToolOutput（tests/tui/collapse-tool-output.test.ts）+ Web timeline/tool 组件重复阈值。
- 命令菜单过滤/排序：`tui/application/adapter.ts` filterCommandMenuItems + Web command menu 重复逻辑。
- 语义 tone：TUI `tui/presentation/colors.ts`/`theme.ts` + Web `styles.css` token（两端各自 hex/ANSI，共享的是 default/muted/accent/success/warning/danger 语义）。
- 语言目录：ZC-108 的 `presentation-shared/language-catalog.ts`（TUI syntax-parsers 与 Web Shiki loader 消费）。
- Capability 判断：`tui/app.tsx`/`web` 侧存在直接按 `Capability` 常量判断可用性的代码（如 index.ts clientCapabilities 属于 CLI 启动层可保留，但 presentation 层需改由 selector 输出的 FeatureAvailability 驱动）。

## 当前存在的问题

1. 同类展示策略在 TUI/Web 各写一份，规则漂移（如截断阈值、格式化单位、菜单排序），违反"业务语义共享只实现一次"护栏。
2. Presentation 直接 import `@za38/protocol` 的 Capability 判断可用性，与 Core 的 command availability 计算（commands.ts 已做 capability/Thread/Run/Interaction 可用性计算）重复且可能不一致。
3. 语义 tone 没有单一规范，两端颜色体系改版时容易各自漂移。

## 为什么现在要修改

- 阶段 5 是阶段 7 的前置：WebUiGateway 要序列化 Selector 视图（selectConversationView 等），Selector 输出 FeatureAvailability 后，Browser 才能只消费视图而不再接触协议 Capability（A-02/A-08）。
- 仅抽取"已存在"的重复逻辑，不做抽象洁癖式重写（方案 15.1 非目标）。

## 目标设计

### 模块清单（方案 §10 表格原样落地，全部纯函数）

| 模块 | 共享内容 | 不共享 |
| --- | --- | --- |
| `command-menu-policy.ts` | Slash query、排序、可见项过滤（迁移自 tui filterCommandMenuItems + web 等价） | hover/focus/菜单位置 |
| `interaction-policy.ts` | 选项映射、提交前视图校验提示（approval decision 展示顺序、question 必答提示） | Web 表单控件/TUI 输入框 |
| `timeline-presenter.ts` | Tool/Message/Interaction 展示语义（kind → 展示结构；中文文案映射集中于此，ZC-110 的 TUI/Web 各自映射并入） | 具体组件和布局 |
| `semantic-tone.ts` | default/muted/accent/success/warning/danger 语义枚举与映射函数 | 具体 hex/ANSI 颜色 |
| `tool-output-policy.ts` | 摘要、截断、折叠阈值策略（迁移自 collapseToolOutput） | 展开状态 |
| `language-catalog.ts` | canonical ID、alias、fallback（ZC-108 已建，本任务补全 16 语言用例） | Grammar、parser、token |
| `formatters.ts` | duration、usage、context 格式化（迁移自 runtime.ts 展示函数 + web 重复） | 本地化组件 |

### FeatureAvailability 与 Selector（方案 §5.7、A-08）

- `interactive/selectors/`：`selectConversationView()`、`selectInteractionView()`、`selectNavigationView()`、`selectCommandView()`、`selectRuntimeView()`；输出可序列化（无 Set/Map/函数/Error/DOM/OpenTUI 对象）。
- `FeatureAvailability`：由 Selector 从 snapshot 推导（如 `canSubmit`、`canCancelRun`、`canOpenThread`、`canToggleSkill`、`canManageMcp`、`canChangeModel`），并入相应 Selector 输出。
- Presentation（TUI/Web）删除对 `Capability` 的直接 import 判断，改用 Selector 输出；`index.ts` 的 clientCapabilities（启动层声明）保留。
- 高频 Timeline 与低频 Catalog 分片发布机制属于 ZC-114（gateway revision），本任务只建立 Selector 函数与其单测。

### 依赖规则

- `presentation-shared/` 只 import 纯 TS 与 `@za38/protocol` 的类型（如需），禁止 import react/react-dom/@opentui/ipc/tui/web/platform/DOM/WebSocket。
- packages/protocol 不收上述模块（方案 §10：UI 通信契约放 CLI 内部，独立版本化——contracts 落位由 ZC-114 执行）。

## 实施步骤

1. 盘点 TUI/Web 两侧重复纯逻辑清单（grep formatDuration/formatUsage/collapseToolOutput/filterCommandMenuItems/Capability 使用点），列出迁移前后对照。
2. 新建 `presentation-shared/` 其余 6 个模块（language-catalog 补全）；为每个模块写单元测试（含边界：空输入、超长、未知语言、空菜单）。
3. 迁移 TUI 消费方：adapter/timeline/theme/colors 改用共享模块；迁移 Web 消费方：presentation 组件改用共享模块；删除本地重复实现。
4. 新建 `interactive/selectors/` 五个 Selector + FeatureAvailability；controller 的 buildSnapshot 委托 Selector（或保持并存，本任务以"Selector 可独立测试且与 snapshot 一致"为准）。
5. TUI/Web presentation 改用 FeatureAvailability（删除 Capability 判断）；新增 `tests/presentation-shared/` 测试目录。
6. `架构总览.md` 补"共享展示策略与 Selector"小节；验证 typecheck/test/project:check；提交证据。

## 范围

- presentation-shared 七模块抽取、Selector/FeatureAvailability、两端消费迁移、重复逻辑删除。

## 非范围

- 不改 InteractiveIntent/Outcome/Snapshot 领域形状；不做分片发布（ZC-114）。
- 不共享组件树/高亮引擎/焦点模型/布局状态（方案 3.1 强制约束）。
- 不把 ViewModel/主题/格式化放入 packages/protocol；无版本变更；不动 Python。

## 验收清单

- [x] `presentation-shared/` 7 个模块存在；`tests/presentation-shared/` 覆盖每个模块边界用例（39 个，含 architecture 断言）。
- [x] `tests/presentation-shared/architecture.test.ts` 断言该目录不 import react/opentui/ipc/tui/web/platform。
- [x] `grep -rn "Capability" packages/cli/src/tui/ packages/cli/src/web/` 仅剩启动层合法使用（web/app.tsx clientCapabilities；presentation 目录无 Capability 判断）。
- [x] TUI/Web 各自 `bun test --isolate tests/tui`、`tests/web` 全绿（非 isolate 下 TUI/Web 240 测试 236 pass，4 fail 为 web server happy-dom 预存干扰）；无重复实现残留（grep formatDuration/collapseToolOutput/argumentSummary 等仅 presentation-shared 一处定义）。
- [x] Selector 单测：输出可 JSON.stringify（无 Set/Map/函数），FeatureAvailability 与 snapshot 状态一致。
- [x] `bun run typecheck`、`bun run test`、`bun run project:check` 全绿（全量 389 测试，4 个预存 web 干扰失败与本次改动无关）；证据与 OCR 结论写入本任务。

> 实施说明：任务文档原问题清单部分过时——命令菜单过滤（filterCommandMenuItems）与 duration/usage 格式化在 ZC-109/110/111 期间已共享，本次将其正式归位到 presentation-shared；TUI 侧 Capability 判断已在 ZC-111 清理，本次清掉 Web 侧残余（web-app.tsx/panels.tsx 的 panel 可见性与 skills/mcp manage 门）。Tool 输出截断（TUI collapseToolOutput vs Web truncateSingleLine，阈值 4 行/360 字符 vs 80 字符）与 approval 文案（approve_thread "允许此会话" vs "本线程允许"）确认为真实漂移，已统一。
>
> 文案统一说明（code review 后补记）：interaction cancelled 未完成→已超时、resolved 已恢复执行→已解决、pending 已恢复执行→等待中（timeline-presenter 统一采用 Web 既有更精确语义）；TUI 工具卡 running 标签 执行中→运行中；RunSummary ctx 由原始数字改为 k 紧凑格式（120000/256000→120k/256k）。未知 approval decision 由"静默丢弃"改为经 `isApprovalDecision` 过滤后丢弃（行为等价，畸形服务端数据不渲染）。semantic-tone 语义枚举已建立并通过两端共有语义键收敛测试防漂移；两端色值映射仍各自实现（方案要求）。
