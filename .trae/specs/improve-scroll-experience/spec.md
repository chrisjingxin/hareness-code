# 改善滚动体验与完成元数据 Spec

## Why

当前项目与 opencode 在三个高频可感知体验上存在明显差距：
1. **滚轮速度**：`<scrollbox>` 未配置 `scrollAcceleration`，使用默认 `LinearScrollAccel`（每次滚轮仅 1 行），用户感知为"滚轮不工作"。opencode 默认 3 行/次 + 可选 macOS 惯性加速。
2. **自动滚底**：用户提交消息后、切换 thread 后，界面不会主动滚到底部。如果 `stickyScroll` 因手动上滚而解除，新消息到达时用户看不到最新内容。opencode 在提交、session 切换、undo 后均主动 `scrollTo(bottom)`。
3. **完成元数据行**：助手回答结束后缺少模型名、耗时、token 用量的紧凑元数据行。opencode 每条助手消息后显示 `▣ mode · model · duration`。hareness-code 的 `RunSummary` 有耗时和 token 但缺少模型名，且仅在最后一次运行后显示。

## What Changes

- 新增滚动加速工具模块，默认使用 `FixedSpeedScroll(3)`（固定 3 行/次，与 opencode 一致，全平台体验稳定），同时导出 `MacOSScrollAccel` 供触控板用户备选（该算法依赖高频事件，Windows 传统鼠标离散档位下加速不会触发）
- 为对话时间线和 SearchPicker 的 scrollbox 添加 `scrollAcceleration` 属性
- 在用户提交消息后和 thread 切换后主动滚动到底部
- 在 `RunSummary` 中增加模型名显示，使其成为完整的完成元数据行
- 新增 `terminal-win32.ts`：在 Windows conhost 上通过 `SetConsoleMode` 设置 `ENABLE_VIRTUAL_TERMINAL_INPUT` (0x0200) + `ENABLE_MOUSE_INPUT` (0x0010)，使 conhost 将鼠标滚轮事件翻译为 SGR 序列转发给应用（Claude Code / Qwen Code 在 conhost 下滚轮可用的原因正是 stdin 处于 VT 输入模式）
- 在 Windows conhost 下启动时输出 stderr 提示作为兜底（若 VT 输入模式因权限等原因设置失败）

## Impact

- Affected specs: TUI 滚动交互、运行摘要
- Affected code:
  - `packages/cli/src/tui/scroll.ts`（新增）
  - `packages/cli/src/tui/components.tsx`（ConversationTimeline scrollbox、RunSummary）
  - `packages/cli/src/tui/overlays.tsx`（SearchPicker scrollbox）
  - `packages/cli/src/tui/app.tsx`（handleSubmit、selectThread 中增加滚底调用）
  - `packages/cli/src/tui/terminal-win32.ts`（新增，conhost VT 输入模式修复）
  - `packages/cli/src/index.ts`（conhost 检测提示，兜底）
  - `packages/cli/tests/tui/scroll.test.ts`（新增测试）

## ADDED Requirements

### Requirement: 滚轮滚动加速策略

系统 SHALL 为所有 scrollbox 提供滚动加速策略，使鼠标滚轮翻阅历史会话记录流畅自然。

#### Scenario: 默认固定速度

- **WHEN** 用户在对话视图中滚动鼠标滚轮
- **THEN** 每次滚轮事件滚动 3 行（`FixedSpeedScroll(3)`），无论平台或输入设备

#### Scenario: SearchPicker 浮层滚动

- **WHEN** 用户在 SearchPicker 浮层中滚动鼠标滚轮
- **THEN** 使用相同的加速策略

### Requirement: 提交后自动滚底

系统 SHALL 在用户提交消息后主动将对话时间线滚动到底部，确保用户能看到自己的消息和 Agent 的响应。

#### Scenario: 用户提交消息

- **WHEN** 用户在 Composer 中按 Enter 提交消息
- **THEN** 对话时间线立即滚动到底部

#### Scenario: 切换 Thread

- **WHEN** 用户通过 ThreadPicker 选择并恢复一个历史 thread
- **THEN** 对话时间线滚动到底部，显示最新消息

### Requirement: 完成元数据行增强

系统 SHALL 在运行结束后显示包含模型名、耗时和 token 用量的完成元数据行。

#### Scenario: 运行完成

- **WHEN** Agent 运行完成（completed/cancelled/failed）
- **THEN** 时间线末尾显示 `● 已完成 · model-name · 3.2s · ↑1.2k ↓350 · ctx 45k/128k` 格式的元数据行

#### Scenario: 模型名缺失

- **WHEN** 无法获取当前模型名（如 thread 恢复后模型信息读取失败）
- **THEN** 元数据行省略模型名字段，不显示占位符
