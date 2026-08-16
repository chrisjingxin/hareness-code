# HC-151 执行清单：TUI 主从双栏抽屉与实时预览

## 阶段一：抽屉遮罩与主从双栏实时代码预览

- [x] 在 `packages/cli/src/tui/presentation/sidebar.tsx` 中将侧边栏重构为统一的 Overlay 抽屉容器，支持 `[ 左栏 (38列) │ 右栏代码视口 ]` 双栏排版
- [x] 将语法高亮与行号代码预览面板内聚到抽屉右侧（`CodePreviewPane`）
- [x] 在 `packages/cli/src/tui/application/adapter.ts` 中实现文件树节点选中时自动触发 `openFilePreview` 实时联动
- [x] 在 `packages/cli/src/tui/app.tsx` 中挂载全屏遮罩抽屉，移除旧的 `FilePreviewModal` 居中弹窗，收敛快捷键
- [x] 运行 `bun test`、`bun run typecheck` 与 `bun run test:ts` 确保全绿

### 🎯 可演示停点 1：右侧遮罩抽屉与主从双栏实时代码扫视
> **如何验证**：
> 1. 运行 `bun run dev` 发送消息进入聊天；
> 2. 点击 `[◧ 侧栏]` 或按 `Ctrl+B`，右侧滑出抽屉，左侧为暗色半透明遮罩；
> 3. 光标在文件树上下移动，右侧即刻展开并刷新对应的代码语法高亮；
> 4. 按 `@` 将当前文件路径插入输入框并关闭抽屉；按 `Esc` 或点击遮罩直接关闭。
