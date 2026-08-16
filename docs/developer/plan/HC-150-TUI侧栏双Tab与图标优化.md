# HC-150 实施计划：TUI 侧栏双 Tab 与图标优化

## 实施步骤

### 步骤 1：状态与契约扩充（TuiAdapter）
- 在 `packages/cli/src/tui/application/adapter.ts` 中将 `SidebarState` 扩充 `activeTab: SidebarTab`（默认为 `"files"`）。
- 实现 `sidebar-tab-switch` Intent 处理方法，支持指定目标 Tab 或交替切换。

### 步骤 2：侧边栏 Header 与双 Tab 视图分流（Sidebar）
- 在 `packages/cli/src/tui/presentation/sidebar.tsx` 顶部实现双 Tab 胶囊组件（`[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]`）。
- 当 `activeTab === "files"` 时，全高渲染 `FileTreeWidget`；
- 当 `activeTab === "status"` 时，滚动渲染 CWD、Context、MCP、Modified Files。
- 绑定鼠标点击切换事件与快捷键。

### 步骤 3：文件树图标与多色彩精细化（FileTreeWidget）
- 在 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx` 中重构图标与颜色映射函数：
  - 目录：`▾ ◆ `/`▸ ◆ ` 琥珀金（`#e6bb72`）；
  - 代码/文档/配置：`· ` 搭配多色彩区分；
  - 符号链接：`↪ ` 淡紫色。
- 保证字符等宽对齐，杜绝 emoji 宽度抖动。

### 步骤 4：快捷键集成与单测全绿
- 在 `packages/cli/src/tui/app.tsx` 中增加侧边栏获焦时的 Tab 切换按键支持（`[` / `]`、`1` / `2`）。
- 编写与更新单测 `packages/cli/tests/tui/presentation/sidebar.test.ts` 与 `sidebar-file-tree.test.ts`。

---

## 🎯 可演示停点

### 停点 1：侧栏双 Tab 切换与文件树独占全高
> **验证方式**：
> 1. 运行 `bun run dev` 发送消息进入聊天；
> 2. 观察侧边栏顶部为 `[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]` 双 Tab；
> 3. 点击 `[ ⚡ 状态 ]`，观察侧边栏切换为运行环境与状态面板；
> 4. 点击 `[ 📁 文件树 ]`，观察文件树独占全高展示，文件夹呈现暖黄色 `▸ ◆ ` / `▾ ◆ `。
