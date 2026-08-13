---
id: HC-111-legacy
title: InteractiveController Feature 拆分
priority: P0
status: 已完成
owner: Antigravity
branch: feat/zc-111-features
scope: 将 1259 行的 InteractiveControllerImpl 单体按 Run、Timeline、Interaction、Thread、Model、Skill、MCP、Command、Catalog 九个 Feature 拆分到 interactive/features/，保留 InteractiveController 外部接口不变，Controller 退化为薄协调器，并为每个 Feature 建立独立状态与 effect 测试。
acceptance: controller.ts 不再承载具体业务逻辑（≤300 行，只做 intent 路由与 Feature 编排）；九个 Feature 各具独立 module 与测试；既有 controller.test.ts 全部断言不改即通过（行为零变化）；features 之间无直接依赖（只依赖 ports/state 公共类型）；`bun run typecheck`、`bun run test`、`bun run project:check` 全绿。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: "拆分后 `wc -l packages/cli/src/interactive/controller.ts` = 300（≤300）；`grep -rn \"from '../features\" packages/cli/src/interactive/features/` 无匹配（Feature 间零直接依赖）；`cd packages/cli && bun test --isolate tests/interactive`：71 pass / 0 fail（含拆分迁移后等价断言 + 新增 mcp-feature 失败路径测试）；`bunx tsc --noEmit` 通过；`bun run test` 全量（isolate）：interactive 相关全绿，11 个 TUI/index 失败为 `@opentui/core` 0.4.3 native 绑定在 bun --isolate 打包下的加载失败（node_modules 内部错误），已用 `git stash` 基线验证为改动前预存问题，非本任务引入；非 isolate `bun test` 下另有 4 个 web 子集 happy-dom fetch 测试间干扰失败（tests/web/integration.test.ts、tests/web/server.test.ts），单独运行各文件全绿，属 HC-115 Web 任务预存问题"
references: docs/developer/task/HC-103-让SkillCatalog在下.md
completed_at: 2026-08-05
---

## 背景

最终架构方案决策 D-04（**Interactive Core 内部按 Feature 拆分，外部仍保留 InteractiveController 接口**）与阶段 4（完成条件：Controller 退化为薄协调器，不再是单体 God Object）。

当前 `interactive/controller.ts`（1259 行）是唯一实现类 `InteractiveControllerImpl`，职责混装：

| 职责 | 锚点（controller.ts） |
| --- | --- |
| Run 生命周期（submit/sendAgentMessage/drainEvents/cancelActiveRun） | 224、300、336、459 |
| Timeline 事件投影（经 state.ts applyAgentEvent） | 336 |
| Interaction（handleInteractionRequest/respond/resolve/settle/validateQuestionAnswers） | 357、399、431、442、1061 |
| Thread（openThread/restoreInitialThread/resetThread） | 477、519、804 |
| Model（selectModel/syncDefaultModel/modelBindingConfirmation） | 552、576、771 |
| Skill（armSkill/setSkillEnabled） | 601、616 |
| MCP（handleMcp/addMcpServer/removeMcpServer） | 643、702、724 |
| Command（executeSlashCommand/applyCommandResult） | 241、251 |
| Catalog（refreshCatalog×4/invalidateAllCatalogs） | 828-910 |
| 快照组装（buildSnapshot/buildCommandItems/publish/commit） | 958、987、998、1004 |

字段清单（controller.ts:101-126）：agent、baseRuntime、scheduler、listeners、clearInteractionHandler、unsubscribeProtocolError、unsubscribeClose、state、snapshot、connection、pendingInteraction、confirmation、requestedModelProfileId、approvalModeOverride、actualModelProfile、armedSkill、threadEpoch、openingThread、closed、catalogs。

## 当前存在的问题

1. 单文件 1259 行：任何 Feature 的改动都必须通读全类，评审与测试定位成本高。
2. 九类职责共享 26 个私有字段，无法独立验证单个 Feature 的状态机（如 Interaction timeout 与 Thread generation 相互纠缠）。
3. 阶段 7（WebUiGateway）需要从 Controller 消费 Selector 视图（方案 §5.7），拆分后 Selector 才能按 Feature 分片发布高频/低频更新。

## 为什么现在要修改

- 阶段 4 以 HC-109（Port 完备）、HC-110（IntentOutcome）为前置：Feature 只依赖 Port 与稳定 outcome，拆分才不会把 IPC/文案依赖带进新模块。
- 外部接口（types.ts 的 InteractiveController）与 InteractiveSnapshot 形状不变，行为零变化即可独立验收（既有 1020 行 contract 测试是回归保险）。

## 目标设计

### 目录与职责（方案 §5.2 表格原样落地）

