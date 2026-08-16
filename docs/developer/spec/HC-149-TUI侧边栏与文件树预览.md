# HC-149 规格说明：TUI 侧边栏与文件树预览

## 1. 概述与设计理念

本规格定义 Harness Code 终端界面（TUI）右侧侧边栏（Sidebar）与代码文件快速预览浮层（Quick Look Modal）的交互行为、状态模型、组件接口与异常语义。

### 通俗业务流程

```
[终端输入 / 尺寸事件 / 运行时事件]
               │
               ▼
   [TuiAdapter / 表现状态层]
      ├─ 判定终端宽度 (width > 120 ? wide : narrow)
      ├─ 计算 Sidebar 显示模式 (并排列 / 半透明抽屉 / 隐藏)
      ├─ 收集 CWD, Context (Token/TPS), MCP 状态, Modified Files
      └─ 管理文件树节点展开状态与选中项
               │
               ▼
      [OpenTUI 渲染层]
      ├─ 宽屏：右侧渲染 Sidebar (宽 40 列)，主区域 flexGrow 填充
      ├─ 窄屏：若用户开启，则渲染全屏半透明遮罩 + 右侧抽屉
      └─ 按需弹出：居中代码高亮 Quick Look 预览浮层
```

---

## 2. 状态模型与 Interface 设计

### 2.1 侧边栏状态（Sidebar State）

在 `packages/cli/src/tui/application/adapter.ts` 的 `TuiAdapterSnapshot` 中扩展：

```ts
export type SidebarState = {
  /** 侧边栏整体开关模式 */
  mode: "auto" | "show" | "hide"
  /** 窄屏下手动呼出的抽屉是否处于打开状态 */
  drawerOpen: boolean
  /** 当前键盘焦点是否位于 Sidebar */
  focus: "chat" | "sidebar"
  /** 工作区文件树状态 */
  fileTree: FileTreeState
  /** 快速预览浮层状态 */
  preview: FilePreviewState | null
}

export type FileTreeNode = {
  path: string
  name: string
  isDir: boolean
  depth: number
  expanded?: boolean
  loading?: boolean
  children?: FileTreeNode[]
}

export type FileTreeState = {
  rootPath: string
  rootNodes: FileTreeNode[]
  selectedIndex: number
  /** 展平后用于虚拟滚动和键盘导航的一维可见节点列表 */
  visibleRows: VisibleTreeRow[]
}

export type VisibleTreeRow = {
  node: FileTreeNode
  depth: number
  isDir: boolean
  expanded: boolean
  path: string
  name: string
}

export type FilePreviewState = {
  filePath: string
  relativePath: string
  content: string
  language?: string
  scrollOffset: number
  totalLines: number
  isBinary?: boolean
  tooLarge?: boolean
}
```

### 2.2 意图分派（TuiIntent 扩展）

```ts
export type TuiIntent =
  // 现有 intents...
  | { type: "sidebar-toggle" }
  | { type: "sidebar-focus-switch" }
  | { type: "file-tree-select"; index: number }
  | { type: "file-tree-toggle-expand"; path: string }
  | { type: "file-tree-navigate"; direction: "up" | "down" | "parent" | "child" }
  | { type: "file-tree-preview"; path: string }
  | { type: "file-preview-close" }
  | { type: "file-preview-scroll"; delta: number }
  | { type: "file-preview-insert-ref"; path: string }
```

---

## 3. 响应式布局与渲染规格

### 3.1 宽度分配与断点规则
- **断点常量**：`SIDEBAR_MIN_TERMINAL_WIDTH = 120`，`SIDEBAR_COLUMN_WIDTH = 40`。
- **并排模式（`terminalWidth > 120` 且 `mode !== "hide"`）**：
  - Sidebar 渲染在主视图右侧：`<box width={40} height="100%" ...>`。
  - 主视图 `ConversationTimeline` 宽度自动为 `terminalWidth - 40`，文字根据此宽度自动计算换行。
- **抽屉模式（`terminalWidth <= 120` 且 `drawerOpen === true`）**：
  - 渲染绝对定位浮层：`<box position="absolute" top={0} left={0} right={0} bottom={0} alignItems="flex-end" backgroundColor={RGBA(0, 0, 0, 0.7)}>`。
  - 抽屉宽度固定为 40 列，高度 100%。

