---
id: HC-137
title: 闭环编辑结果反馈、diagnostics 与弱模型生产验收
feature_area: Agent 文件读写可靠性
parent_task: -
decomposed_by: Codex
priority: P1
status: 已过时
owner: Codex (Luna Max)
branch: codex/zc-135-snapshot-file-contract
reviewed_at: 2026-08-10
review_due: -
scope: 在安全提交完成后，向弱模型返回实际落盘内容的新 Snapshot、变更范围和有界局部窗口，使连续编辑无需完整重读；增加可选限时 LSP diagnostics 摘要与去敏观测指标，使用 HC-133 fixture 对最终生产路径重新跑真实企业弱模型评测，并完成用户/架构文档、项目检查和版本影响闭环。
acceptance: edit/write 成功返回可直接继续使用的新 Snapshot、实际 changed range 和有界上下文，不返回整文件；delete 使旧 Snapshot 失效；diagnostics 超时/缺 LSP 不回滚写入、不误报零错误，输出有数量/字节上限；观测只记录 code、相对路径和聚合指标，不记录源码/new_text/密钥；最终真实弱模型无 silent corruption、完成率不低于基线且报告含 token/调用/重读/延迟；用户文档、架构总览、任务证据、project:check/typecheck/test 和版本影响闭环。
user_docs: docs/user/交互使用.md、docs/user/安全与沙箱.md、docs/user/故障排查.md
developer_docs: docs/developer/spec/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/research/弱模型文件编辑评测.md、docs/developer/architecture/架构总览.md
test_evidence: "focused 80 passed；非沙箱 Agent 全量无失败；typecheck/project:check 通过；Bun 全量仅现有 bundle EISDIR 失败；真实模型未授权未运行"
references: docs/developer/task/HC-131-统筹弱模型优先的文件读写可靠性.md、docs/developer/task/HC-133-建立企业弱模型文件编辑评测与s.md、docs/developer/task/HC-136-工程化文件mutation的d.md
completed_at: -
---

> 2026-08-10 流程复核：本文件是同一功能内部的历史验收步骤，不再作为独立 Task。
> 仍有效的范围、Todo 与证据已收回 [HC-131](HC-131-统筹弱模型优先的文件读写可靠性.md)；以下内容仅保留历史追溯。

## 服务的用户结果

安全写入不能以每次完整重读为代价。成功结果应让弱模型立即知道“实际改成了什么、现在的
Snapshot 是什么、是否出现新的诊断”，失败结果则只给一个明确恢复动作。

## 实施步骤

1. 根据 commit 后实际重读内容计算 changed range；返回新 Snapshot、增删统计和变更前后
   有界上下文，不返回整个大文件。
2. 行号变化后以新 Snapshot 的实际源行号为准；连续 edit 可以直接引用返回区间，模型无需
   为同一区域重新 `read_file`。
3. `POST_WRITE_DRIFT` 时同时返回 proposed 与 actual 的有界差异提示；下一次 edit 只能使用
   actual Snapshot。
4. 接入可选 LSP diagnostics：提交后、只读、短超时、有数量/字节上限；区分 `unavailable`、
   `timeout`、`0 diagnostics`，失败不回滚已成功写入。
5. 不自动 formatter；若现有用户流程需要，文档说明显式运行后 Snapshot 会 stale，必须重读。
6. 增加聚合观测：error code、Snapshot 过期/stale/未读范围次数、重读次数、edit 成功率、
   结果字节、diagnostics 延迟；日志不含源码、new_text、完整 ID 或凭据。
7. 使用 HC-133 同版本 fixture 和重复次数对最终生产 ToolNode 路径重跑实际企业弱模型，比较
   基线和候选报告，任何 silent corruption 阻止完成任务。
8. 更新交互使用、安全与沙箱、故障排查、架构总览和 HC-131 设计最终状态；记录无版本变更
   或通过 `version:set` 执行正式版本更新。
9. 运行 Python focused/full、Bun tests、typecheck、project:check，并把命令/结果写入任务证据。

