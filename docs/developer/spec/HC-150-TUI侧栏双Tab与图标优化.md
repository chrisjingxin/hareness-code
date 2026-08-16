# HC-150 规格说明：TUI 侧栏双 Tab 与图标优化

## 1. 契约与状态定义

### 1.1 `SidebarState` 契约扩充

在 `packages/cli/src/tui/application/adapter.ts` 中扩充 `SidebarTab` 与 `SidebarState`：

```ts
export type SidebarTab = "files" | "status"

export type SidebarState = {
  readonly mode: "auto" | "show" | "hide"
  readonly drawerOpen: boolean
  readonly focus: "chat" | "sidebar"
  readonly activeTab: SidebarTab
  readonly fileTree: SidebarFileTreeState
  readonly preview: WorkspacePreviewState | null
}
```

### 1.2 `TuiIntent` 契约扩充

```ts
export type TuiIntent =
  // 现有 intents...
  | { type: "sidebar-tab-switch"; tab?: SidebarTab }
```

- 当 `tab` 显式提供时，直接切换至目标 Tab；
- 当 `tab` 为空时，在 `"files"` 和 `"status"` 之间交替切换。

---

## 2. 交互与布局规格

### 2.1 侧边栏 Header 结构

侧边栏顶部由左侧双 Tab 胶囊与右侧关闭按钮组成：

```text
┌──────────────────────────────────────┐
│ [📁 文件树]  [⚡ 运行状态]    [✕ 关闭] │
├──────────────────────────────────────┤
│ ... 内容区域 ...                      │
```

- **激活 Tab**：背景色为 `tuiTheme.surfaceElevated`，前景色为 `tuiTheme.primary`（或 Mode Accent），加粗显示；
- **非激活 Tab**：背景透明，前景色为 `tuiTheme.muted`；
- **鼠标支持**：点击对应 Tab 胶囊触发 `sidebar-tab-switch`；
- **键盘快捷键**：
  - 当侧边栏获焦时，按 `[` / `]`、`1` / `2` 可快速切换 Tab。

### 2.2 视图分流

1. **`activeTab === "files"`（默认）**：
   - 渲染 `FileTreeWidget`，`flexGrow={1}` 占据除顶部 Tab 栏以外的 100% 垂直高度；
   - 内部包含工作区文件行数元信息、带层级缩进的文件树、支持键盘光标上下导航与快速预览。
2. **`activeTab === "status"`**：
   - 依次垂直渲染 `CwdWidget`、`ContextWidget`、`McpWidget`、`ModifiedFilesWidget`；
   - `<scrollbox flexGrow={1}>` 确保小屏终端下状态小部件内容过多时平滑滚动。

### 2.3 文件树图标与色彩规格

- **缩进**：每层深度缩进 2 空格；
- **折叠指示**：
  - 收起目录：`▸ `
  - 展开目录：`▾ `
  - 加载中目录：`… `
  - 文件：`  `（2 个空格占位保持对齐）
- **图标与色彩**：
  - 目录：前缀图标 `◆`，前景色统一采用琥珀暖黄色（`#e6bb72` / `syntaxNumber`），加粗显示；
  - 代码文件（`.ts`、`.tsx`、`.js`、`.py`、`.rs`、`.go` 等）：前缀 `· `，前景色采用亮文本色或淡青色（`#7bd4d0`）；
  - 配置文件/文档（`.json`、`.yaml`、`.md` 等）：前缀 `· `，前景色采用中性柔和文本色（`tuiTheme.text`）；
  - 符号链接：前缀 `↪ `，前景色采用淡紫色（`#c4a7f2`）。
- **选中态高亮**：
  - 保持选中行背景 `tuiTheme.surfaceElevated`，选中前缀指示符 `› `，获焦时使用 `tuiTheme.primary`。

---

## 3. 不变性约束

1. 默认 Tab 为 `"files"`，确保进入聊天界面即刻直观查看代码工程结构。
2. 切换 Tab 不重置文件树的展开/折叠状态与光标选中项位置。
3. 键盘与鼠标双模操作等价，不产生冗余 RPC。
