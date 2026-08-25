---
id: HC-128
title: 强化 Web Run、Composer、空态与 Interaction 决策流
feature_area: Web UI 工作台体验升级
parent_task: HC-124
decomposed_by: Codex
priority: P1
status: 待认领
owner: 未认领
branch: -
reviewed_at: 2026-08-24
review_due: 2026-09-07
scope: 让当前 Run 状态和下一步可执行动作成为工作台核心层级，统一 Topbar/Composer 状态、空 Thread quick prompt、active Run 草稿与取消、pending approval/question 的信息和决策顺序，同时保持既有业务 intent 与安全门禁。
acceptance: Topbar 与 Composer 上方分别表达全局 Run 和当前动作且不编造进度；active Run 可继续编辑草稿但不能重复提交，取消始终可达；三个 quick prompt 只填充 draft 不自动运行；pending Interaction 不在 Timeline 重复，主/次决策层级、焦点和失败保留正确；IME、Slash、readonly/leaving/connection error 回归通过。
user_docs: docs/user/交互使用.md、docs/user/Web界面.md
developer_docs: docs/developer/spec/HC-124-统筹WebUI工作台体验升级与.md
test_evidence: -
references: docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md、docs/developer/task/HC-107-修复WebComposer、补.md、docs/developer/task/HC-125-统一Web工作台视觉token.md、docs/developer/task/HC-126-建立Web工作台三档响应式与外.md
completed_at: -
---

## 背景

当前页面有 activity chip、Timeline 底部 run status、Composer cancel 和 Interaction dock，但信息分散。用户在长 Run 中需要最快确认“Agent 在做什么”和“我现在能取消、审批还是继续准备输入”。

## 当前存在的问题

- Run 状态分散且视觉权重低，completed Tool 反而更醒目。
- active Run placeholder 同时承担状态和操作说明，扫描效率不足。
- 空 Thread 只有解释文字，没有安全的起步入口。
- Interaction 的内容和按钮需要统一主次层级、焦点进入与处理后返回。

## 目标设计

```text
activity / activeRun / interaction / connection
  → Topbar：全局状态
  → Composer status rail：当前可执行动作
  → Interaction：需要用户决定时占据 status rail 上方
  → Composer：始终保存草稿和下一步输入
```

所有文案只能来自现有 snapshot 可证明的状态；没有 duration/total/risk 数据就不显示。

## 实施步骤

1. 统一 Topbar activity 和 Composer status presenter，去掉重复、冲突或模糊文案。
2. active Run 时 textarea 继续可编辑下一条草稿，Submit 明确不可用，Send 位稳定切换 Cancel。
3. 空 Thread 增加三个 quick prompt；Adapter 的 `quick-prompt-select` 只写 draft 并请求 Composer focus。
4. 收敛 approval/question 的标题、预览、反馈和 action 顺序；同一上下文只有一个 filled primary。
5. pending Interaction 只渲染在 live dock；终态历史继续进入 Timeline。
6. 补齐 submitting、失败保留、readonly、leaving、断连、IME、Slash 和 focus restore 测试。

## 范围

- `web-app.tsx`、`composer.tsx`、`interaction-form.tsx`、Timeline 空态和 Adapter 表现 intent。
- 对应 Web adapter/presentation tests 和用户文档。

## 非范围

- 不新增附件、计划视图、消息队列或并行 Run。
- 不自动提交 quick prompt。
- 不改变 approval policy、Interaction schema 或 Controller 门禁。
- 不实现动效和性能 windowing。

## 验收清单

- [ ] Run 全局状态和当前动作不冲突，取消在所有目标布局可达。
- [ ] active Run 下草稿可继续编辑并稳定保留，不会重复提交。
- [ ] quick prompt 只填 draft，连接不可用时禁用并说明。
- [ ] pending Interaction 单实例、主次动作、焦点和失败反馈正确。
- [ ] IME、Enter/Shift+Enter、Slash、readonly/leaving/断连测试通过。
- [ ] focused tests、build、typecheck 通过。

## 定期复核记录

- 2026-08-09（Codex）：从 HC-124 拆解；保留 HC-107 已建立的 Composer 单一 owner 与语法能力，下一次复核 2026-08-23。

