# HC-151 实施计划：TUI 主从双栏抽屉与实时预览

## 实施步骤

### 步骤 1：重构侧边栏容器为统一浮层遮罩抽屉（Sidebar Overlay）
- 修改 `packages/cli/src/tui/presentation/sidebar.tsx`：
  - 不再扣减主界面的宽度，统一使用全屏半透明遮罩 + 右对齐抽屉容器；
  - 内部支持双栏布局（左侧导航/状态抽屉 + 右侧代码预览面板）。

### 步骤 2：实现右侧代码实时预览面板（CodePreviewPane）
- 在 `sidebar.tsx` 内嵌（或独立子组件）代码预览面板；
- 继承 Tree-sitter WebAssembly 语法高亮、`<line-number>`、`<code>` 标签及大文件降级保护；
- 提供文件信息头部与 `@` 快捷引用提示。

### 步骤 3：实现文件树光标联动实时预览（TuiAdapter & Explorer）
- 在 `packages/cli/src/tui/application/adapter.ts` 中：
  - 当选中文件树非目录节点时，自动派发 `openFilePreview(path)` 载入内容；
  - 选定目录时清空预览；
  - 打开抽屉时若默认选中的是文件，立即自动触发预览加载。

### 步骤 4：主界面集成与快捷键收敛（App & Thread）
- 在 `packages/cli/src/tui/app.tsx` 中：
  - 侧边栏始终作为全屏顶层 Overlay 渲染；
  - 移除旧的 `FilePreviewModal` 居中弹窗；
  - 调整 `useKeyboard` 处理逻辑。
- 运行测试与类型检查。

---

## 🎯 可演示停点

### 停点 1：右侧遮罩抽屉与主从双栏实时代码扫视
> **验证方式**：
> 1. 运行 `bun run dev`，主聊天界面始终占满屏幕，无挤压折行；
> 2. 点击右上角 `[◧ 侧栏]` 或按 `Ctrl+B` / `F2` 唤出右侧遮罩抽屉；
> 3. 在文件树中按上下键移动光标，右侧实时展开对应代码文件并带有彩色的语法高亮；
> 4. 按 `@` 键一键引用当前预览文件到输入框并关闭抽屉；
> 5. 按 `Esc` 或点击遮罩直接关闭抽屉。
