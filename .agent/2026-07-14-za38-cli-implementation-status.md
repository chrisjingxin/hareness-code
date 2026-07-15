# za38-cli 实现状态与开发交接

> 更新时间：2026-07-15
> 当前结论：已交付“OpenTUI → Bun CLI → Python sidecar → OpenAI 兼容网关”的可运行纵向切片，包含真实 AskUser/HITL 恢复；**完整 v0.1 尚未完成**。本文用于后续领取任务，所有未完成项均以本文为准。

## 已实现

### 通讯与运行控制

- Python `JsonRpcServer` 已从同步阻塞模式改为并发 run 控制面。
- `query` 会先返回 `thread_id/run_id/accepted`，再在后台 task 中流式执行；同一 thread 的并发 query 会被拒绝。
- 已实现 `initialize`、`query`、`cancel`、`respond`、`config.show`、`config.path`、`shutdown`。
- 已实现 `run/started`、`message/delta`、工具事件、`approval/requested`、`question/requested`、`run/completed`、`run/cancelled`、`run/failed` 等 v1 事件；每个 run 带递增 sequence，并保证终态。
- `respond` 使用 LangGraph 原生 interrupt id 映射恢复；真实 `AskUserMiddleware` 与 `write_file` 拒绝路径均有 deepagents 回归覆盖。
- Python stdout 仅写 JSONL；Bun 客户端兼容 Node Buffer 与 Bun `Uint8Array` 子进程输出。

### 模型配置与 Agent

- 新增 OpenAI 兼容模型配置：用户 TOML < 工作区 TOML < `ZA38_*` 环境变量 < `--config`。
- 支持模型名、Base URL、API Key 环境变量名、超时、重试、静态 headers 和环境变量 headers；CLI/RPC 展示会脱敏。
- 接入 `langchain-openai` 的 `ChatOpenAI`，已验证标准 OpenAI SSE 流式响应。
- Agent 保留现有 deepagents 文件工具、todo、task、HITL、ask_user、压缩与 QuickJS 组装逻辑；交互模式的写/编辑/删除等高风险工具明确只允许“批准/拒绝”，不会再意外自动批准。
- 修正 Memory/Skills middleware：仅对真实存在的 za38 原生路径启用，避免空环境启动失败。

### CLI 与测试

- 新增 `za38` 入口、`--non-interactive`、`--message`、`--json`、`--config`、`--cwd`、`--resume`、帮助与版本输出。
- 已实现 `za38 config show|path`。交互模式基于 `@opentui/react` 重设计为 MiMo-Code 截图风格：使用暖黑画布与 za38 蓝色强调，空会话显示沉浸式首页，首次发送后切换为全宽会话流。
- 首页品牌为 MiMo-Code MIT 字符栅格渲染算法移植的 `HARNESS CODE` 像素字标：保留 shimmer/sweep 动效，使用 za38 蓝色而不复用 MiMo 品牌。`powered by za38` 绝对定位在完整字标右下角。宽终端显示星点、闪烁和 Braille 流星；终端小于 88×28 或 `TERM=dumb` 时自动关闭装饰以保证可读性。
- composer 支持 `/` 或 `Ctrl+P` 命令弹窗，包含真实可用的 `/help`、`/quit`、`/clear`、`/force-clear`、`/version`；支持筛选、方向键、Tab/Enter、Esc 与鼠标选择。宽终端首页与会话菜单均从输入框上方展开；首页会预留菜单行高，避免遮挡 Logo。
- composer 使用 `textarea`，`Enter` 发送、`Shift+Enter` 换行，最多 6 行。`Ctrl+C` 会按优先级清空输入、取消运行或退出；`Esc` 取消运行/关闭菜单，`Ctrl+O` 展开全部工具详情。会话内会保存最近 100 条实际发送的提示词，空输入时以 `↑`/`↓` 回填；手动编辑文本时方向键仍用于移动光标，`PgUp`/`PgDn` 浏览会话。
- 会话流不再使用顶栏和默认侧栏；用户消息、工具、审批和提问均以紧凑的左轨层级呈现。composer 左轨由同一容器绘制并在下沿结束，不再产生越界尾线；无文本流期间和底栏运行态均显示动态 Thinking/spinner。空 composer 时 `↑`/`↓` 浏览时间线、`PageUp`/`PageDown` 翻半页；有文本时保留 textarea 光标移动。
- Markdown 和代码块使用统一语义色。首版离线内置 Python、JavaScript/TypeScript、Java、Go、C/C++、Bash、HTML/CSS、JSON、YAML、Markdown 与 Zig；HTML 标签/属性以及 HTML 内的 CSS/JavaScript 注入均有语义高亮。其中 WASM parser 和 query 约 6.6 MB，发布时由 Bun 自动复制到 `dist`，运行时不访问 GitHub。资源来源、SHA-256 与许可证见 `packages/cli/src/tui/assets/syntax/manifest.json` 和 `THIRD_PARTY_NOTICES.md`。
- 流 reducer 会拒绝旧 run、重复帧和乱序帧，并保留工具所属 run、审批请求详情和最终 usage/duration 供界面渲染。
- 已实现 `/help`、`/quit`（`/q`）、`/clear`、`/force-clear`、`/version`。未接入的 Slash Command 不再作为本地功能入口展示或伪造结果，而是交由 Agent 正常处理。
- 新增协议、参数、配置、并发取消、真实 interrupt 恢复、Python stdio、Bun→Python echo 和 Bun→Python→OpenAI mock gateway 测试。
- 根命令 `bun run typecheck`、`bun run test`、`bun run build` 分别检查类型、运行常规回归和构建 CLI。

