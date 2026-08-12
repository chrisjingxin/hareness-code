---
id: HC-099-legacy
title: 重组 CLI TUI 工程目录
priority: P0
status: 已完成
owner: Codex
branch: master
scope: 按 Application、Presentation 与 Platform 重组 TUI 源码和测试，拆分混合视图文件并归位 Web launcher。
acceptance: TUI Presentation 不直接调用 IPC，Controller interface、终端快照、CLI 命令和 Web 行为保持不变，类型检查、测试与构建通过。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/斜杠命令体系.md
test_evidence: bun run typecheck; cd packages/cli && bun test (111 tests, 110 passed, 1 skipped); bun run build; TUI architecture test confirms Presentation 无 IPC import and syntax asset generator uses Platform canonical paths
references: 工作区目录重构（未提交）
completed_at: 2026-07-31
---

## 背景

`packages/cli/src/tui/` 同时平铺状态、工作流、React 视图、主题、终端兼容和语法资源。`components.tsx` 还包含首页、会话、时间线、输入框和 Picker 等多类视图。

## 当前存在的问题

- Application 状态和 Presentation 文件只靠命名区分。
- `components.tsx` 的无关视图共同变化，测试归属不清晰。
- Web launcher 位于 CLI src 根目录，未与 Web adapter 归类。

## 为什么现在要修改

新的命令、Picker、Web 表现层和终端适配会继续增加文件。先固定目录 seam，可以防止 React 视图直接获取 IPC client 或业务决策。

## 目标设计

```text
tui/
├─ app.tsx
├─ application/
├─ presentation/
├─ platform/
└─ upstream/
```

`TuiController` 保持状态与工作流的唯一 interface；`app.tsx` 只组合 Controller 和 React/OpenTUI；Presentation 只接收 snapshot、props 和 intent callback。

## 实施步骤

1. 移动 Application、Presentation、Platform 和测试文件。
2. 将 `components.tsx` 按 Home、Thread/Timeline、Composer 与 Picker 拆分。
3. 将 Web launcher 迁入 `web/` 并更新 CLI 入口。
4. 增加 Presentation 不导入 IPC 的结构测试。
5. 运行类型检查、CLI 测试、快照与构建。

## 范围

- CLI 内部路径、视图拆分和测试归属。

## 非范围

- 不改变 Controller interface、TUI 文案、视觉、快捷键或协议。
- 不因行数拆散 `TuiController`。

## 验收清单

- [x] TUI 根目录只保留组合入口和职责目录。
- [x] Presentation 不直接依赖 IPC。
- [x] 终端快照无行为差异。
- [x] TypeScript 检查、测试与构建通过。
- [x] 无版本变更。

## 版本影响

无版本变更。此次迁移只调整 CLI 内部目录、视图文件归属和 Web launcher 路径，Controller interface、终端交互与协议保持不变。
