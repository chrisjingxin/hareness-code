---
id: HC-103-legacy
title: 提取共享交互内核并迁移 TUI
priority: P0
status: 已完成
owner: chrisjingxin
branch: codex/zc-103
scope: 从现有 TuiController 提取表现层无关的 Interactive Core，以 InteractiveController 作为统一 interface，并让 TUI 完整迁移，统一 Run、Timeline、Interaction、Thread、catalog 和 Slash Command 语义。
acceptance: TUI 的聊天与命令行为全部通过 InteractiveController 驱动；应用语义不再依赖 OpenTUI 或 DOM；同一 intent 在不同 Interactive Adapter 下产生相同 RPC 与领域 snapshot；旧重复实现和透传 wrapper 被删除。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/architecture/斜杠命令体系.md
test_evidence: "CLI: bun test 167 pass 0 fail（含 tests/interactive 50 项 interface contract 与 10 项 TUI adapter 回归）；Python: pytest 540 passed 1 skipped；bun run build / typecheck / project:check 全部通过。OCR：限定 commit 8acf9f0..HEAD 范围内检视，通过。"
references: docs/developer/architecture/斜杠命令体系.md
completed_at: 2026-08-03
---

## 背景

当前 `TuiController` 同时拥有 Prompt 编辑、OpenTUI picker/dialog、Slash Command、Run、Timeline reducer、Interaction、Thread 恢复、模型选择、Skill 和 MCP 工作流。Web 若直接复用这个类会被终端细节污染；若另写一个 controller，又会产生两套业务语义。

本任务先建立表现层无关的 Interactive Core，并把现有 TUI 迁移成第一个 Interactive Adapter。HC-104 只能复用这个已被 TUI 验证过的 interface，不能先为 Web 新增平行实现。

## 当前存在的问题

- Run/Event/Interaction 与 TUI draft、光标、overlay、滚动状态位于同一个大类。
- CommandRegistry 已统一声明命令，但副作用解释仍只存在于 TUI Controller。
- Thread/模型/Skill/MCP catalog 的加载、错误和选择状态没有可供第二个 Interactive Adapter 使用的领域 snapshot。
- 现有测试主要通过 TUI 专用 snapshot 验证；若再新增 Web controller，语义漂移无法被契约测试发现。

## 为什么现在要修改

共享应用层是 Web parity 的前置条件。只有先让现有 TUI 通过新 seam 工作，才能证明该 module 真正隐藏了业务复杂度，而不是为 Web 创建一个无人复用的浅层 wrapper。

## 目标设计

`InteractiveController` 提供小 interface：

```text
dispatch(InteractiveIntent) → Promise<InteractiveResult | void>
getSnapshot()              → InteractiveSnapshot
subscribe(listener)        → unsubscribe
close()                    → Promise<void>
```

它内部拥有：

- Timeline reducer、active Run、取消和唯一终态；
- approval/question 的动态 schema、超时与 response；
- nullable current Thread、恢复和空首页；
- 模型、Skill、MCP catalog 与选择/写入；
- CommandRegistry、availability 和 command dispatcher；
- semantic notice/status，不包含终端尺寸、DOM、颜色或组件状态。

TUI Interactive Adapter 只拥有 draft/光标、Prompt history、picker/dialog 展示、快捷键、滚动和 OpenTUI refs。它将用户动作转换为 `InteractiveIntent`，不直接调用 AgentClient 业务方法。

## 实施步骤

1. 列出 `TuiController` 当前字段和方法，按“共享语义 / TUI presentation”分类，先固定 `InteractiveSnapshot` 与 `InteractiveIntent`。
2. 将 reducer、Run drain、Interaction handler、Thread/catalog/model/Skill/MCP 工作流移入 `InteractiveController`；依赖通过现有 AgentClient interface 注入。
3. 让 Slash Command dispatcher 返回表现层无关的 semantic intent/result；help、status、参数校验、capability 和 busy 条件只实现一次。
4. 将 TUI Controller 改造为 Interactive Adapter，所有 Agent RPC 和领域状态均通过 `InteractiveController`；保留 OpenTUI 专属 overlay 与输入体验。
5. 删除旧的 Run/Event/Interaction/catalog 重复字段和方法，不保留 alias、fallback 或双写。
6. 将旧测试迁移到共享 interface；新增同一 intent 序列的 snapshot/RPC 契约测试，再保留少量 TUI adapter 测试验证键盘和 overlay 映射。
7. 更新架构总览和 Slash Command 文档，说明共享 module 与两个 adapter 的 seam。

