# ADR 0003：单一 Interactive Core 与双原生 Renderer 架构

## 状态与背景

- **状态**：已通过 (Accepted) - 2026-08-04
- **决议范围**：Harness CLI / TUI / Web 交互流、高亮引擎与表现层架构

在 ZC-108 至 ZC-115 的演进中，针对交互 Core、TUI 渲染器与内置 Web UI 渲染器的能力边界与解耦方案做出以下终态决策。

## 决策表摘要 (D-01 ~ D-10)

| 决策编号 | 领域 | 架构决策说明 |
| --- | --- | --- |
| **D-01** | Controller 单例 | CLI Composition Root 创建唯一 `InteractiveController` 实例，TUI/Web 共享同一 Core；禁止 UI 侧自行创建或在 Handoff 时销毁。 |
| **D-02** | Web 通信架构 | Browser 不直接连接 Python Agent Host，改为通过 WebSocket UI token 连接 CLI 进程内的 `WebUiGateway` 消费共享 Core 视图。 |
| **D-03** | Host 控制权 | Handoff 只转移 Presentation 输入权，Python 侧 `ControlLease` Holder 始终为 CLI 进程 (stdio Connection)。 |
| **D-04** | Core 模块解耦 | `InteractiveController` 按 9 个 Feature (Run, Timeline, Interaction, Thread, Model, Skill, MCP, Command, Catalog) 拆分。 |
| **D-05** | 依赖倒置 | Interactive Core 零平台/IPC 依赖，所有外部能力（AgentGateway, Clock, Scheduler, IdGenerator 等）抽象为 Port。 |
| **D-06** | IntentOutcome | `dispatch` 统一返回 Typed `IntentOutcome` (`accepted`/`rejected`)，提交失败保留用户输入草稿。 |
| **D-07** | 纯状态化 | 领域状态与 Snapshot 保存语义，不保存平台特定中文文案，由 Presenter / Presentation 负责展示映射。 |
| **D-08** | Web 高亮引擎 | Web 端高亮统一使用 Shiki Worker 单例（`shiki/core` + JS Regex Engine），移除 Oniguruma WASM 与 `'wasm-unsafe-eval'` CSP。 |
| **D-09** | TUI 高亮引擎 | TUI 侧继续保留原生 OpenTUI Tree-sitter 语法高亮，两端不强行共享 Grammar/Token/Renderer 资产。 |
| **D-10** | 共享展示策略 | 建立 `presentation-shared` 抽取跨端纯展示逻辑；Presentation 统一消费 Selector 输出的 `FeatureAvailability`。 |

## 目录与护栏

- `packages/cli/src/presentation-shared/`：跨端纯展示策略与语言目录，绝对零 React/OpenTUI/IPC/DOM 依赖。
- `packages/cli/src/web/syntax/`：Web 高亮服务，使用 `shiki/core` fine-grained imports 打包，运行期零网络请求。
