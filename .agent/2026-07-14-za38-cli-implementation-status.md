# za38-cli 实现状态与开发交接

> 更新时间：2026-07-16
> 当前结论：已交付“OpenTUI → Bun CLI → Python sidecar → OpenAI 兼容网关”的可运行纵向切片，包含真实 AskUser/HITL 恢复；**完整 v0.1 尚未完成**。本文只记录已交付能力与实施交接；未完成任务的唯一来源为 `docs/developer/tasks/`，生成看板见 `docs/developer/任务看板.md`。TUI 复用取舍见 `.agent/2026-07-15-tui-reuse-audit.md`。

## 已实现

### 通讯与运行控制

- 协议已直接升级到 JSON-RPC v2：`initialize` 协商 major/minor 与 capability，版本不兼容会在执行业务方法前拒绝。
- `run.start` 先返回 `thread_id/run_id/accepted` 再创建后台执行；不同 thread 可并发，同一 thread 的第二个活动 run 会被拒绝。
- Agent 流统一为 `event` notification，包含稳定 `event_id/type/thread_id/run_id/sequence/timestamp_ms/payload`；审批和问答改为 Agent→Client 双向 `request`。
- Node `JsonRpcPeer` 保留远端错误 code/data，处理半帧、多帧、跨 chunk UTF-8、写入背压、反向请求和连接关闭清理。
- JSON Schema v2 生成 TypeScript 与 Python Pydantic 模型；共享 fixture 在两端执行，已知 payload 严格拒绝额外字段。
- stdio 帧限制为 8 MiB，工具参数、结果与审批详情限制为 1 MiB并携带截断元数据；Python stdout 仅写 JSONL。
- LangGraph interrupt 仍按原生 interrupt id 恢复；真实 AskUser 与 `write_file` 拒绝路径均有 deepagents 回归覆盖。

### 模型配置与 Agent

- 新增 OpenAI 兼容模型配置：用户 TOML < 工作区 TOML < `ZA38_*` 环境变量 < `--config`。
- 支持模型名、Base URL、API Key 环境变量名、超时、重试、静态 headers 和环境变量 headers；CLI/RPC 展示会脱敏。
- 接入 `langchain-openai` 的 `ChatOpenAI`，已验证标准 OpenAI SSE 流式响应。
- Agent 保留现有 deepagents 文件工具、todo、task、HITL、ask_user、压缩与 QuickJS 组装逻辑；交互模式的写/编辑/删除等高风险工具明确只允许“批准/拒绝”，不会再意外自动批准。
- 修正 Memory/Skills middleware：仅对真实存在的 za38 原生路径启用，避免空环境启动失败。

### 可选远端执行（ZC-008 进行中）

- 执行方式调整为 Qwen Code 风格：默认 `tools.sandbox = false`，继续使用本机 backend，并在 TUI 显示“本机执行 · 未隔离”；`cwd` 只表示默认目录，不宣称为安全边界。
- 新增 `--sandbox`、`ZA38_SANDBOX=true`、`[tools].sandbox` 和 `approval_mode`。只有显式开启时才导入企业远端 `sandbox.factory`；provider 不存在、认证失败或启动失败都会终止该 run，绝不降级为本机 shell。
- `RemoteSandboxSettings` 支持 provider、factory、逻辑工作目录与不含秘密的 `sandbox.params`。工厂必须返回 deepagents `SandboxBackendProtocol` 并负责工作区同步、网络白名单、认证和生命周期。
- 远端模式动态提示逻辑工作目录，禁用会在 Python sidecar 执行的 memory、skills 与 `js_eval`；本机工具环境显式不继承模型 API Key 等父进程环境变量。
- 未交付企业 provider 的具体 API、Docker、Podman、容器镜像、远端同步/回写和企业审计出口；这些依赖企业平台资料，`ZC-008` 保持进行中。

### CLI 与测试

