---
id: HC-142
title: 对人工审批的截断文件 diff 失败关闭
feature_area: Agent 文件读写可靠性
parent_task: -
decomposed_by: Codex
priority: P0
status: 已过时
owner: Codex
branch: master
reviewed_at: 2026-08-11
review_due: -
scope: 原计划在人工审批 diff 超过 200 行、16 KiB 或无法完整展示时拒绝文件 mutation；用户已明确否决该方向，任务不再实施。
acceptance: 不适用；现行产品决定是审批 diff 作为明确标记省略内容的有界预览，即使预览受内容上限或终端高度限制也保留允许/拒绝选项。
user_docs: 不涉及
developer_docs: docs/developer/spec/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/architecture/架构总览.md
test_evidence: 不实施；本地未推送提交 4f330a5 已从 master 待推送历史移除。
references: docs/developer/task/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/spec/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/task/archive/HC-141-安全初始化空文件.md、docs/developer/task/archive/HC-143-Snapshot并发一致性.md
completed_at: -
---

> 2026-08-11 复核结论：本任务已过时。保留文件用于追溯“截断即失败关闭”方案及其被否决的原因，
> 不再作为实现入口，也不创建替代任务。

## 原问题与结论

文件 mutation 的审批 diff 为避免 payload 和终端界面无界增长，最多保留 200 行、16 KiB；终端
视口还可能只显示其中一部分。原任务把“没有完整展示”视为授权缺口，计划取消批准选项并返回
`APPROVAL_DIFF_TOO_LARGE`。

用户随后明确选择与 Qwen Code 一致的产品语义：diff 是有界审查预览，不要求用户逐字看完后才能
批准。预览因内容上限或终端高度只显示部分时，界面应明确提示仍有内容未展示，同时继续提供允许和
拒绝选项。

因此，原任务要改变的行为不是缺陷。`4f330a5` 在本地实现的失败关闭与最终产品决定相反，从未
推送，现已从 `master` 待推送历史移除；不需要为恢复既有正确行为建立新的功能任务。

## 保留的不变式

- 审批前仍固定 current、proposed content、expected identity 和参数 fingerprint。
- 批准后只提交同一 Thread、Tool Call ID 和 fingerprint 对应的 prepared plan。
- 同一 Tool Call 改参、重放或提交前文件版本变化仍然失败，不能写入未批准或过期内容。
- Workspace、Policy、敏感路径、Snapshot、一次性计划和 backend CAS 边界保持不变。
- 200 行、16 KiB 和终端高度只约束展示；工具输入、文件大小和 backend 能力硬上限仍可拒绝执行。

## 不再实施

- 不因 `MutationDiff.truncated` 取消批准选项或消费 Tool Call。
- 不新增或保留 `APPROVAL_DIFF_TOO_LARGE` 错误。
- 不要求 Agent 仅为通过人工审批而拆碎单次编辑。
- 不为本任务创建分页、artifact、下载链接或外部编辑器能力。

## 与其他任务的关系

- [HC-131](HC-131-统筹弱模型优先的文件读写可靠性.md) 继续作为文件读写可靠性的事实入口，保留 prepared plan、Snapshot 和 CAS 语义。
- [HC-141](archive/HC-141-安全初始化空文件.md) 的空文件初始化采用同一有界审批预览语义。
- [HC-143](archive/HC-143-Snapshot并发一致性.md) 只处理 Snapshot Store 并发一致性，与本决策无关。

## 定期复核记录

- 2026-08-11（Codex）：从 HC-131 拆出“无法完整展示时失败关闭”的候选任务。
- 2026-08-11（Codex）：曾按该方向生成本地提交 `4f330a5`，未推送。
- 2026-08-11（Codex）：用户确认截断或因终端高度隐藏的 diff 仍可批准；任务标记已过时，
  `4f330a5` 已从 `master` 待推送历史移除，不另建替代任务。
