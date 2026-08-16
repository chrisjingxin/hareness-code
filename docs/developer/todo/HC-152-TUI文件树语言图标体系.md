# HC-152 执行清单：TUI 文件树语言图标体系

## 阶段一：语言图标与专色映射引擎

- [x] 创建 `packages/cli/src/tui/presentation/sidebar/file-icons.ts` 映射引擎，支持特殊文件名、主流扩展名与文件夹状态
- [x] 在 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx` 中接入 `getFileIconInfo` 渲染行图标与色彩
- [x] 在 `packages/cli/src/tui/presentation/sidebar.tsx` 中为 `CodePreviewPane` 头部应用对应的语言图标与色彩
- [x] 编写 `packages/cli/tests/tui/presentation/file-icons.test.ts` 覆盖全面测试
- [x] 运行 `bun test`、`bun run typecheck` 与 `bun run test:ts` 确保全绿

### 🎯 可演示停点 1：文件树专色语言图标呈现
> **如何验证**：
> 1. 运行 `bun run dev`，打开侧边栏；
> 2. 观察文件树中：Python 显示专属 Python 蓝黄图标，TS/JS 显示 TS/JS 图标，`.json`、`Dockerfile`、`.gitignore`、`README.md` 各自呈现专属图标与品牌色彩；
> 3. 展开/折叠目录时，文件夹图标在展开态与收起态之间优雅切换。
