---
id: HC-150
title: TUI侧栏双Tab与图标优化
feature_area: CLI/TUI表现层
parent_task: -
decomposed_by: Antigravity
priority: P1
status: 进行中
owner: Antigravity
branch: feat/hc-150-sidebar-tabs
reviewed_at: 2026-08-16
review_due: 2026-08-30
scope: 将 TUI 侧边栏拆分为「文件树」与「运行状态」双 Tab 布局，优化文件夹与文件折叠图标为优雅彩色轻量字符，支持鼠标与快捷键切换 Tab。
acceptance: 侧边栏顶部支持 [ 📁 文件树 ] 与 [ ⚡ 状态 ] 胶囊切换；文件树 Tab 独占全高空间展示文件列表与滚动；状态 Tab 展示 CWD、Context、MCP 与变更文件；文件夹采用 ▾ / ▸ 搭配琥珀金高亮，文件采用色彩区分。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-150-TUI侧栏双Tab与图标优化.md
test_evidence: -
references: docs/developer/task/archive/HC-149-TUI侧边栏与文件树预览.md
completed_at: -
---

# HC-150: TUI 侧栏双 Tab 与图标优化

## 1. 为什么做（Why）

在 `HC-149` 落地后，侧边栏将 CWD、Context、MCP、Modified Files 以及文件树全部纵向堆叠在 40 列的垂直区域内。这种布局在实际使用中存在以下痛点：
1. **纵向空间严重受挤压**：文件树只能分配到屏幕下方的一小块空间，如果项目目录结构较深，文件列表容易被挤压，滚动和浏览体验欠佳。
2. **状态与文件诉求互斥**：开发者查阅文件树时，希望最大化垂直浏览面积；而确认 MCP 连接或 Token 消耗时，希望集中查看运行环境，二者不需要同时挤在同一个视口。
3. **Emoji 占位与视觉对齐问题**：此前使用的 `📁` emoji 在部分终端中宽度不一（1.5~2 单元格），显得笨重且无法按状态着色；需要更轻量、色彩丰富的字符级图标。

## 2. 用户最终得到什么（User Outcome）

1. **侧栏双 Tab 顶部胶囊切换**：
   - 侧边栏顶部清晰提供两个 Tab：`[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]`，默认展示 `文件树`。
   - 激活 Tab 带有醒目的主题色背景高亮与加粗标识。
   - 支持鼠标点击任一 Tab 即刻切换，或在侧边栏获焦时通过快捷键（如 `[` / `]`、`1` / `2`）切页。
2. **文件树 Tab（Files View）**：
   - 文件树独占整栏垂直高度，支持自适应滚动与大范围目录浏览。
   - 折叠/展开指示符采用精细字符 `▾` / `▸`。
   - 文件夹名称与图标统一着暖黄色/琥珀金（`#e6bb72`），代码文件带清爽蓝色/中性色，符号链接带淡紫色，层级分明且优雅。
3. **状态 Tab（Status View）**：
   - 集中整洁地展示工作区路径（CWD）、Context 消耗与 TPS 速度、MCP 服务列表状态指示灯、以及变更文件列表（Modified Files）。

## 3. 范围边界（Scope）

- **包含（In Scope）**：
  - `SidebarState` 增加 `activeTab: "files" | "status"` 状态。
  - `TuiAdapter` 增加 `sidebar-tab-switch` Intent 与快捷键绑定。
  - `sidebar.tsx` 增加顶部 Tab 切换条，并根据当前激活 Tab 分别渲染 `FileTreeWidget` 或四个状态 Widget。
  - `file-tree-widget.tsx` 图标与色彩体系重构（`▾`/`▸` 展开指示 + 暖黄文件夹 + 色彩区分）。
  - 单元测试与 TUI 渲染测试。
- **不包含（Out of Scope）**：
  - 变更跨进程 JSON-RPC 协议（领域状态完全保持单一事实源）。

## 4. 什么算完成（Acceptance Criteria）

1. 侧边栏顶部支持 `[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]` 切换，点击或快捷键流畅切页。
2. 切至「文件树」时，文件树独占侧栏全高并正常支持键盘 `↑`/`↓`/`←`/`→`/`Enter`/`@` 导航。
3. 切至「状态」时，工作目录、Context、MCP 与变更文件清晰排版。
4. 文件夹与文件图标变为精细字符与多色彩区分，字符对齐整齐无抖动。
5. 单元测试 `bun run test:ts` 与类型检查 `bun run typecheck` 全部通过。
