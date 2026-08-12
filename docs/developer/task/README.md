# 任务源

当前活动任务源只保留已经排期、可独立认领并能验证的工程任务。已完成任务移入 `archive/` 作为审计记录，不进入任务看板；尚未排期的产品方向统一记录在 [新功能候选](../project/新功能候选.md)。

## 流程

```text
task → spec → plan → todo → implement → review
```

- **task**（本目录）：功能源头，先于 Spec/Plan/Todo 产生；生成前使用 `mattpocock:grill-me` 做需求确认。
- **spec**：`docs/developer/spec/`，与 Task 同名；`agent-skills:spec-driven-development` + `mattpocock:codebase-design`。
- **plan** / **todo**：`docs/developer/plan/`、`docs/developer/todo/`，与 Spec 同名；`agent-skills:planning-and-task-breakdown`。
- **implement**：`agent-skills:test-driven-development`。
- **review**：`agent-skills:code-review-and-quality`；合并冲突用 `mattpocock:resolving-merge-conflicts`。

完整约定见根目录 [AGENTS.md](../../../AGENTS.md)。

## 命名

活动目录中一个文件只描述一个可独立认领的功能任务：

```text
HC-XXX-功能简介.md
```

- `HC` 为固定前缀；`XXX` 为三位数字编号。
- **必须**带中文功能简介（不超过 15 字），禁止只有 `HC-XXX.md`。
- 同编号的 Spec / Plan / Todo 文件名与 Task 完全一致。

不要编辑生成的 [任务看板](任务看板.md)。

## Front matter

```md
---
id: HC-XXX
title: 功能简介（中文，≤15 字）
feature_area: 稳定的功能板块名称
parent_task: 直接上层任务 ID；根任务为 -
decomposed_by: 拆解者名称
priority: P0
status: 待认领
owner: 未认领
branch: -
reviewed_at: YYYY-MM-DD
review_due: YYYY-MM-DD
scope: 要完成的范围。
acceptance: 可验证的验收结果。
user_docs: 不涉及或具体文档路径
developer_docs: 对应 spec/plan/architecture 路径
test_evidence: -
references: -
completed_at: -
---
```

任务正文应让不熟悉实现的人也能判断：背景、当前问题、范围/非范围、可观察验收。

## 状态与认领

状态仅可为：`待认领`、`进行中`、`阻塞`、`待验收`、`已完成`、`已过时`。

```bash
bun run task:claim -- <ID> --owner <名称> --branch <分支>
bun run task:complete -- <ID> --evidence "bun run test" --references "abc123"
```

- `进行中` 必须有负责人和分支。
- `task:complete` 会写入证据并把文件移入 `archive/`，不再出现在活动看板。
- `已过时` 必须填写 `reviewed_at`、替代 `references`，并将 `review_due` 设为 `-`。

## 看板与校验

```bash
bun run tasks:sync
bun run tasks:check
bun run docs:check
```

活动任务默认 14 天复核一次；到期未更新时 `tasks:check` 失败。活动文档若引用 `HC-XXX`，`docs:check` 会确认该 ID 在活动或归档任务中存在；`docs/developer/research/archive/` 的历史快照允许保留旧编号，但本地链接仍会被校验。
