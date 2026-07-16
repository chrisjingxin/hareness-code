# 任务源

一个文件只描述一个可独立认领的任务，文件名使用 `<ID>.md`，例如 `ZC-001.md`。不要编辑生成的 [任务看板](../任务看板.md)。

```md
---
id: <任务 ID，例如 ZC-序号>
title: 简短标题
priority: P1
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

## 范围

## 非范围

## 验收清单
```

字段、状态转换和命令见 [任务看板说明](../任务看板说明.md)。