## 明确不修改

- 不实现自动 formatter、fuzzy recovery、hashline 或跨文件事务。
- 不把 diagnostics 当作提交成功条件或事实来源。
- 不记录模型 prompt、完整 tool args、源码片段或 API 响应正文。

## 验收清单

- [x] 连续编辑使用返回的新 Snapshot，不需要完整重读。
- [x] 返回内容、diagnostics 和日志全部有界且去敏。
- [x] diagnostics unavailable/timeout/zero 三种状态可区分。
- [ ] 最终真实弱模型报告满足 HC-133 安全与完成率门槛。
- [x] 用户/架构/研究/任务证据和版本影响闭环。
- [ ] `bun run project:check`、`bun run typecheck`、`bun run test` 与 Python 全量通过。

## 实施与验证证据

- 已在唯一 `SnapshotFileToolContract` 路径实现：commit 后实际重读计算 `changed_range`，create/edit
  返回最多 200 行、32 KiB 的局部上下文和新 Snapshot；完整显示的 `shown_lines` 直接计入新 Snapshot
  的已读范围，因此连续 edit 不必完整重读。delete 会使同路径旧 Snapshot 失效。
- 已接入异步 ToolNode 的可选 Plugin LSP diagnostics：最多 1 秒、20 项、8 KiB，只返回严重级别、短
  code 与行号；`unavailable`、`timeout`、`ok,count=0` 语义独立，失败不回滚写入，也不触发 formatter。
  `POST_WRITE_DRIFT` 同时返回已批准 `proposed_diff` 与实际 `actual_diff`，下一步只使用实际 Snapshot。
- 已增加 Host 进程内 `FileToolMetrics` 和相对路径的结构化日志。指标仅聚合 error code、Snapshot
  expired/stale/未读范围、重读、edit 成功、结果字节和 diagnostics 延迟；不保存路径、源码、`new_text`、
  完整 Snapshot ID 或凭据。`config.show` 返回脱敏 `file_tool_metrics`。
- `cd packages/agent && .venv/bin/python -m pytest -q tests/tools/test_snapshot_file_contract.py tests/host/test_server.py tests/runtime/test_agent.py`
  → `80 passed, 23 warnings`。覆盖连续 edit、line-bound 局部窗口、drift 双 diff、diagnostics 三态/上限、
  去敏 metrics/log 及真实 ToolNode 接线。
- `cd packages/agent && .venv/bin/python -m pytest -q --disable-warnings`：受限环境先在 9 个 Host
  attachment 用例因禁止绑定 `127.0.0.1:0` 失败；以允许 loopback 的执行环境重跑后完成且未出现失败。
- `bun run typecheck` → 通过；`bun run project:check` → 通过。
- `bun run test` 在允许 loopback 的执行环境中为 `539 pass, 1 skip, 1 fail`。唯一失败为既有
  `packages/cli/tests/web/bundle.test.ts`，Bun 读取 worktree 内 React、react-dom、protocol 与 marked
  的链接模块时报 `EISDIR`；与本任务 Python 文件工具改动无关。受限环境另有 5 个 Web loopback
  用例因端口绑定限制失败，非沙箱重跑已通过。
- 真实企业弱模型评测未运行：用户明确要求不调用真实模型，因此没有伪造完成率、token、调用、重读或
  延迟报告。该外部验收未完成前，任务保持 `进行中`。
- 版本影响：无。仅收敛未发布的内部 Agent 实现、文档与测试，未运行 `version:set`。

## 定期复核记录

- 2026-08-10（Codex）：从 HC-131 拆解；依赖 HC-136，下一次复核 2026-08-24。
- 2026-08-10（Codex (Luna Max)）：HC-136 依赖已在当前分支工作树内，范围仍有效。已完成本地结果、
  diagnostics、观测、文档和自动测试闭环；真实弱模型评测受“不得调用真实模型”用户约束未运行，且 Bun
  bundle 测试存在既有 worktree `EISDIR` 环境失败。因此保持进行中，下一次复核 2026-08-24。
