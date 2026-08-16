# HC-152 实施计划：TUI 文件树语言图标体系

## 实施步骤

### 步骤 1：新建语言图标与色彩解析模块（file-icons.ts）
- 创建 `packages/cli/src/tui/presentation/sidebar/file-icons.ts`；
- 构建 60+ 种主流编程语言、特殊配置文件与文件夹状态的图标/色彩映射字典；
- 导出 `getFileIconInfo(name, kind, expanded)` 纯函数。

### 步骤 2：在文件树与代码预览面板中接入图标体系
- 修改 `packages/cli/src/tui/presentation/sidebar/file-tree-widget.tsx`：使用 `getFileIconInfo` 渲染行前缀与颜色；
- 修改 `packages/cli/src/tui/presentation/sidebar.tsx`：在 `CodePreviewPane` 的文件头部也使用对应的语言图标与颜色。

### 步骤 3：编写单测与工程一致性检查
- 编写 `packages/cli/tests/tui/presentation/file-icons.test.ts` 覆盖各语言扩展名、特殊文件名、文件夹与兜底逻辑；
- 运行 `bun run typecheck` 与 `bun run test:ts`。

---

## 🎯 可演示停点

### 停点 1：文件树专色语言图标呈现
> **验证方式**：
> 1. 运行 `bun run dev`，打开侧边栏；
> 2. 观察文件树中：Python 显示专属 Python 蓝黄图标，TS/JS 显示 TS/JS 图标，`.json`、`Dockerfile`、`.gitignore`、`README.md` 各自呈现专属图标与品牌色彩；
> 3. 展开/折叠目录时，文件夹图标在展开态与收起态之间优雅切换。
