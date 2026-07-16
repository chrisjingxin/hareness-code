# Harness Code TUI 复用审计

> 审计日期：2026-07-15
> 范围：仅 OpenTUI 表现层；Python Agent 与 JSON-RPC 不在本次变更范围。

## 结论

不复制 MiMo-Code 或 OpenCode 的整层 TUI，也不迁移当前 React 根组件到 Solid。Harness Code 保持 React 19 + `@opentui/react` 0.4.3，并采用“组件级适配、算法级复用”。

| 项目 | 框架 | OpenTUI | TUI 规模 |
| --- | --- | --- | --- |
| Harness Code | React | 0.4.3 | 约 2 千行 |
| MiMo-Code | Solid | 0.1.101 | 约 2.7 万行 |
| OpenCode | Solid | 0.4.3 | 约 2.7 万行 |

MiMo/OpenCode 的 App、Prompt、Session 均依赖 SDK、Sync、Route、Project、Plugin 和结构化 Tool Part。当前协议只提供文本流与通用工具事件，整层复制会引入一套不能工作的状态系统，也会造成一次 React→Solid 的无收益重写。

## 当前策略

- 保留 `IpcClient → TuiState → React View` 边界，Agent 与协议不变。
- 通用交互以 OpenCode 0.4.3 为上游；Logo、星空等纯视觉算法以 MiMo-Code 为上游。
- 直接移植内容放在 `packages/cli/src/tui/upstream/`，每个文件标记来源路径、commit 与 MIT 许可；总归属记录在 `THIRD_PARTY_NOTICES.md`。
- 离线 Tree-sitter parser、hash 与 HTML injection 保留 Harness 实现，不能回退到上游运行时网络下载。

## 已落实

- 消息和工具按 JSON-RPC sequence 进入统一 timeline，工具不再堆到回答末尾。
- textarea 在真实光标边界处理 `↑/↓` 历史与空输入滚动；全局快捷键不再抢方向键。
- 历史写入 `~/.harness/prompt-history.jsonl`，支持损坏行自愈、连续去重和 50 条上限。
- 已适配 OpenCode 的通用工具输出折叠逻辑，并新增 OpenTUI 测试渲染器覆盖。
- 根级 ErrorBoundary 提供安全的降级画面，并支持 `Ctrl+C`、`Ctrl+D`、`Esc` 退出。

## 后续优先级

1. 真实 composer anchor 的 Slash 菜单定位、滚动加速与 sticky-scroll 回归。
2. 将星空/Logo 动画改为 renderable 的命令式更新，避免 React 高频全树重渲染。
3. 选择/复制、bracketed paste、IME 末字符提交保护。
4. 协议未来提供结构化工具 input/diff 后，再评估专项 Tool View；此前不复制 OpenCode 的 SDK 视图。
