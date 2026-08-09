# 任务源

当前活动任务源只保留已经排期、可独立认领并能验证的工程任务。已完成任务移入 `archive/` 作为审计记录，不进入任务看板；尚未排期的产品方向统一记录在 [新功能候选](../project/新功能候选.md)。

活动目录中的一个文件只描述一个可独立认领的架构任务，文件名使用 `<ID>.md`。不要编辑生成的 [任务看板](任务看板.md)。

```md
---
id: <任务 ID>
title: 简短标题
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

状态仅可为：`待认领`、`进行中`、`阻塞`、`待验收`、`已完成`、`已过时`。认领时必须写入负责人和分支：

```bash
bun run task:claim -- <ID> --owner <名称> --branch <分支>
```

`进行中` 必须有负责人和分支；`已完成` 必须同时有测试证据、完成日期和用户/开发者文档影响记录。完成示例：

```bash
bun run task:complete -- <ID> --evidence "bun run test" --references "abc123"
```

`已过时` 只用于需求、架构或上层方案已经替代原任务且继续实施会产生错误结果的情况。它必须填写本次 `reviewed_at`，把 `review_due` 设为 `-`，在 `references` 和正文“定期复核记录”中说明替代任务及保留的历史产物。

## 拆解与复核

- 多任务功能先建立上层任务和同 ID 设计文档；每个子任务通过 `parent_task` 指向直接上层，通过 `feature_area` 归入同一功能板块。
- `decomposed_by` 是任务拆解责任人；`owner` 是后续通过 `task:claim` 写入的实现认领者，两者不得混用。
- 活动任务默认 14 天复核一次。复核确认范围仍有效后更新 `reviewed_at` 与 `review_due`；到期未更新时 `tasks:check` 失败。
- 认领前必须先复核任务与当前代码/上层设计是否一致。发现任务实际完成、部分失效或已被替代时，先更新任务状态和复核记录，不得直接按过时方案开工。
- 上层任务只有在所有子任务完成或有依据地过时，并通过共同验收后才能关闭。

## 元数据与校验

每个新建或实质更新任务的 front matter 固定包含：`id`、`title`、`feature_area`、`parent_task`、`decomposed_by`、`priority`、`status`、`owner`、`branch`、`reviewed_at`、`review_due`、`scope`、`acceptance`、`user_docs`、`developer_docs`、`test_evidence`、`references`、`completed_at`。优先级为 `P0`、`P1` 或 `P2`。历史任务缺少追溯字段时仍可读取，但看板会明确显示“历史未记录/历史未归类”；下次认领或实质更新时必须补齐。

```bash
bun run tasks:sync
bun run tasks:check
```

任务正文应描述范围、非范围和可验证验收条件。活动文档若引用任务 ID，`bun run docs:check` 会确认该 ID 存在；`docs/developer/research/archive/` 的历史快照允许保留当时的旧编号，但其中的本地链接仍会被校验。