## 已验证命令

```bash
# 类型检查、常规回归与构建；loopback 测试默认跳过
bun run typecheck
bun run test
bun run build

# Python Agent 的 OpenAI SSE loopback 端到端测试
cd packages/agent
ZA38_RUN_LOOPBACK_E2E=1 .venv/bin/python -m pytest tests/test_gateway_e2e.py -q

# Bun CLI → Python sidecar → OpenAI SSE mock 的端到端测试
cd packages/cli
ZA38_RUN_LOOPBACK_E2E=1 bun test tests/gateway.integration.test.ts
```

最后两项会绑定 `127.0.0.1` 临时端口，因此在受限沙箱环境中需要允许 loopback。它们不访问外网。本次已验证：Bun 常规回归 35 通过/1 跳过、Python 13 通过/1 跳过；Python Agent 与 Bun CLI 两条 loopback E2E 均通过。离线 Tree-sitter worker 已实际高亮全部 10 个外置首版语言、HTML 内 CSS/JavaScript 注入，并校验全部资源 SHA-256。另以 80×24 与 130×40、`TERM=xterm-256color` 伪终端完成首页星空/闪烁、上方 `/` 菜单、Enter 发送、`/clear` 回首页、空输入的上下/分页浏览、Ctrl+C 语义和 Harness Code 品牌烟测。另新增 RGBA 归一化插值回归，防止 Logo 或星空动画再次退化为近黑色；以本地 echo Agent 验证发送两条提示词后 `↑` 回填最新、`↓` 清空的真实终端行为。

## 关键文件

| 路径 | 责任 |
|---|---|
| `packages/agent/za38_agent/server.py` | 并发 run、JSON-RPC、AskUser/HITL 事件翻译与恢复、配置 RPC |
| `packages/agent/za38_agent/config.py` | TOML/环境变量合并、脱敏与模型配置校验 |
| `packages/agent/za38_agent/providers/za38_gateway.py` | OpenAI 兼容 ChatOpenAI adapter |
| `packages/cli/src/tui/app.tsx` | OpenTUI 根界面、输入、流式事件、审批和提问交互 |
| `packages/cli/src/tui/state.ts` | 防乱序的流事件 reducer 与渲染状态 |
| `packages/cli/src/tui/components.tsx` | 首页、星点背景、Logo、全宽会话流、工具左轨、composer、审批/提问 dock 和状态栏 |
| `packages/cli/src/tui/model.ts` | 握手配置摘要、Git 分支、终端降级规则和运行指标格式化 |
| `packages/cli/src/tui/theme.ts` | za38 蓝色强调的暖黑主题与 Markdown/代码块语法样式 |
| `packages/cli/src/tui/harness-logo.tsx` | MiMo 风格字符栅格 Logo、蓝色 shimmer/sweep 与 powered by 定位 |
| `packages/cli/src/tui/syntax-parsers.ts` | 离线 Tree-sitter parser 注册、首版语言清单与诊断入口 |
| `packages/cli/src/tui/assets/syntax/manifest.json` | 随 CLI 分发的 parser/query 来源与 SHA-256 校验清单 |
| `packages/cli/src/index.ts` | sidecar 生命周期、无头执行与 OpenTUI 入口 |
| `packages/cli/src/ipc/client.ts` | JSONL 客户端、请求超时与帧解析 |
| `packages/protocol/schema/v1.json` | 协议 Schema 来源；当前尚未接入运行时验证 |

