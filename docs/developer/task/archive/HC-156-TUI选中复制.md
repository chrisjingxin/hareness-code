---
id: HC-156
title: TUI选中复制
feature_area: TUI表现层
parent_task: -
decomposed_by: Codex
priority: P1
status: 已完成
owner: Codex
branch: feat_hc_156_TUI选中复制
reviewed_at: 2026-08-19
review_due: -
scope: 在 CLI TUI 中提供文本选区复制和即时 Toast 反馈：macOS/Linux 鼠标松开后自动复制；Windows 在有选区时通过 Ctrl+C 或右键复制；复用现有剪贴板与 Toast 链路。
acceptance: 用户能选中 TUI 文本并得到可观察的复制结果和成功/失败反馈；无选区不产生提示；原有 Ctrl+C 语义在无选区时保持不变；相关自动化测试、类型检查和用户文档通过。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-156-TUI选中复制.md、docs/developer/plan/HC-156-TUI选中复制.md、docs/developer/todo/HC-156-TUI选中复制.md、docs/developer/architecture/TUI表现层.md
test_evidence: cd packages/cli && bun test tests/tui/presentation/selection-copy.test.ts tests/tui/app-interaction.test.ts tests/tui/btw-modal.test.ts tests/tui/presentation/sidebar-drawer-preview.test.ts (30 pass)
references: docs/developer/research/156-TUI选中复制.md
completed_at: 2026-08-19
---

# HC-156 TUI 选中复制

## 背景

当前 TUI 已能把 `/btw` 回答写入系统剪贴板，并通过右上角 Toast 反馈结果；但用户无法直接复制时间线、弹窗或其他 TUI 文本的选区。阅读模型输出、工具结果或错误信息时，只能依赖终端自身的行为，且没有 Harness 内的明确反馈。

MiMo Code 的源码调研见 [选中复制调研](../../research/156-TUI选中复制.md)：它在非 Windows 平台鼠标松开时复制非空选区，在 Windows 使用 Ctrl+C / 右键回退，并在复制结果确定后显示 Toast。

## 用户最终得到什么

```text
选中文字
  → 判断平台与是否存在非空选区
  → 调用现有系统剪贴板工具
  → 成功或失败 Toast
  → 清理本次选区，继续原有 TUI 交互
```

- macOS/Linux：在 TUI 任意可选文本上完成鼠标拖选并松开，选区自动复制；复制成功显示“已复制到剪贴板”，失败显示错误 Toast。
- Windows：不在鼠标松开时自动复制；有选区时 Ctrl+C 或右键复制该选区。没有选区时 Ctrl+C 保持当前的清空输入、取消运行或退出语义。
- 没有非空选区时，不调用剪贴板、不显示 Toast，也不改变既有交互。

## 范围

- 在 `packages/cli/` 的 TUI 表现层读取和清理渲染器选区，并将复制结果接入既有 `copyToClipboard()` 与 `TuiAdapter.showToast()`。
- 为自动复制、Windows 快捷键/右键回退、无选区和复制失败补充聚焦测试。
- 更新 `docs/user/交互使用.md`，说明跨平台触发方式与现有 Ctrl+C 的优先级。
- 在完成时增量更新 `docs/developer/architecture/TUI表现层.md`，记录选区复制属于 TUI 表现层而非协议或 Agent 能力。

## 非范围

- 不修改 JSON-RPC Protocol、Python Agent、Thread 持久化或 Timeline 数据形状。
- 不新增剪贴板后端、环境变量或 OSC 52 远程终端支持；继续使用现有跨平台系统剪贴板工具。
- 不做历史通知中心、复制历史或对任意文本增加独立复制按钮。

## 验收

- [x] 在 macOS/Linux，鼠标松开非空 TUI 选区后，选区内容被交给现有剪贴板工具，且仅出现一条对应的成功或失败 Toast。
- [x] 在 Windows，有非空选区时 Ctrl+C 和右键复制选区；无选区时 Ctrl+C 仍按现有规则清空输入、取消运行或退出。
- [x] 空选区不会调用剪贴板工具、不会清空无关状态，也不会产生 Toast。
- [x] 覆盖选区的弹窗/浮层不会因事件冒泡导致重复复制或重复 Toast。
- [x] 相关 Bun 测试、`bun run typecheck` 与更新后的用户文档校验通过。
