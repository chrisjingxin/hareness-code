# HC-149 执行清单：TUI 侧边栏与文件树预览

## 阶段一：侧边栏基础布局与响应式分栏容器

- [x] 在 `packages/cli/src/tui/application/adapter.ts` 中定义 `SidebarState` 并在初始快照中挂载
- [x] 在 `packages/cli/src/tui/application/shortcuts.ts` 中注册 `Ctrl+B` 与 `F2` 全局切换快捷键
- [x] 创建 `packages/cli/src/tui/presentation/sidebar.tsx` 侧边栏主体容器组件
- [x] 修改 `packages/cli/src/tui/app.tsx` 和 `thread.tsx`，将 Sidebar 接入主布局并根据侧边栏显隐动态计算主视图可用宽度
- [x] 编写测试覆盖宽屏并排渲染与窄屏抽屉模式

### 🎯 可演示停点 1：Sidebar 骨架与响应式切换
> **如何验证**：
> 1. 运行 `bun run dev`；
> 2. 将终端窗口拉宽（>120 列），观察右侧是否展示 40 列的 Sidebar 骨架容器；
> 3. 将终端窗口收窄（<=120 列），观察 Sidebar 是否自动隐藏，按 `Ctrl+B` 观察是否弹出半透明抽屉。

---

## 阶段二：环境与运行态小部件（CWD、Context、MCP、Modified Files）

- [x] 创建 `packages/cli/src/tui/presentation/sidebar/cwd-widget.tsx` 并完成 `$HOME` 简写路径格式化
- [x] 创建 `packages/cli/src/tui/presentation/sidebar/context-widget.tsx` 并完成 Token 统计、窗口利用率及 TPS 展示
- [x] 创建 `packages/cli/src/tui/presentation/sidebar/mcp-widget.tsx` 并完成 MCP 服务状态圆点与错误提示
- [x] 创建 `packages/cli/src/tui/presentation/sidebar/modified-files-widget.tsx` 并完成会话变更文件列表与 diff 行数计算
- [x] 在 `Sidebar` 容器中依次组装这四个小部件并编写单元测试

### 🎯 可演示停点 2：运行态与环境信息就绪
> **如何验证**：
> 1. 运行 `bun run dev`；
> 2. 观察 Sidebar 右侧面板，从上到下是否清晰展示当前工作目录、Context 使用情况、MCP 服务器连接指示灯及已修改文件。

---

## 阶段三：工作区文件树与双模焦点导航

- [x] 复用 `WorkspaceExplorer` 领域契约并与 `TuiAdapter` 订阅联动（支持 `.gitignore` 与 Git 全量/懒加载）
- [x] 在 `TuiAdapter` 中实现文件树展开/折叠、选中项移动及可见行（`visibleRows`）展平计算
- [x] 创建 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx`，支持 `▶`/`▼` 图标、层级缩进与滚动
- [x] 实现 `Tab` 在 InputBar 与文件树之间的焦点切换，实现键盘 `↑`/`↓`/`←`/`→`/`Enter` 导航与鼠标点击展开
- [x] 编写文件树与键盘/鼠标交互测试（`packages/cli/tests/tui/presentation/sidebar-file-tree.test.ts`）

### 🎯 可演示停点 3：文件树交互与按需加载
> **如何验证**：
> 1. 运行 `bun run dev`；
> 2. 观察 Sidebar 底部出现工作区文件树；
> 3. 按 `Tab` 键将焦点切换至文件树，使用方向键 `↑`/`↓` 移动光标，按 `Enter` 展开/折叠目录，验证懒加载是否流畅。

---

## 阶段四：代码快速预览浮层（Quick Look Modal）与路径一键引用

- [x] 创建 `packages/cli/src/tui/presentation/file-preview-modal.tsx` 浮层组件
- [x] 接入带行号的代码渲染与元信息展示（语言、行数、大小），支持滚轮与键盘滚动
- [x] 实现按 `@` 键将文件路径以 `@path/to/file` 自动插入 InputBar 并关闭浮层
- [x] 增加针对二进制与超过 1MB 大文件的安全提示与保护
- [x] 编写预览浮层渲染、滚动与快捷键测试，运行整体类型检查 `bun run typecheck` 与测试 `bun run test`

### 🎯 可演示停点 4：完整功能交付与链路闭环
> **如何验证**：
> 1. 运行 `bun run dev`；
> 2. 在文件树中选中一个代码文件按 `Enter`（或鼠标双击），观察是否弹出高亮代码预览浮层；
> 3. 在浮层中按 `@` 键，观察文件路径是否自动填充至输入框，且焦点自动回到输入框。