## 待领取任务

### P0：完整可用性

1. **OpenTUI 完整体验**
   - 当前已接入官方 `@opentui/react`，并完成 Harness Code 品牌首页、窄终端降级、动态星空/流星、全宽消息流、离线 Markdown/代码块渲染、工具左轨、Thinking 动效、运行状态栏、审批菜单、单题 ask_user、`/help`、`/clear`、`/force-clear`、`/quit`、`/version`。
   - 待补多题 ask_user 表单、线程/Agent 选择器、复制快捷键，以及基于未来会话/MCP/技能 RPC 的动态状态信息。
   - `/threads`、`/compact`、`/mcp`、`/tools`、`/reload`、`/remember`、`/skill:<name>`、`/agents` 尚无后端 RPC；当前不会在本地命令或快捷键中伪造这些功能。

2. **安全边界**
   - 已以真实 deepagents fixture 验证 AskUser 和 `write_file` HITL 的 `Command(resume={interrupt_id: ...})` 恢复格式，并确认拒绝写入不落盘。
   - 对文件工具增加真实路径与 symlink 工作区边界检查；shell allowlist 目前不是 OS 级隔离，需加入危险语法/路径策略和回归测试。
   - 将现有 Unicode 安全模块接入工具审批和 TUI 展示。

3. **会话与上下文**
   - 集成 `AsyncSqliteSaver`、线程 metadata、恢复和 `za38 threads list`；当前 Agent 默认使用内存 checkpointer。
   - 集成 LocalContext、稳定 usage 累计和 compact 状态。

### P1：保留能力的完整实现

4. **原生子 Agent、记忆与技能**
   - 实现 `~/.za38` 与 `<workspace>/.za38` 的 agents/skills/AGENTS.md 发现、优先级、frontmatter 校验与 list RPC。
   - 实现 `/remember`、`/skill:<name>`、`za38 agents list`、`za38 skills list`。

5. **MCP**
   - 迁移 stdio/SSE/HTTP 配置发现、环境变量插值、工具过滤、session manager。
   - 实现项目 MCP 指纹信任、OAuth 流程、`/mcp` 与 `za38 mcp list`。

6. **协议加固**
   - 将 `packages/protocol/schema/v1.json` 接入两端运行时校验并补充共享 fixture。
   - 增加 heartbeat、工具参数/结果截断、事件持久化与 child exit 诊断。

### P2：发行与企业扩展

7. **跨平台打包和安装器**
   - 构建携带 Bun、OpenTUI native 库和 Python sidecar 的 darwin/linux/windows 平台包。
   - 实现 shell、PowerShell、CMD 安装器、签名 manifest、SHA-256、原子升级与 CI 平台冒烟测试。
   - 当前 `langchain-openai`、QuickJS、MCP adapter 已写入 `pyproject.toml`，开发虚拟环境已安装；尚无锁文件、wheelhouse 或 frozen sidecar。首版 Tree-sitter parser 已随 CLI 源码离线分发并校验 hash；跨平台发行时仍需确认 npm 平台包会携带 `dist` 下的 WASM/query 资源，并完成安装后离线烟测。

8. **企业沙箱 adapter**
   - 按 `ExecutionBackend` 抽象接入企业 sandbox，接管 shell、文件同步和 diff 回写。
   - 在此之前，文档必须持续声明本地 shell 仅为尽力限制。

9. **后续明确延期功能**
   - goal/rubric、审计/hooks、自动更新/doctor、主题/通知/剪贴板/编辑器/媒体、web search/fetch、ACP、za38 垂域代码生成。

## 开发约束

- 所有必要代码注释和 docstring 使用中文，解释设计意图与安全约束，不能重复代码字面含义。
- 不要向 Python stdout 打印日志；所有 stdout 内容必须是 JSON-RPC JSONL。
- 任何协议修改必须同时修改 TypeScript 类型、Python 行为与至少一条跨进程测试。
- 任何模型/网关改动必须保留 loopback SSE E2E；禁止在测试中使用真实凭证或外网模型。
- 不要把计划中的功能标记为完成，除非有对应实现和测试。
