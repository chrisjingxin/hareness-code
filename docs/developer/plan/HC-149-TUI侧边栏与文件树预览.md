# HC-149 实施计划：TUI 侧边栏与文件树预览

## 1. 概述与设计原则

本实施计划将 HC-149 拆解为 4 个垂直递进、依赖有序的步骤。每个步骤均产出可独立编译、测试与观察的阶段性成果，并在关键位置设立**可演示停点**。

---

## 2. 实施步骤与停点规划

### 步骤 1：侧边栏基础布局与响应式分栏容器
- **修改内容**：
  - 在 `packages/cli/src/tui/application/adapter.ts` 中引入 `SidebarState` 及 `sidebar-toggle` 等基础 intents。
  - 在 `packages/cli/src/tui/presentation/sidebar.tsx` 中创建 Sidebar 容器组件，支持宽屏并排渲染与窄屏半透明抽屉模式。
  - 在 `packages/cli/src/tui/app.tsx` 中将 Sidebar 接入主布局树，并调整主时间线宽度计算。
  - 在快捷键解析器 `shortcuts.ts` 中注册 `Ctrl+B` / `F2` 全局开关。
- **验证方式**：
  - 编写 focused tests 验证断点与 intent 派发。
- 🎯 **可演示停点 1（Sidebar 骨架与响应式切换）**：
  - 运行 `bun run dev`，在终端宽度 > 120 时右侧出现 40 列的 Sidebar 骨架；缩小终端后自动收起；按 `Ctrl+B` 能唤出半透明抽屉。

---

### 步骤 2：环境与运行态小部件（CWD、Context、MCP、Modified Files）
- **修改内容**：
  - 实现 `packages/cli/src/tui/presentation/sidebar/cwd-widget.tsx`：格式化展示工作目录。
  - 实现 `packages/cli/src/tui/presentation/sidebar/context-widget.tsx`：计算并展示 Token 使用量、窗口百分比、实时 TPS。
  - 实现 `packages/cli/src/tui/presentation/sidebar/mcp-widget.tsx`：查询并展示 MCP 连接状态指示灯。
  - 实现 `packages/cli/src/tui/presentation/sidebar/modified-files-widget.tsx`：展示当前会话发生修改的文件列表与 diff 行数增删。
- **验证方式**：
  - 编写小部件渲染单元测试，覆盖不同状态（正常、连接失败、无变更等）。
- 🎯 **可演示停点 2（运行态与环境信息就绪）**：
  - 运行 `bun run dev`，Sidebar 内部按顺序展示出真实的 CWD、Context 消耗指示、MCP 服务状态以及变更文件。

---

### 步骤 3：工作区文件树与双模焦点导航
- **修改内容**：
  - 实现本地文件扫描与懒加载工具 `packages/cli/src/tui/platform/file-tree-scanner.ts`，自动加载 `.gitignore` 过滤无关目录。
  - 实现 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx` 树形组件，支持 `▶`/`▼` 展开折叠与状态展示。
  - 在 `TuiAdapter` 中实现文件树的展开收起、行展平（visibleRows）与选中索引状态管理。
  - 在 `shortcuts.ts` 和 `app.tsx` 中实现 `Tab` / `Ctrl+B` 焦点切换与 `↑`/`↓`/`←`/`→` 键盘导航、鼠标点击展开。
- **验证方式**：
  - 编写文件树扫描与键盘导航测试。
- 🎯 **可演示停点 3（文件树交互与按需加载）**：
  - 运行 `bun run dev`，在侧边栏可浏览工作区文件树；按 `Tab` 聚焦文件树后，使用上下左右方向键可顺畅展开/折叠目录，鼠标点击同样生效。

---

### 步骤 4：代码快速预览浮层（Quick Look Modal）与路径一键引用
- **修改内容**：
  - 实现 `packages/cli/src/tui/presentation/file-preview-modal.tsx` 浮层组件。
  - 集成现有的 Tree-sitter / OpenTUI 语法解析器，提供代码高亮与行号显示。
  - 支持 `↑`/`↓`/`PgUp`/`PgDn` 滚动查看；在文件上按 `@` 键一键将文件相对路径注入 InputBar 并关闭预览。
  - 增加大文件（>1MB）与二进制文件保护。
- **验证方式**：
  - 编写预览浮层渲染、滚动、`@` 引用注入测试。
- 🎯 **可演示停点 4（完整功能交付：快速预览与链路闭环）**：
  - 运行 `bun run dev`，在文件树中选中某个代码文件按 `Enter`，全屏弹出高亮代码预览浮层；按 `@` 键后路径自动填入聊天输入框并切回输入模式。

---

## 3. 风险与缓解对策

| 潜在风险 | 影响程度 | 缓解对策 |
| :--- | :--- | :--- |
| 大型工程（数万文件）遍历导致 TUI 掉帧 | 高 | 采用严格的**第一层懒加载**策略，只有用户主动展开子目录时才异步读取；结合 `.gitignore` 过滤 `node_modules` 等无关目录。 |
| 键盘焦点在 InputBar 与文件树之间打架 | 中 | 严格划分 Focus 模式。在 `chat` 模式下，所有字符与光标键 100% 留给 InputBar；只有在 `sidebar` 模式下才激活树导航按键。 |
| 窄屏下半透明抽屉遮挡主会话无法阅读 | 低 | 抽屉模式下按 `Esc` 或任意非抽屉区域点击即可快速关闭。 |