- 新增 `za38` 入口、`--non-interactive`、`--message`、`--json`、`--config`、`--cwd`、`--resume`、帮助与版本输出。
- 已实现 `za38 config show|path`。交互模式基于 `@opentui/react` 重设计为 MiMo-Code 截图风格：使用暖黑画布与 za38 蓝色强调，空会话显示沉浸式首页，首次发送后切换为全宽会话流。
- 首页品牌为 MiMo-Code MIT 字符栅格渲染算法移植的 `HARNESS CODE` 像素字标：保留 shimmer/sweep 动效，使用 za38 蓝色而不复用 MiMo 品牌。`powered by za38` 绝对定位在完整字标右下角。宽终端显示星点、闪烁和 Braille 流星；终端小于 88×28 或 `TERM=dumb` 时自动关闭装饰以保证可读性。
- composer 支持 `/` 或 `Ctrl+P` 命令弹窗，包含真实可用的 `/help`、`/quit`、`/clear`、`/force-clear`、`/version`；支持筛选、方向键、Tab/Enter、Esc 与鼠标选择。宽终端首页与会话菜单均从输入框上方展开；首页会预留菜单行高，避免遮挡 Logo。
- composer 使用 `textarea`，`Enter` 发送、`Shift+Enter` 换行，最多 6 行。`Ctrl+C` 会按优先级清空输入、取消运行或退出；`Esc` 取消运行/关闭菜单，`Ctrl+O` 展开全部工具详情。提示词历史持久化在 `~/.harness/prompt-history.jsonl`：跳过损坏行、连续去重、最多 50 条；`/clear` 只清空当前会话，不清除历史。`↑`/`↓` 仅在真实光标位于 composer 首/尾时回填；空 composer 没有可用历史时才滚动会话，手动编辑不会被全局快捷键抢键。
- 会话流不再使用顶栏和默认侧栏；用户消息、工具、审批和提问均以紧凑的左轨层级呈现。工具流按 provider `index` 关联缺少 `id/name` 的后续参数分片，避免同一次调用拆成 `execute` 与兜底 `tool` 两张卡片；审批 dock 固定保留选项高度，展示动作预览、“允许一次”和“拒绝”，工具或交互阶段不再重复显示 Thinking。composer 左轨由同一容器绘制并在下沿结束，不再产生越界尾线。空 composer 时 `↑`/`↓` 浏览时间线、`PageUp`/`PageDown` 翻半页；有文本时保留 textarea 光标移动。
- Markdown 和代码块使用统一语义色。首版离线内置 Python、JavaScript/TypeScript、Java、Go、C/C++、Bash、HTML/CSS、JSON、YAML、Markdown 与 Zig；HTML 标签/属性以及 HTML 内的 CSS/JavaScript 注入均有语义高亮。其中 WASM parser 和 query 约 6.6 MB，发布时由 Bun 自动复制到 `dist`，运行时不访问 GitHub。资源来源、SHA-256 与许可证见 `packages/cli/src/tui/assets/syntax/manifest.json` 和 `THIRD_PARTY_NOTICES.md`。
- 流 reducer 会拒绝旧 run、重复帧和乱序帧，并按 JSON-RPC event sequence 把用户消息、回答文本、工具和系统通知写入统一 timeline，保留工具所属 run、审批请求详情和最终 usage/duration 供界面渲染。
- OpenTUI 测试渲染器已覆盖 80×24 紧凑首页品牌/底栏与 130×40 会话工具/输入框；通用工具输出采用 OpenCode MIT 的纯函数折叠逻辑，来源 commit 已记录在 `THIRD_PARTY_NOTICES.md`。
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

最后两项会绑定 `127.0.0.1` 临时端口，因此在受限沙箱环境中需要允许 loopback，且不访问外网。2026-07-16 工具流与审批 TUI 修复后已验证：Bun 常规回归 56 通过/1 跳过、Python 17 通过/1 跳过，类型检查、协议生成一致性、构建和项目一致性检查通过。两条 loopback E2E 本轮因沙箱禁止绑定本机端口且提权请求被执行环境拒绝而未复跑；此前 v1 阶段曾验证通过，不能替代 v2 的待验收项。离线 Tree-sitter worker 已实际高亮全部 10 个外置首版语言、HTML 内 CSS/JavaScript 注入，并校验全部资源 SHA-256。

