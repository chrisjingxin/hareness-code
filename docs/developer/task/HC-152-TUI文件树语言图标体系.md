---
id: HC-152
title: TUI文件树语言图标体系
feature_area: CLI/TUI表现层
parent_task: -
decomposed_by: Antigravity
priority: P1
status: 待验收
owner: Antigravity
branch: feat/hc-152-file-icons
reviewed_at: 2026-08-16
review_due: 2026-08-30
scope: 在 TUI 表现层引入针对编程语言与配置文件的专色图标体系（Nerd Fonts + 品牌专色映射），支持文件夹展开/收起精准识别与数十种主流编程语言图标。
acceptance: 文件夹展开与收起呈现精致的专用图标与琥珀金色彩；Python、TS/JS、Rust、Go、C/C++、JSON、Markdown、Docker、Git 等根据文件类型呈现专属图标与品牌色彩；字符宽度严格对齐无抖动。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-152-TUI文件树语言图标体系.md
test_evidence: bun test packages/cli/tests/tui/presentation/file-icons.test.ts (4 pass); bun run typecheck (0 error); bun run test:ts (728 pass)
references: docs/developer/task/HC-151-TUI主从双栏抽屉与实时预览.md
completed_at: -
---

# HC-152: TUI 文件树语言图标体系

## 1. 为什么做（Why）

目前文件树中的图标仅使用了通用的 `📂`/`📁` 以及 `📄` 单一符号，所有不同语言与配置文件缺乏视觉区分度。开发者在浏览工程目录时，无法一眼区分 Python 代码、TypeScript 脚本、Rust 源码、JSON 配置、Markdown 文档或 Dockerfile。引入专属的语言图标与色彩体系，可以极大地提升工程代码浏览的辨识度与高级感。

## 2. 用户最终得到什么（User Outcome）

1. **丰富的多语言与配置文件专属图标**：
   - Python 文件（`.py`、`.ipynb`）：呈现 Python 专属图标 ` ` / `󰌠 ` 与 Python 蓝色；
   - TypeScript / JavaScript（`.ts`、`.tsx`、`.js`、`.jsx`）：呈现 TS 蓝色 ` ` / JS 亮黄色 ` `；
   - Rust（`.rs`）：呈现 Rust 铁锈橙色 ` `；
   - Go（`.go`）：呈现 Gopher 青蓝色 ` `；
   - C / C++ / Header（`.c`、`.cpp`、`.h`）：呈现 C/C++ 经典蓝色 ` ` / ` `；
   - 配置文件（`package.json`、`tsconfig.json`、`.gitignore`、`Dockerfile`、`.env` 等）：呈现对应的专用图标；
   - Markdown / 文档（`.md`、`.txt`、`.pdf`）：呈现文档与阅读图标。
2. **精致高级的文件夹识别**：
   - 展开文件夹：`▾ 󰝰 ` / `▾ 📂 ` 琥珀金；
   - 收起文件夹：`▸ 󰉋 ` / `▸ 📁 ` 暖黄色。
3. **完美对齐与抗抖动**：
   - 所有图标规范占用固定宽度（2 单元格对齐），杜绝字符跳动。

## 3. 范围边界（Scope）

- **包含（In Scope）**：
  - 新建 `packages/cli/src/tui/presentation/sidebar/file-icons.ts` 纯图标映射模块。
  - 在 `file-tree-widget.tsx` 与 `sidebar.tsx`（CodePreviewPane 头部）接入智能图标与色彩解析。
  - 覆盖单元测试与类型检查。
- **不包含（Out of Scope）**：
  - 跨进程 JSON-RPC 协议改动。

## 4. 什么算完成（Acceptance Criteria）

1. 在文件树中展示工程文件时，不同后缀与特殊文件名精准显示对应的语言图标与颜色。
2. 文件夹展开与折叠时图标状态切换自然且醒目。
3. 单元测试与类型检查全部通过。
