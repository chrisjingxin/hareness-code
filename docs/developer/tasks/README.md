# 任务源

当前活动任务源只保留基于现有代码的 P0 架构重构。已完成任务移入 `archive/` 作为审计记录，不进入任务看板；尚未排期的产品方向统一记录在 [新功能候选](../project/新功能候选.md)。

活动目录中的一个文件只描述一个可独立认领的架构任务，文件名使用 `<ID>.md`。不要编辑生成的 [任务看板](任务看板.md)。

```md
---
id: <任务 ID>
title: 简短标题
priority: P0
status: 待认领
owner: 未认领
branch: -
scope: 要完成的范围。
acceptance: 可验证的验收结果。
user_docs: 不涉及或具体文档路径
developer_docs: 不涉及或具体文档路径
test_evidence: -
references: -
completed_at: -
---

## 背景

说明当前实现如何工作，以及本任务位于哪条生产调用链。

## 当前存在的问题

列出可从当前代码验证的问题、涉及的 module/interface 和具体后果。

## 为什么现在要修改

说明不修改的风险、对其他架构任务的阻塞，以及为什么不是未来功能。

## 目标设计

描述目标 module、interface、seam、adapter 和关键 invariant；必要时给出 ASCII 流程。

## 实施步骤

给出按依赖顺序可执行的迁移步骤和主要代码位置。

## 范围

## 非范围

## 验收清单
```

架构任务不得只写“重构某文件”，必须让认领者能从背景、当前问题和目标 interface 判断该改什么、不该改什么。

## 状态与认领

状态仅可为：`待认领`、`进行中`、`阻塞`、`待验收`、`已完成`。认领时必须写入负责人和分支：

```bash
bun run task:claim -- <ID> --owner <名称> --branch <分支>
```

`进行中` 必须有负责人和分支；`已完成` 必须同时有测试证据、完成日期和用户/开发者文档影响记录。完成示例：

```bash
bun run task:complete -- <ID> --evidence "bun run test" --references "abc123"
```

## 元数据与校验

每个任务的 front matter 固定包含：`id`、`title`、`priority`、`status`、`owner`、`branch`、`scope`、`acceptance`、`user_docs`、`developer_docs`、`test_evidence`、`references`、`completed_at`。优先级为 `P0`、`P1` 或 `P2`。

```bash
bun run tasks:sync
bun run tasks:check
```

任务正文应描述范围、非范围和可验证验收条件。活动文档若引用任务 ID，`bun run docs:check` 会确认该 ID 存在；`docs/developer/research/archive/` 的历史快照允许保留当时的旧编号，但其中的本地链接仍会被校验。
