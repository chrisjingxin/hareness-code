---
id: ZC-100
title: 整理开发文档与项目脚本
priority: P0
status: 已完成
owner: Codex
branch: master
scope: 将开发文档归入 Architecture、Project、Research 与 Tasks，并按任务、文档和发布职责拆分项目管理脚本。
acceptance: 开发文档根目录不再平铺，历史材料明确归档，任务工具和全仓链接使用新 canonical 路径，项目检查通过。
user_docs: 不涉及
developer_docs: docs/developer/
test_evidence: bun test scripts/project (8 passed); bun run docs:check; bun run tasks:check; bun run project:check
references: 工作区目录重构（未提交）
completed_at: 2026-07-31
---

## 背景

开发者文档当前将架构说明、工程流程、调研报告和任务看板平铺在同一目录。根 `project-management.ts` 也同时实现任务、文档和发布三类独立命令。

## 当前存在的问题

- 文档用途必须打开文件后才能判断。
- 过期实施计划仍位于 `.agent/` 并被当前架构文档引用。
- 任务看板说明与任务 README 重复。
- 项目管理脚本与测试归属在 CLI package 中，不符合实际所有权。

## 为什么现在要修改

源码迁移后会产生一次集中链接更新；在同一阶段归类文档和脚本可以只修改一次 canonical 路径，避免长期保留旧路径。

## 目标设计

```text
docs/developer/
├─ architecture/
├─ project/
├─ research/
└─ tasks/

scripts/project/
├─ index.ts
├─ tasks.ts
├─ docs.ts
└─ release.ts
```

任务 README 同时说明任务源与看板规则；历史计划只在 Research archive 保留，不再作为当前事实来源。

## 实施步骤

1. 在保留现有任务归档改动的前提下移动文档。
2. 合并任务说明，移动生成看板并更新项目工具。
3. 拆分项目脚本与测试，更新根 package scripts。
4. 更新 README、AGENTS、任务元数据和全仓 Markdown 链接。
5. 运行任务、文档和完整项目检查。

## 范围

- 开发者文档、任务工具、项目脚本与相关测试。

## 非范围

- 不重写用户文档内容。
- 不清理本地缓存、IDE、虚拟环境或 `tmp/`。

## 验收清单

- [x] 开发文档根目录只保留职责目录。
- [x] 当前文档入口没有指向旧源码路径或过期 `.agent` 状态；历史快照已归档并保留可用链接。
- [x] 任务看板继续由工具生成。
- [x] 项目管理测试与 `project:check` 通过。
- [x] 无版本变更。

## 版本影响

无版本变更。此次迁移只调整开发文档、任务工具和检查命令的路径，不改变用户文档、CLI 行为或协议。
