# HC-150 执行清单：TUI 侧栏双 Tab 与图标优化

## 阶段一：侧栏双 Tab 状态与视图分流

- [x] 在 `packages/cli/src/tui/application/adapter.ts` 中定义 `SidebarTab` 并在 `SidebarState` 增加 `activeTab: "files" | "status"`
- [x] 在 `adapter.ts` 中实现 `sidebar-tab-switch` Intent
- [x] 修改 `packages/cli/src/tui/presentation/sidebar.tsx`：顶部渲染 `[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]` 胶囊，根据 `activeTab` 分别展示全高文件树或状态小部件
- [x] 修改 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx`：重构图标体系为 `▾`/`▸` 搭配琥珀暖黄文件夹与色彩区分文件
- [x] 在 `packages/cli/src/tui/app.tsx` 中增加 Tab 切换按键交互（`[` / `]`、`1` / `2`）
- [x] 编写测试覆盖 Tab 切换与彩色文件树渲染，运行 `bun run typecheck` 与 `bun run test:ts`

### 🎯 可演示停点 1：侧栏双 Tab 切换与彩色精简文件树
> **如何验证**：
> 1. 运行 `bun run dev` 发送消息进入聊天；
> 2. 观察侧边栏顶部为 `[ 📁 文件树 ]` 与 `[ ⚡ 状态 ]` 双 Tab；
> 3. 点击 `[ ⚡ 状态 ]`，观察侧边栏平滑切换为运行环境状态面板；
> 4. 点击 `[ 📁 文件树 ]`，观察文件树独占整栏全高，文件夹呈现精致醒目的暖黄色 `▸ ◆ ` / `▾ ◆ ` 图标。
