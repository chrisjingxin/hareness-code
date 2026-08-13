---
id: HC-127
title: 收敛 Web Timeline 身份层级与 Tool Activity Group
feature_area: Web UI 工作台体验升级
parent_task: HC-124
decomposed_by: Codex
priority: P1
status: 待认领
owner: 未认领
branch: -
reviewed_at: 2026-08-09
review_due: 2026-08-23
scope: 在不改变 canonical Timeline 数据的前提下，删除重复角色 avatar，统一 User/Assistant/System 阅读边，并把同一 Run 的连续 Tool 纯投影为可展开 Activity Group，降低长会话噪声且保持 running/failed/Interaction 可见。
acceptance: User/Assistant 各只有一种清晰身份表达；短 User surface 随内容收敛；同一 Run 连续 completed Tool 可折叠为数量摘要，running/failed 始终显式；Message/Interaction/不同 Run 正确断组；展开状态稳定、历史顺序与原文不变、异常数据降级逐项渲染；Timeline tests、Markdown/Tool 回归、build、typecheck 通过。
user_docs: docs/user/Web界面.md
developer_docs: docs/developer/spec/HC-124-统筹WebUI工作台体验升级与.md
test_evidence: -
references: docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md、docs/developer/task/HC-125-统一Web工作台视觉token.md、docs/developer/task/HC-126-建立Web工作台三档响应式与外.md
completed_at: -
---

## 背景

Coding Agent 的长 Run 会产生大量 Tool。当前每个 Tool 都占独立卡片并重复成功状态，User/Assistant 又同时显示 author 和单字母 avatar，导致工具活动压过 Agent 结论。

## 当前存在的问题

- `U/H` avatar 与“你/Harness”重复，形成不必要的 37px 缩进轨。
- `THREAD · N 项记录` 只显示技术计数，不帮助理解当前任务。
- completed Tool 每项重复绿色状态；连续操作无法快速折叠扫读。
- 不能为了 UI 分组修改或持久化 canonical Timeline 顺序。

## 目标设计

```text
InteractiveSnapshot.timeline
  → 过滤空 final Assistant / pending live Interaction
  → 按 runId 和相邻边界派生 TimelineRenderItem
  → Message / Tool Activity Group / Interaction
```

分组规则、降级行为和视觉层级以 [HC-124 设计](../spec/HC-124-统筹WebUI工作台体验升级与.md) 为准。

## 实施步骤

1. 建立纯 `projectTimelineForWeb()` 或同职责 presenter，输入 Timeline，输出可序列化 render items。
2. 删除 User/Assistant 单字母 avatar，重新统一 author、正文、Tool 和 Interaction leading edge。
3. 同一 runId 的相邻 Tool 形成 Activity Group；Message/Interaction/不同 Run 断组。
4. completed group 默认摘要；running/failed Tool 保持显式，并允许展开查看全部参数/输出。
5. 保留稳定 key、Tool 原始顺序和局部展开状态；异常 id/status 逐项降级。
6. 增加分组纯函数、React 渲染、ARIA expanded、Tool output 和滚动保持测试。

## 范围

- `web/presentation/timeline.tsx`、相关纯 presenter 与 CSS。
- Timeline/Tool/Interaction presentation tests。

## 非范围

- 不改变 `interactive/state.ts`、Protocol Event 或 Transcript。
- 不新增时间、duration、进度或 Tool 分类业务数据。
- 不实现 virtualization（HC-130 根据测量决定）。
- 不调整 Composer/审批表单。

## 验收清单

- [ ] User/Assistant 无重复 avatar，角色和内容仍可访问地识别。
- [ ] completed Tool Group 可扫读、可展开；running/failed 永不隐藏。
- [ ] 同 Run/跨 Run、Message、Interaction 和异常数据边界测试齐全。
- [ ] canonical Timeline 次序、Tool 内容、复制/展开和 Markdown 不回归。
- [ ] `cd packages/cli && bun test --isolate tests/web/presentation`、build、typecheck 通过。

## 定期复核记录

- 2026-08-09（Codex）：从 HC-124 拆解；等待 HC-125/HC-126 稳定结构，下一次复核 2026-08-23。

