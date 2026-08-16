# HC-151 规格说明：TUI 主从双栏抽屉与实时预览

## 1. 交互与布局契约

### 1.1 遮罩抽屉架构（Overlay Drawer Architecture）

```text
┌── 终端全屏 (Terminal Screen) ────────────────────────────────────────────────────────┐
│ 主对话时间线 (Thread Timeline - 100% 原始宽度，不变形)                             │
│                                              ┌── 半透明遮罩浮层 (Overlay Drawer) ──┐ │
│                                              │ ┌─── 文件树 ───┐ ┌── 代码实时预览 ─┐│ │
│                                              │ │ 📁 文件树    │ │ 📄 src/index.ts ││ │
│                                              │ │ ▾ 📂 src     │ │  1: import React││ │
│                                              │ │   📄 app.tsx │ │  2: export ...  ││ │
│                                              │ │ › 📄 main.ts │ │  3: console...  ││ │
│                                              │ └──────────────┘ └─────────────────┘│ │
│                                              └─────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **容器属性**：
  - `position="absolute"`，占满全屏 `(top: 0, left: 0, right: 0, bottom: 0)`；
  - 背景色：`RGBA.fromInts(0, 0, 0, 160)`（半透明暗色遮罩）；
  - `alignItems="flex-end"`（全部抽屉停靠在屏幕右侧）。
- **双栏宽度分配**：
  - **左栏（侧栏 / Master）**：固定宽度 `38` 列，展示 Tab 切换与文件树 / 状态小部件；
  - **右栏（预览 / Detail）**：
    - 当激活项为有效文件且终端宽度足以容纳时（`terminalWidth >= 90`），右侧展开代码预览视口，宽度自适应分配（如 `min(terminalWidth - 40, 80)` 列）；
    - 当激活项为目录或窄屏终端时，仅展示左栏（38 列），右栏不展开。

### 1.2 实时联动与加载生命周期（Live Preview Lifecycle）

1. **光标选定即预览**：
   - 当在文件树中选中某行（`file-tree-select` 或 `file-tree-navigate`）时：
     - 若该行是文件（`row.kind === "file" | "symlink"`），立即派发 `workspace.preview.load` 加载文件内容；
     - 若该行是目录（`row.kind === "directory"`），将当前预览状态置为 `null`。
2. **预览面板（CodePreviewPane）视觉规格**：
   - 顶部 Header：展示文件路径、语言类型、大小、总行数与 `[@ 引用路径]` 快捷提示；
   - 正文内容区：使用 OpenTUI 原生 `<line-number>` 与 `<code>` 标签，Tree-sitter 语法高亮渲染；
   - 底部操作栏：提示 `[@] 引用到输入框`、`[↑/↓] 切换文件`、`[Esc] 关闭`。

---

## 2. 状态机与 Intent 变更

1. **`adapter.ts` 变更**：
   - `SidebarState.drawerOpen` 成为侧边栏开闭的唯一布尔状态源；
   - 当选中文件树节点时，自动触发 `openFilePreview(row.path)` 并保存至 `sidebar.preview`；
   - 移除独立的 `FilePreviewModal`，预览组件作为 `Sidebar` 的右侧面板内聚渲染。
2. **快捷键状态转换**：
   - 抽屉打开时：
     - `↑` / `↓`（或 `k` / `j`）：在文件树中移动光标，并实时刷新右侧代码预览；
     - `←` / `→`（或 `h` / `l`）：折叠 / 展开目录；
     - `@`：将当前预览文件的路径（如 `@packages/cli/src/index.ts`）填入输入框，关闭抽屉并交还焦点；
     - `Esc` / `Tab` / 点击遮罩：关闭抽屉并交还焦点。
