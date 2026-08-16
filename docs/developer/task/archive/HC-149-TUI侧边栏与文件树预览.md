---
id: HC-149
title: TUI侧边栏与文件树预览
feature_area: CLI/TUI表现层
parent_task: -
decomposed_by: 历史未记录
priority: P1
status: 已完成
owner: Antigravity
branch: feat/hc-149-tui-sidebar
reviewed_at: 2026-08-16
review_due: -
scope: 在 TUI 右侧增加响应式侧边栏（包含 CWD、Context、MCP 状态、Modified Files 与工作区文件树），并支持文件语法高亮快速预览浮层与键盘/鼠标交互。
acceptance: 宽屏（>120列）自适应展示侧边栏并动态分配宽度，窄屏支持快捷键呼出半透明抽屉；支持展示 CWD、Token/TPS、MCP 与变更文件列表；支持文件树按需展开，支持 Enter 呼出代码快速预览浮层与 @ 键引用路径。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-149-TUI侧边栏与文件树预览.md
test_evidence: bun run typecheck && bun run test:ts (724 pass) && bun run project:check
references: docs/developer/research/148-TUI右侧侧边栏调研.md
completed_at: 2026-08-16
---

# HC-149: TUI 侧边栏与文件树预览

## 1. 为什么做（Why）

目前 Harness Code 在终端中采用单列全宽流式布局，主区域集中在会话时间线（Timeline）与底部交互栏（InputBar / Dock）。但在实际编码过程中，开发者需要频繁确认以下信息与进行轻量操作：
1. **运行环境与消耗感知**：当前工作目录（CWD）、模型 Context/Token 窗口利用率、流式生成 TPS、MCP 服务连接状态等。
2. **工作区结构与代码查阅**：想要快速看一眼项目目录树或预览某个文件的内容，目前必须退出 TUI 或依赖命令行工具，打断了工作流。
3. **变更感知**：需要快速了解当前会话已修改了哪些文件及 Diff 增删行数。

通过引入响应式右侧 Sidebar 及内置文件树与快速预览浮层，能大幅提升终端编码与观察效率。

## 2. 用户最终得到什么（User Outcome）

1. **响应式侧边栏（Sidebar）**：
   - **宽屏模式（终端宽度 > 120 列）**：侧边栏默认在右侧常驻分栏展示（固定 40 列），主会话区自适应占据剩余宽度。
   - **窄屏模式（终端宽度 ≤ 120 列）**：侧边栏默认隐藏，用户可按快捷键（`Ctrl+B` 或 `F2`）唤出全屏半透明遮罩的右侧抽屉，再次按快捷键或 `Esc` 收起。
   - **内容板块**：
     - **CWD**：展示当前工作目录路径（`$HOME` 简写为 `~`）。
     - **Context**：展示 Token 消耗量、窗口占用百分比、实时 TPS 与累计花费。
     - **MCP**：展示 MCP 服务器列表与连接状态指示灯（🟢已连接 / 🔴失败 / 🟡等待）。
     - **Modified Files**：展示当前会话中发生修改的文件及 `+N` / `-M` 行数变化。
     - **Workspace Files**：展示工作区文件树，支持按需展开/折叠。
2. **文件快速预览（Quick Look Modal）**：
   - 在文件树中选中文件并按 `Enter`（或鼠标点击），弹出居中代码预览浮层。
   - 浮层使用现有 Tree-sitter 语法高亮解析器展示代码，支持 `↑`/`↓`/`PgUp`/`PgDn` 滚动。
   - 按 `@` 键可一键将该文件相对路径填入聊天输入框，按 `q` 或 `Esc` 退出预览。
3. **双模焦点与无缝切换**：
   - 按 `Ctrl+B` 或 `F2` 可在「底部聊天输入框」与「侧边栏文件树」之间快速切换焦点，支持 `Tab` 循环切换。
   - OpenTUI 原生鼠标支持：直接点击目录折叠/展开，点击文件弹出预览。

## 3. 范围边界（Scope）

- **包含（In Scope）**：
  - CLI 表现层增加侧边栏布局容器与响应式断点控制。
  - CWD、Context/TPS、MCP 状态、Modified Files 四大状态小部件。
  - 本地轻量文件树组件（基于 `.gitignore` 过滤，目录懒加载）。
  - 语法高亮文件预览浮层（Quick Look Modal）及 `@` 快捷键引用能力。
  - 键盘焦点切换（`Ctrl+B` / `F2` / `Tab`）与鼠标点击支持。
  - 单元测试与 TUI 渲染测试。
- **不包含（Out of Scope）**：
  - 在 TUI 内对文件进行内联编辑（编辑仍由 Agent 工具或外部编辑器完成）。
  - 跨进程 JSON-RPC 协议改动（所有状态均复用现有协议事件与 CLI 本地文件系统）。

## 4. 什么算完成（Acceptance Criteria）

1. **终端自适应**：宽度 > 120 时并排渲染且 Timeline 宽度正确扣减，宽度 ≤ 120 时默认收起，快捷键可呼出半透明抽屉。
2. **状态展示准确**：CWD 路径简化正确，Context 与 MCP 状态与实际运行时一致，Modified Files 正确统计 Diff。
3. **文件树性能良好**：展开目录时异步懒加载，自动忽略 `.gitignore` 目录，无卡顿。
4. **预览浮层可用**：支持语法高亮预览文件，支持上下滚动翻页，支持一键 `@` 引用至 InputBar。
5. **测试全绿**：新增组件有完整的 focused tests，`bun run typecheck` 和 `bun run test` 均通过。