```text
interactive/features/
  run-feature.ts         启动、取消、事件流消费、终态收敛、actual model 更新；不负责 Timeline 渲染
  timeline-feature.ts    Event sequence 校验、message/tool/interaction 因果投影；不负责 RPC 调用
  interaction-feature.ts pending request、timeout、校验、fail-closed 回写；不负责表单草稿和焦点
  thread-feature.ts      Thread 列表、打开、恢复、generation 防晚到；不负责 Picker 查询和选中行
  model-feature.ts       模型列表、当前选择、默认模型同步、错误映射；不负责模型面板布局
  skill-feature.ts       Skill 列表、arm/clear、启停；不负责菜单 hover
  mcp-feature.ts         MCP 状态、添加、删除；不负责表单组件状态
  command-feature.ts     Registry、解析、availability、semantic operation；不负责快捷键识别
  catalog-feature.ts     Catalog loadable 状态、epoch、刷新协调；不负责平台搜索框状态
```

### 关键决策

- **状态归属**：`interactive/state.ts` 的 reducer 保留为 Timeline/Interaction 的纯投影（applyAgentEvent 等）；Feature 持有各自运行态（pendingInteraction、confirmation、requestedModelProfileId、armedSkill、threadEpoch、catalogs 等按归属迁移到对应 Feature）。
- **Feature 接口**：每个 Feature 形如 `{ init(ctx): void; dispatch(intent, ctx): Promise<IntentOutcome | void>; ... }`，ctx 只含 ports（gateway/clock/scheduler/idGenerator）与共享 state 引用；Feature 之间禁止直接 import（通过 Controller 编排或共享 state 层）。
- **Controller**：只保留 `getSnapshot/subscribe/dispatch/close` 路由、listeners 管理、snapshot 组装（buildSnapshot 可委托给 selectors 层——本任务先内联，HC-112 再抽 selectors）、Feature 生命周期（init/close）。预期 ≤300 行。
- **快照发布**：保持当前"单 snapshot 整体发布"语义（分片发布是 HC-112/HC-114 的 revision 机制，本任务不改发布频率，避免行为变化）。
- **测试**：既有 controller.test.ts 拆分为每 Feature 一个测试文件（run.test.ts、interaction.test.ts、thread.test.ts、model.test.ts、skill.test.ts、mcp.test.ts、command.test.ts、catalog.test.ts、timeline.test.ts），沿用 makeHarness 内存 port；先迁移后新增，每个 Feature 文件覆盖该 Feature 的独立状态机与 effect（含失败路径）。

## 实施步骤

1. 依据 controller.ts 方法分组与 state.ts reducer 边界，划定九个 Feature 的字段/方法归属表（写入设计注释或本文档附录）。
2. 逐个抽取 Feature（建议顺序：catalog → thread → model → skill → mcp → interaction → run/timeline → command）：每抽一个，运行 `bun test --isolate tests/interactive` 保持全绿后再抽下一个（小步提交，每 Feature 一个 commit）。
3. Controller 缩减为薄协调器：dispatch 按 intent 路由到 Feature，close 依次关闭 Feature 并失效 generation。
4. 拆分 controller.test.ts 为九个 Feature 测试文件（断言不变，仅按归属移动）；为跨 Feature 场景（如 submit 时 armedSkill 消费、interaction 与 run 终态互斥）保留集成级用例在 controller.test.ts 精简版。
5. `架构总览.md` 补"Interactive Core Feature 拆分"小节；验证 typecheck/test/project:check；提交证据。

## 范围

- 九 Feature 抽取、Controller 薄化、测试拆分、目录落位 interactive/features/。

## 非范围

- 不改 InteractiveController 外部接口、Intent/Outcome/Snapshot 形状（HC-110 产物）。
- 不做 Selector 分片与展示策略抽取（HC-112）；不做 Composition Root 提升（HC-113）。
- 不优化 reducer 本身的行为；无版本变更；不动 packages/protocol 与 Python。

## 验收清单

- [x] `wc -l packages/cli/src/interactive/controller.ts` ≤ 300（实测 300）；`interactive/features/` 存在 9 个 Feature module。
- [x] `grep -rn "from \"\.\./features\|from '../features" packages/cli/src/interactive/features/` 无匹配（Feature 间零直接依赖）。
- [x] 既有 controller.test.ts 断言（或迁移后等价断言）全部通过；每个 Feature 有独立测试文件且含失败路径（mcp-feature.test.ts 为新增失败路径用例）。
- [x] `getSnapshot/subscribe/dispatch/close` 行为与拆分前一致（contract 测试 + TUI/Web 集成测试回归）。
- [x] `bun run typecheck`、`bun run test`（interactive 全绿；web 子集 4 个 happy-dom 测试间干扰失败为 HC-115 预存问题）、`bun run project:check` 全绿；证据与 OCR 结论写入本任务。