## 范围

- CLI 的 Interactive Core interface、implementation、intent 和 snapshot。
- TUI Interactive Adapter 的完整迁移。
- Timeline、Run、Interaction、Thread、模型、Skill、MCP、compact、status 和 Slash Command 语义。
- interface 级测试和 TUI adapter 回归。

## 非范围

- 不实现 React DOM 或 Web 页面布局。
- 不实现 WebHandoffCoordinator、attachment 或 Host 控制租约。
- 不改变 Agent 业务规则或新增 TUI 当前没有的产品功能。
- 不保留新旧 Controller 双运行或状态同步层。

## 验收清单

- [x] TUI 生产路径创建并使用 `InteractiveController`，旧 TUI 业务实现已删除。
- [x] TUI Interactive Adapter 不直接调用 Run、Thread、模型、Skill、MCP 和配置写 RPC。
- [x] CommandRegistry 的解析、可用性、参数校验和执行只存在一条 canonical 路径。
- [x] approval/question、取消、Thread 切换、空首页和模型选择均通过共享 snapshot 可观察。
- [x] `thread.changed` 能明确表达 `string | null`，清空 Thread 不会保留旧身份。
- [x] interface 测试覆盖正常、失败、取消、超时、乱序/重复 Event 和 catalog 部分失败。
- [x] 删除 module 后复杂度会重新散落到 adapter，证明该 seam 具有实际 depth。

## 前置

- 无；可与 HC-101 并行，但 HC-104 必须等待本任务完成。

## 代码检视记录（OCR）

**审查命令**：`git diff 8acf9f0^..HEAD`（限定 HC-103 的 commit 范围：8acf9f0、5c007ad、c546038）
**背景**：任务文档 HC-103.md、方案设计 designs/HC-103.md、AGENTS.md 仓库协作规范。
**检视结论**：通过。功能与验收项全部完成；架构分层符合方案（interactive/ 不依赖 tui/web/React/OpenTUI/DOM，TUI adapter 不直接调用 IPC）；无安全问题（无密钥泄漏、无空 catch、无类型断言绕过、不可信 payload 均经校验）。发现并修复 2 个高优先级问题，均补充回归测试后复检通过：
1. **transientNotice 未渲染**（宿主级 Web 启动失败等通知在 adapter 设置后 Presentation 不可见）→ 在时间线末尾与首页 composer 下方按系统消息样式渲染，新增 app-interaction 测试验证 `/web` 无 launcher 时通知可见。
2. **Interaction timeout_ms=0 竞态**（timer 注册早于 pendingInteraction 赋值，同步触发导致已 resolve 的 Interaction 残留 pending 永不收敛）→ 先登记 pending 再注册 timer，新增 tests/interactive 边界回归测试。
其余检视点（approval decisions allowlist、question 完整 schema 校验、Thread generation 晚到丢弃、模型双路径、semantic operation dispatcher、catalog 部分失败隔离）均符合方案 invariant。

**合并说明**：master 已 rebase 到远程 `13959c5`（增加工具审批机制，作者 LYMAXPUP）。合并过程中解决 3 处冲突：协议生成文件（重新运行 `protocol:generate` 对齐）、`run_coordinator.py`（同时保留远程 `session_rules` 属性与 HC-101 的 `start(allow_multithread)` 新签名）、`runtime.ts`（把远程 `TuiApprovalMode`/`APPROVAL_MODE_CYCLE`/`nextApprovalMode` 及 Shift+Tab 审批模式循环迁移到 Interactive Core：新增 `approval-mode.cycle` intent，controller 持有 `approvalModeOverride` 并传给 `run.start` 的 `approval_mode`，补充 interface 测试）。CLI 侧 168 测试、typecheck、build、project:check 全部通过；Python 599 passed / 2 failed。

**预存问题记录（与 HC-103 无关，经用户确认不修复）**：远程 `13959c5` 自带的 2 个 Python 测试失败，已在单独 checkout 13959c5 时复现确认，建议远程作者处理：
1. `tests/test_architecture.py::test_package_root_contains_only_entrypoints`：13959c5 新增的 `harness_agent/subagents.py` 位于 package 根目录，违反架构规则（应归入 `runtime/` 并同步测试 import）。
2. `tests/host/test_server.py::test_auto_edit_writes_without_interruption_but_shell_still_requires_approval`：auto-edit 模式下 `write_file` 未实际写入文件（`auto.txt` 缺失），疑似远程审批链路缺陷。

**版本影响**：本任务为重构，无用户可见功能变更，无版本号变更。
