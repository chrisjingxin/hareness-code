# za38-cli 实现状态与开发交接

> 更新时间：2026-07-14
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
- 已实现 `za38 config show|path`。交互模式已改为 `@opentui/react`：消息流、工具卡片、状态栏、输入框、Ctrl+C 取消、审批选择和单题 ask_user 选择/文本回答均可用；流 reducer 会拒绝旧 run、重复帧和乱序帧。
- 已实现 `/help`、`/quit`（`/q`）、`/clear`、`/force-clear`、`/version`；其余计划内 Slash Command 会明确提示尚未连接对应内核，避免伪造成功。
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

最后两项会绑定 `127.0.0.1` 临时端口，因此在受限沙箱环境中需要允许 loopback。它们不访问外网。本次已验证：常规 Bun 12 通过/1 跳过，Python 13 通过/1 跳过；两条 loopback E2E 均通过。

## 关键文件

| 路径 | 责任 |
|---|---|
| `packages/agent/za38_agent/server.py` | 并发 run、JSON-RPC、AskUser/HITL 事件翻译与恢复、配置 RPC |
| `packages/agent/za38_agent/config.py` | TOML/环境变量合并、脱敏与模型配置校验 |
| `packages/agent/za38_agent/providers/za38_gateway.py` | OpenAI 兼容 ChatOpenAI adapter |
| `packages/cli/src/tui/app.tsx` | OpenTUI 根界面、输入、流式事件、审批和提问交互 |
| `packages/cli/src/tui/state.ts` | 防乱序的流事件 reducer 与渲染状态 |
| `packages/cli/src/index.ts` | sidecar 生命周期、无头执行与 OpenTUI 入口 |
| `packages/cli/src/ipc/client.ts` | JSONL 客户端、请求超时与帧解析 |
| `packages/protocol/schema/v1.json` | 协议 Schema 来源；当前尚未接入运行时验证 |

## 待领取任务

### P0：完整可用性

1. **OpenTUI 完整体验**
   - 当前已接入官方 `@opentui/react`，并完成消息流、工具卡片、状态栏、审批菜单、单题 ask_user、`/help`、`/clear`、`/force-clear`、`/quit`、`/version`。
   - 待补 Markdown/代码块渲染、多题 ask_user 表单、线程/Agent 选择器，以及工具详情的参数截断和展开。
   - `/threads`、`/compact`、`/mcp`、`/tools`、`/reload`、`/remember`、`/skill:<name>`、`/agents` 已被识别但尚无后端 RPC，当前会显示明确的未接入提示。

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
   - 当前 `langchain-openai`、QuickJS、MCP adapter 已写入 `pyproject.toml`，开发虚拟环境已安装；尚无锁文件、wheelhouse 或 frozen sidecar。

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