## 关键文件

| 路径 | 责任 |
|---|---|
| `packages/agent/harness_agent/server.py` | 并发 run、JSON-RPC、AskUser/HITL 事件翻译与恢复、配置 RPC |
| `packages/agent/harness_agent/config.py` | TOML/环境变量合并、脱敏与模型配置校验 |
| `packages/agent/harness_agent/execution.py` | 本机默认与企业远端 `SandboxBackendProtocol` 工厂选择、fail-closed 边界 |
| `packages/agent/harness_agent/providers/harness_gateway.py` | OpenAI 兼容 ChatOpenAI adapter |
| `packages/cli/src/tui/app.tsx` | OpenTUI 根界面、输入、流式事件、审批和提问交互 |
| `packages/cli/src/tui/state.ts` | 防乱序的流事件 reducer 与渲染状态 |
| `packages/cli/src/tui/components.tsx` | 首页、星点背景、Logo、全宽会话流、工具左轨、composer、审批/提问 dock 和状态栏 |
| `packages/cli/src/tui/upstream/` | 经许可证审计后适配的 OpenCode/MiMo 通用 UI 纯逻辑；文件内记录精确上游来源 |
| `packages/cli/src/tui/model.ts` | 握手配置摘要、Git 分支、终端降级规则和运行指标格式化 |
| `packages/cli/src/tui/theme.ts` | za38 蓝色强调的暖黑主题与 Markdown/代码块语法样式 |
| `packages/cli/src/tui/harness-logo.tsx` | MiMo 风格字符栅格 Logo、蓝色 shimmer/sweep 与 powered by 定位 |
| `packages/cli/src/tui/syntax-parsers.ts` | 离线 Tree-sitter parser 注册、首版语言清单与诊断入口 |
| `packages/cli/src/tui/assets/syntax/manifest.json` | 随 CLI 分发的 parser/query 来源与 SHA-256 校验清单 |
| `packages/cli/src/index.ts` | sidecar 生命周期、无头执行与 OpenTUI 入口 |
| `packages/cli/src/ipc/client.ts` | v2 双向 JsonRpcPeer、请求超时、反向 request、背压与帧限制 |
| `packages/protocol/schema/v2.json` | 协议唯一 Schema 来源与生成元数据 |
| `packages/protocol/fixtures/v2-contract.json` | TypeScript/Python 共用的有效与无效契约 fixture |

## 待领取任务迁移

此前 P0/P1/P2 待办已迁移为 `ZC-001` 至 `ZC-009` 的独立任务文件，避免本交接文档与任务系统维护两份状态。领取、阻塞、验收与完成状态请查看 [开发任务看板](../docs/developer/任务看板.md)，并按 [开发工作流](../docs/developer/开发工作流.md) 执行。

当前能力的用户说明位于 `docs/user/`，架构与协作说明位于 `docs/developer/`；`.agent/` 继续只服务于实施计划和上下文交接。

## 开发约束

- 已补齐维护中生产源码的中文文件说明、类说明、公开方法说明，以及状态转换、并发、终端兼容和安全边界的关键步骤注释；自动生成资源、第三方离线资产和行为命名测试仅保留来源或用途说明，避免注释噪音。
- 所有必要代码注释和 docstring 使用中文，解释设计意图与安全约束，不能重复代码字面含义。
- 不要向 Python stdout 打印日志；所有 stdout 内容必须是 JSON-RPC JSONL。
- 任何协议修改必须同时修改 TypeScript 类型、Python 行为与至少一条跨进程测试。
- 任何模型/网关改动必须保留 loopback SSE E2E；禁止在测试中使用真实凭证或外网模型。
- 不要把计划中的功能标记为完成，除非有对应实现和测试。
