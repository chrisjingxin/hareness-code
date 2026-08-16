---
id: HC-154
title: 隐藏内置工作流Skill
feature_area: Skill 扩展与管理
parent_task: -
decomposed_by: 历史未记录
priority: P1
status: 已完成
owner: Antigravity
branch: master
reviewed_at: 2026-08-16
review_due: -
scope: 在公开 Skill 列表与 TUI 界面中隐藏内置供 Compose 使用的 Skill，保留内部运行时调用，支持用户显式安装同名 Skill 正常展示。
acceptance: 1. skills.list RPC 与 TUI 侧边栏/补全中默认隐藏 source 为 builtin 的 Skill；2. Compose 内部工作流及按 ID 读取解析仍可正常加载 builtin Skill；3. 用户在 project/user 安装同名 Skill 时正常在列表中显示并生效。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-154-隐藏内置工作流Skill.md
test_evidence: pytest: 2178 passed; bun run test:ts: 732 passed
references: -
completed_at: 2026-08-16
---

# HC-154 隐藏内置工作流Skill

## 1. 为什么做（Why）
当前系统中内置了一批用于 Compose 模式流程管控的内部 Skill（如 spec 驱动、plan 拆解、review 检查等）。这些 Skill 是系统核心链路的基础设施，用户在日常使用（浏览技能列表、命令补全、侧边栏管理）时不应感知或产生冗余干扰；但如果用户主动安装了相同名称的开源 Skill，则应正常向用户呈现和管理。

## 2. 用户最终得到什么（What）
- **清爽可见**：`/skills` 命令、侧边栏 Skills 抽屉、Slash 补全列表中不再混入内置工作流 Skill，仅展示用户实际安装或项目配置的技能；
- **透明覆盖**：如果用户在 `.harness/skills/` 或 `~/.harness/skills/` 安装了开源同名 Skill，该 Skill 正常在界面中展示并支持启停；
- **流程不坏**：Compose 模式内部工作流引擎在执行时仍能精准加载和执行内置 Skill，不影响结构化研发链路。

## 3. 验收标准（Acceptance）
1. `SkillRegistry.list()` 和 `skills.list` RPC 返回结果中默认过滤掉 `source == "builtin"` 的 Skill；
2. `SkillRegistry.resolve()` / `SkillRegistry.load()` / `SkillRegistry.read_resource()` 仍完整支持 `builtin` Skill，保证内部运行不受影响；
3. 当扫描到 `source` 为 `project`、`user` 或 `market` 的 Skill 时，正常包含在 `list()` 返回结果中；
4. 全量 Python 和 TypeScript 测试通过。
