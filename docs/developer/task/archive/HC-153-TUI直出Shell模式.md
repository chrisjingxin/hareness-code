---
id: HC-153
title: TUI直出Shell模式
feature_area: CLI/TUI表现层
parent_task: -
decomposed_by: Antigravity
priority: P1
status: 已完成
owner: Antigravity
branch: master
reviewed_at: 2026-08-16
review_due: 2026-08-30
scope: 在 TUI 输入栏实现参考 MiMo Code 的极速无感直出 Shell 模式，支持 ! 触发、专属色彩 Shell 状态指示、Esc/Backspace 极速退出、免模型直出执行与时间线结果持久化。
acceptance: 输入框为空时键入 ! 瞬间进入 Shell 模式（! 不写入文本）；左上角呈现专属色彩的「Shell」纯文本标签（与 Build 金、Compose 紫不同色）；不改变边框颜色；Esc 或空内容 Backspace 瞬间退出；提交时免 LLM 推理直出执行并在时间线展示结果与支持中断。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-153-TUI直出Shell模式.md
test_evidence: 2026-08-16: bun run protocol:check (pass), bun run typecheck (pass), bun run test:ts (732 pass), pytest (2176 pass)。涵盖直出模式状态机切换、Esc 快捷键退出、direct_shell 协议下发与本地子进程超时安全执行。
references: docs/developer/research/153-TUI直出Shell模式.md
completed_at: 2026-08-16
---

# HC-153: TUI 直出 Shell 模式

## 1. 为什么做（Why）

开发者在终端使用 Coding Agent 时，经常需要快速运行如 `git status`、`ls -la`、`pytest`、`npm test` 等命令。如果每条简单的 Shell 命令都需要通过 LLM 生成工具调用，不仅耗费时间、消耗 Token，而且交互繁琐。提供极速无感的 `!` 直出 Shell 模式，能让开发者在同一个 TUI 界面中无缝完成终端命令调试与 AI 协同。

## 2. 用户最终得到什么（User Outcome）

1. **极简优雅的 Shell 模式指示**：
   - 输入框为空时按 `!` 键，瞬间进入 Shell 模式（`!` 字符不写入输入框）；
   - 输入栏上方的 Mode 标签呈现纯文本 **`Shell`**（不带图标、不带括号，使用独立于 Build 与 Compose 的专色 `#56B6C2` 绿松石青）；
   - 输入框边框保持常规颜色，无多余装饰。
2. **零摩擦退出（Instant Exit）**：
   - 在 Shell 模式下，按 `Esc` 键或在输入框为空时按 `Backspace` 键，瞬间退回常规对话模式。
3. **免模型极速执行（Direct Shell Execution）**：
   - 按 `Enter` 提交后，直接在本地工作区执行命令，不调用 LLM 模型；
   - 支持流式输出与 `Esc` 运行中断；
   - 结果记录在时间线中，后续 AI 对话可以感知该命令及其输出。

## 3. 范围边界（Scope）

- **包含**：
  - TUI `InputBar` / `adapter.ts` 的 `inputMode: "chat" | "shell"` 状态机与按键拦截；
  - `tuiTheme` 增加 `modeShell` 专色；
  - `InteractiveController` 支持直出 Shell 执行与时间线记录；
  - Python sidecar 提供安全受控的直接 Shell 执行后端（120s 超时、取消与工作区隔离）。
- **不包含**：
  - 持久化交互式 PTY（如 vim/top）。

## 4. 什么算完成（Acceptance Criteria）

1. 终端按 `!` 键能准确无缝切换为 Shell 模式，标签显示专色 `Shell`；
2. 按 `Esc` 或 `Backspace` 能即时退回普通对话；
3. 执行命令能在时间线中正确流式输出并支持中断；
4. 单元测试与类型检查全部通过。