### 3.2 侧边栏内部内容排版（从上到下）

1. **Header / CWD 区域**：
   - 标题：`📁 工作目录`
   - 路径：格式化后的相对/家目录简写路径（如 `~/Code/harness-code`）。
2. **Context & Runtime 区域**：
   - 标题：`⚡ Context`
   - 进度条/百分比：`[====>     ] 42% (54,230 / 128,000 tokens)`
   - 实时指标：`生成速率: 68.5 tps · 累计消耗: $0.12`
3. **MCP 服务状态区域**：
   - 标题：`🔌 MCP (3 活跃)`
   - 列表项：`🟢 filesystem`、`🟢 git`、`🔴 memory (连接超时)`
   - 超过 3 个时支持折叠。
4. **Modified Files 变更文件区域**：
   - 标题：`📝 变更文件 (2)`
   - 列表项：`packages/cli/src/tui/app.tsx  +42 -8`
5. **Workspace Files 文件树区域（占满剩余高度）**：
   - 标题：`🌲 工作区文件 (按 Tab 切换聚焦)`
   - 树形结构：`<scrollbox flexGrow={1}>` 支持键盘上下与鼠标点击展开。

---

## 4. 文件树与快速预览交互规格

### 4.1 文件树遍历与懒加载（Lazy Traversal）
- **根目录初始化**：在进入 TUI 或初始化时，异步读取 `workspaceRoot` 第一层文件与目录，按名称字典序排序（目录排在前面）。
- **Gitignore 过滤**：自动忽略 `.git`、`node_modules`、`dist`、`.venv`、`__pycache__` 等常见衍生目录。
- **按需展开**：点击/回车展开未加载的子目录时，设置 `loading: true` 并异步 `fs.readdir`，读取完成后更新节点并重新计算 `visibleRows`。

### 4.2 焦点与快捷键映射表

| 场景 | 快捷键 | 动作行为 |
| :--- | :--- | :--- |
| **全局** | `Ctrl+B` / `F2` | 在「InputBar 输入框」与「Sidebar 文件树」之间切换焦点；若 Sidebar 未打开则打开并聚焦。 |
| **文件树聚焦** | `↑` / `↓` | 在可见行中移动高亮光标 (`selectedIndex`)。 |
| **文件树聚焦** | `→` / `Enter`（在文件夹上） | 若文件夹已收起则展开；若未加载则触发异步懒加载。 |
| **文件树聚焦** | `←` / `Enter`（在已展开文件夹上） | 折叠当前文件夹。 |
| **文件树聚焦** | `Enter` / `Space`（在文件上） | 读取文件文本并打开 Quick Look 预览浮层。 |
| **文件树聚焦** | `@`（在文件上） | 将文件相对路径以 `@path/to/file` 插入到 InputBar，并将焦点切回 InputBar。 |
| **文件树聚焦** | `Esc` / `Tab` | 焦点返还 InputBar。 |
| **预览浮层开启** | `↑` / `↓` | 上下滚动 1 行。 |
| **预览浮层开启** | `PageUp` / `PageDown` | 上下翻页（半屏高度）。 |
| **预览浮层开启** | `@` | 插入当前预览文件路径到输入框并关闭浮层。 |
| **预览浮层开启** | `q` / `Esc` | 关闭预览浮层。 |

---

## 5. 关键不变性（Invariants）

1. **单向数据流**：所有组件只消费 `TuiAdapterSnapshot`，用户按键及鼠标事件只派发 `TuiIntent`，禁止组件内部跨层篡改状态。
2. **非阻塞与异步保护**：文件读写和目录扫描均为异步操作，扫描失败或超时不得阻断 TUI 主渲染循环。
3. **输入框安全性**：当焦点处于 InputBar 时，输入任何常规按键（包括 `@`、方向键、字母）均优先用于文本输入与历史导航，绝不误触文件树操作。
4. **大文件与二进制保护**：文件体积 > 1MB 或包含 `\0` 字节的二进制文件，在预览浮层中展示提示信息，不进行文本反序列化与高亮解析。
