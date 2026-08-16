---
id: HC-151
title: TUI主从双栏抽屉与实时预览
feature_area: CLI/TUI表现层
parent_task: -
decomposed_by: Antigravity
priority: P1
status: 进行中
owner: Antigravity
branch: feat/hc-151-drawer-preview
reviewed_at: 2026-08-16
review_due: 2026-08-30
scope: 将 TUI 侧边栏升级为非侵入式右侧浮层遮罩抽屉，实现「左侧文件树 + 右侧代码实时预览」的一体化主从双栏联动浏览，移除居中弹框。
acceptance: 打开侧边栏不再改变主聊天视口宽度；文件树光标上下移动时右侧代码视口实时同步高亮刷新；支持按 @ 引用路径、按 Esc / 点击遮罩平滑关闭；小屏自适应降级。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-151-TUI主从双栏抽屉与实时预览.md
test_evidence: -
references: docs/developer/task/HC-150-TUI侧栏双Tab与图标优化.md
completed_at: -
---

# HC-151: TUI 主从双栏抽屉与实时预览

## 1. 为什么做（Why）

目前 TUI 侧边栏与文件预览存在两个交互痛点：
1. **主屏排版挤压**：侧边栏在宽屏下常驻并排，会挤压主聊天区域的排版宽度（扣减 40 列），导致原本漂亮的对话时间线被动折行。
2. **弹框割裂体验**：查看代码时在屏幕正中央弹出 Modal 浮层，彻底遮挡了背后的文件树。开发者想要查看多个文件时，必须反复“回车弹窗 -> 看代码 -> Esc关闭 -> 移动光标 -> 再次回车弹窗”，操作极其繁琐。

## 2. 用户最终得到什么（User Outcome）

1. **非侵入式半透明遮罩抽屉（Overlay Drawer）**：
   - 无论宽屏窄屏，侧边栏均以悬浮抽屉形态从右侧滑出，主聊天界面宽度保持 100% 稳定，绝不发生内容抖动或换行挤压。
   - 点击左侧半透明遮罩或按 `Esc` 即可平滑收起。
2. **「文件树 + 代码视口」一体化主从双栏（Master-Detail Dual Pane）**：
   - **左侧 Master 栏（36 列）**：展示工作区文件树（或运行状态 Tab）；
   - **右侧 Detail 栏（自适应宽 60~90 列）**：展开展示当前选中的代码文件完整内容；
   - 顶部提供语言、文件大小、总行数元信息；代码区支持 Tree-sitter 高性能语法高亮与行号。
3. **光标上下移动实时联动（Live Synchronous Preview）**：
   - 在文件树中按 `↑`/`↓`（或 `j`/`k`）移动光标，右侧代码视口**毫秒级实时刷新**对应文件内容，实现流畅的代码快速扫读。
   - 按 `@` 键一键将当前预览文件路径（如 `@src/app.tsx`）填入主输入框并关闭抽屉。

## 3. 范围边界（Scope）

- **包含（In Scope）**：
  - 侧边栏容器改造：全部采用 `OverlayShell` 风格的右靠齐遮罩抽屉。
  - 抽屉双栏布局组件：`[ 文件树 / 状态 (36列) ] + [ 代码视口 (剩余宽度) ]`。
  - 文件树光标移动事件实时触发文件预览加载（受控缓存/防抖，避免无谓重复 IO）。
  - 下线旧的居中 Modal 弹框（`FilePreviewModal`），将预览逻辑内聚到双栏抽屉中。
  - 快捷键与双模操作：`Tab` / `Ctrl+B` 唤起/收起，`↑`/`↓`/`Enter`/`@`/`Esc` 完整键盘状态流。
- **不包含（Out of Scope）**：
  - 跨进程 JSON-RPC 协议改动。

## 4. 什么算完成（Acceptance Criteria）

1. 唤起侧边栏时，主聊天视口完全不发生重排，抽屉以半透明遮罩形式浮在最上层。
2. 在文件树中选中代码文件时，抽屉右侧展开代码预览视口并带语法高亮。
3. 上下移动光标实时切换右侧代码预览；按 `@` 一键将 `@path` 写入输入框。
4. 按 `Esc` 或点击左侧遮罩直接关闭抽屉，光标归还输入框。
5. 单元测试 `bun run test:ts` 与 `bun run typecheck` 全绿通过。
