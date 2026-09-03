# dcode 与 Harness Code 功能差距调研

## 1. 结论摘要

本次对比固定在以下源码快照：

- dcode：`/Users/zhangjingxin/Code/OpenSource/deepagents`，`deepagents-code 0.1.65`（`libs/code/pyproject.toml:5-8`），commit `03436b369c0324498602fe6b7918cf36f3629d76`；工作区只有未跟踪的 `.codex/`，未参与判断。
- Harness Code：本仓库 commit `bccc6398906478c2c0816a2f3debd9ffe8487a0b`；工作区已有与本调研无关的 `docs/developer/project/新功能候选.md` 修改，本报告未触碰。
- 调研日期：2026-09-02。

结论不是“dcode 有 45 个 Slash Command、Harness 只有 19 个，所以缺 26 个”。命令数量会把入口差异误算成功能差异。例如 dcode 的 `/context`、`/tokens` 在 Harness 中由 `/status` 和项目检查器覆盖；dcode 的 `/manual`、`/auto`、`/yolo` 在 Harness 中由 `Shift+Tab` 五档审批覆盖；`/remember` 背后的跨会话记忆在 Harness 已有 `memory_save/memory_search`。

源码级反证后，确认 dcode 仍领先的主要功能有 22 组：

| 优先级 | dcode 有、Harness 缺失或明显较弱的能力 | Harness 相近能力 | 判断 | 置信度 |
| --- | --- | --- | --- | --- |
| 高 | 可安装成品、自更新与可选依赖安装 | 仅源码开发启动 | 缺失 | 高 |
| 高 | 多原生模型 Provider 与统一认证 UI | OpenAI-compatible Profile | 明显较弱 | 高 |
| 高 | 六个内置远端 sandbox + 第三方 provider 市场 | 企业自供一个 factory 抽象 | 明显较弱 | 高 |
| 高 | MCP OAuth 登录、刷新、GitHub/Slack 专用流程 | 静态 Header/env 认证 | 缺失 | 高 |
| 中 | MCP user/project 配置自动发现、TUI 禁用/重连 | 显式配置 + Plugin 内 `.mcp.json` | 明显较弱 | 高 |
| 高 | ACP 标准服务端 | 自有 JSON-RPC v3 TUI/Web 协议 | 缺失 | 高 |
| 高 | Headless/CI 的 stdin、quiet、buffer、turn/timeout 控制 | 单消息 + text/JSON | 明显较弱 | 高 |
| 高 | 可切换主 Agent/persona | Plugin Agent 仅用于受控委派 | 缺失 | 高 |
| 高 | 持久 Goal + Rubric 自动评测闭环 | Compose 有目标/验收/人工检视 | 部分相近但不等价 | 高 |
| 中 | 线程成本估算、告警与上下文注入审计 | Token/缓存/本地日志 | 明显较弱 | 高 |
| 中 | 独立摘要模型与会话内 reasoning effort 调整 | 当前 Profile 同时承担摘要；effort 只在 TOML | 明显较弱 | 高 |
| 中 | 用户/项目级全生命周期 Hooks | Plugin 受控 Hooks，仅 4 个事件 | 明显较弱 | 高 |
| 中 | 任意 Python extension 注册 middleware/tool/storage route | 声明式 Plugin 组件 | 缺失（dcode 侧仍实验性） | 高 |
| 中 | Plugin marketplace、远程源与更新 | Plugin 仅本地目录/ZIP；Skill 有市场接口 | 明显较弱 | 高 |
| 中 | 远程 managed config 与分级配置 Provider | 用户配置 + 显式配置 + 未来 lock seam | 缺失 | 高 |
| 中 | 图片/视频剪贴板与拖放直接作为消息附件 | `@` 路径 + Agent 后续 `read_file` 图片 | 明显较弱 | 高 |
| 中 | 内置 JS interpreter / Programmatic Tool Calling | Shell + 普通工具调用 | 缺失 | 高 |
| 中 | 可配置的远端异步 Subagent | 本机 Inline/Managed Agent 与 Team | 缺失 | 高 |
| 中 | LangSmith 在线 tracing 与一键打开 Thread | 本地脱敏 JSONL + `harness logs` | 部分相近但不等价 | 高 |
| 低 | `!`/`!!` 直接 Shell 与隐身输出模式 | 必须让 Agent 调 Shell | 缺失 | 高 |
| 低 | Skill create/delete 与 bundled 自维护 Skill | Skill 安装/更新/启停 + memory 工具 | 明显较弱 | 高 |
| 低 | Thread 删除/跨 cwd 恢复、Prompt 搜索、外部编辑器、通知中心、TUI 主题/时间戳等便利项 | 恢复选择器、方向键历史、文本选中复制、Web 明暗主题 | 多项缺失或较弱 | 高 |

如果后续要转成候选 Task，建议先处理“交付安装 → Provider/Auth → MCP OAuth → 内置 sandbox → Headless/ACP”这条产品可用性链，不建议先追平主题、时间戳或 Slash Command 数量。

## 2. 判定口径

只把用户或管理员能观察到的能力计为功能：能否安装、登录、选择运行主体、隔离执行、接入 IDE、控制 CI、管理上下文和扩展行为。以下不计为功能缺失：

- 同一能力采用 Textual、OpenTUI、JSON-RPC 或 LangGraph middleware 的实现差异；
- dcode 用 Slash Command、Harness 用快捷键/侧栏/CLI 子命令的入口差异；
- Harness 为安全、审计或企业边界有意采用更严格语义；
- 只有类名、配置字段或死代码，但没有生产调用链的“纸面能力”。

每一项都同时找 dcode 正向证据和 Harness 反证/相近能力。仓库范围搜索不到只作为辅助，不单独证明缺失。

## 3. 逐项差距

### 3.1 可安装成品、自更新与可选依赖安装

**dcode 能力。** 官方 README 给出一条 `curl` 安装命令，安装后直接运行 `dcode`；运行中可 `/update`、`/auto-update`、`/install`、`/uninstall`，CLI 也提供相同入口。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/README.md:12-27`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:312-334`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2823-2861`。
- Harness 反证：`README.md:5` 明确说明当前只能从源码运行，跨平台安装包和安装器尚未交付；`docs/user/快速开始.md:46` 明确把安装器、自动更新和预编译包列为待交付。

**判断。** 完整缺失，且会阻断非仓库开发者使用。置信度：高。

### 3.2 多原生模型 Provider 与统一认证

**dcode 能力。** dcode 声明 Anthropic、Bedrock、Cohere、DeepSeek、Google GenAI、Groq、Ollama、OpenAI、OpenRouter、Vertex、xAI 等 22 个 provider extra，并提供 `/auth` 管理 Provider/服务凭据；`openai_codex` 还实现 ChatGPT OAuth PKCE 登录。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/pyproject.toml:108-138`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:111-112`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/integrations/openai_codex.py:1-2,276-347`。
- Harness 相近能力：Harness 有可切换、可审计的 Model Profile 和 `/model` Picker，但 `docs/user/模型配置.md:3,62` 明确只支持 `provider = "openai-compatible"`，不启用 Anthropic/Gemini 等原生 SDK；凭据通过 TOML/env 提供，没有统一登录 UI。

**判断。** Harness 的 OpenAI-compatible 网关能间接覆盖多个模型，不等于 provider 原生协议、认证和能力适配。明显较弱。置信度：高。

### 3.3 内置远端 sandbox 生态、复用与快照

**dcode 能力。** `--sandbox` 内置 AgentCore、Daytona、LangSmith、Modal、Runloop、Vercel，并接受第三方 entry point 和配置声明的 provider；还公开 sandbox ID 重连、snapshot/blueprint 和 setup script。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2715-2752`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/integrations/sandbox_registry.py:38-75`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/pyproject.toml:138-149`。
- Harness 相近能力：Harness 已有 fail-closed 的 `SandboxBackendProtocol` factory 和 `--sandbox`，但要求企业先提供插件与 factory；没有任何可直接安装的 provider，也没有 CLI 暴露 sandbox ID、快照或 setup。证据：`docs/user/安全与沙箱.md:154-183`；`packages/cli/src/args.ts:381-390`。

**判断。** 不是“完全没有 sandbox”，而是只有抽象层、没有开箱即用生态和生命周期操作。明显较弱。置信度：高。

### 3.4 MCP OAuth 登录与刷新

**dcode 能力。** `dcode mcp login` 能列出需要认证的 server 并执行 OAuth；有持久 token storage、刷新协调、通用 Provider，以及 GitHub Device Flow 与 Slack 特化流程。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/client/commands/mcp.py:56-76,91-192,215-285`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/mcp_auth.py:1-3,95-96,306-379`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/mcp_providers/_registry.py:10-29`。
- Harness 相近能力：Harness 支持 stdio、HTTP、SSE MCP 的增删、热连接与静态 Header/env 展开；`packages/agent/harness_agent/extensions/mcp.py:976-1001` 只构造 Header，没有 OAuth/token 生命周期；`packages/cli/src/interactive/commands.ts:221` 的 `/mcp` 也没有 login 子命令。

**判断。** 完整缺失。静态 Bearer Header 不等于 OAuth 登录、刷新和重新认证。置信度：高。

### 3.4.1 MCP 自动发现、禁用与重连

**dcode 能力。** dcode 自动发现 user/project `.mcp.json`，兼容 Claude Desktop JSON，显式 `--mcp-config` 只作为最高优先级覆盖；MCP Viewer 能显示工具 schema 和 `ok/unauthenticated/awaiting_reconnect/error/disabled` 状态，并可在 TUI 禁用、重新启用或重新认证单个 server。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/mcp_tools.py:1-7,68-109,113-160`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2758-2773`。
- Harness 相近能力：Harness `/mcp` 可显示状态、增删和热连接显式 server；Plugin Adapter 能读取已安装 Plugin 内的 Claude `.mcp.json`，证据：`docs/user/交互使用.md:121-127`、`packages/agent/harness_agent/plugins/claude.py:243-262`。但不会自动信任/发现工作区根部或用户目录的 MCP 配置，也没有单 server disable/reconnect 交互。

**判断。** MCP 核心 transport 和管理已有；差距是配置发现、项目 trust 与故障恢复 UI。明显较弱。置信度：高。

### 3.5 ACP 标准服务端

**dcode 能力。** `--acp` 直接把 dcode 作为 ACP stdio server，并已覆盖 ACP 审批模式与会话持久配置。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2868-2872`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/CHANGELOG.md:182,218`。
- Harness 相近能力：Harness 有稳定的自有 JSON-RPC v3，服务 TUI、无头 CLI 和本机 Web；架构证据：`docs/developer/architecture/架构总览.md:6-15,75-87`。生产 `packages/` 中没有 ACP 入口，现有文档只在未来 IDE/ACP 场景中提及。

**判断。** 完整缺失。自有协议功能更强不代表能被 ACP 客户端直接驱动。置信度：高。

### 3.6 Headless/CI 执行控制

**dcode 能力。** 除单任务外，还支持管道 stdin、`--stdin`、纯净 stdout、禁用流式、最大 agent turn、硬超时（超时 exit 124）、递归上限、模型参数/Profile 临时覆盖、启动命令、启动 Skill，以及按本次 invocation 收紧 filesystem tool 与 Shell allowlist。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2493-2689,2749-2757,2811-2820`，特别是 `:2566-2640` 与 `:2687-2689`。
- Harness 相近能力：`packages/cli/src/args.ts:55-66` 的非交互运行只接受一条必填消息和 `--json`；`packages/cli/src/index.ts:425-432` 执行单个 Run。没有 stdin 管道、quiet/no-stream、turn 或 wall-clock timeout 入口。

**判断。** 基础无头能力已有，但 CI 的可控性和组合性明显较弱。置信度：高。

### 3.7 可切换主 Agent/persona

**dcode 能力。** `/agents` 是“浏览并切换可用 Agent”，启动也可 `--agent NAME` 选择主 Agent。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/COMMANDS.md:15`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2480-2490`。
- Harness 相近能力：Harness 已支持内置/Plugin Agent 和 Managed/Inline 委派，但 `/agents` 只是子代理浏览器。证据：`docs/user/交互使用.md:5,129`；`packages/cli/src/interactive/commands.ts:219`。`docs/user/模型配置.md:64-66` 明确主 Agent 不能由普通配置替换，Plugin Agent 是委派角色。

**判断。** 自定义子代理已交付，不能再沿用旧报告的“没有自定义 Agent”；真正差距是用户不能把它选作根 Agent/persona。置信度：高。

### 3.8 持久 Goal 与 Rubric 自动验收闭环

**dcode 能力。** `/goal` 管理持久目标和验收条件，`/rubric` 设置显式标准；无头 `--rubric` 会让 grader 自评并循环到满足，支持独立 grader model 和最大迭代数。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:155-164,274-279`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2643-2678`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/goal_rubric.py:116-158`。
- Harness 相近能力：Compose 会把目标、规格、计划、Todo 和检视持久化，并在最终由用户确认；证据：`docs/user/交互使用.md:77-88`。这不是任意 Build Thread 上的持续 Goal，也没有一个按 Rubric 自动评分并循环执行的运行时。

**判断。** 两者都处理“目标/验收”，但交互契约不同。Harness 的 Compose 更强调文档与人工门禁；dcode 的 Goal/Rubric 更适合长任务和 CI 自动闭环。部分相近但功能仍缺失。置信度：高。

### 3.9 成本估算、阈值告警与上下文注入审计

**dcode 能力。** `/cost` 显示 Thread 累计美元成本并按请求类型/模型拆分；运行时持久化 `session_cost`。`/context-doctor` 分项估算 fresh session 中系统提示、工具、Skill、MCP 等注入 token，并与 live usage 对照；通知中心可配置成本等告警。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:137-144,211-214`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/cost_tracking.py:1-5,100-109`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/context_doctor.py:188-255`。
- Harness 相近能力：每轮显示 token usage，`/status` 展示 Token 水位与缓存命中率，本地诊断日志记录 input/output/cached tokens；证据：`docs/user/交互使用.md:19,71,119`；`packages/agent/harness_agent/diagnostic_log/contract.py:27-29`。仓库没有价格表、美元估算、成本阈值或上下文来源分项审计入口。

**判断。** Token 可观察性已有；“花了多少钱”和“固定上下文是谁注入的”仍缺。明显较弱。置信度：高。

### 3.10 独立摘要模型与会话内 reasoning effort

**dcode 能力。** `/summarization-model` 可单独选择上下文压缩模型；`/effort` 可调整当前模型 reasoning effort，CLI 也可逐次覆盖。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:173-174,204-205`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2493-2526`。
- Harness 相近能力：Harness 的压缩器绑定当前 AgentEngine Profile 的摘要模型，证据：`packages/agent/harness_agent/threads/context_compaction.py:160-165`；reasoning effort 已支持，但只能写入 Model Profile TOML，证据：`docs/user/模型配置.md:58`。`/model` 能通过换整个 Profile 间接切 effort，不能在现有 Profile 上临时调整。

**判断。** 明显较弱。独立摘要模型可用便宜/长上下文模型降低压缩成本；会话内 effort 则是便利性和成本控制。置信度：高。

### 3.11 用户/项目级全生命周期 Hooks

**dcode 能力。** 用户、项目和 Plugin 都可声明 Hook；覆盖 SessionStart、UserPromptSubmit、SessionEnd、PermissionRequest、Notification、Pre/PostToolUse、PreCompact、Stop、SubagentStart/Stop 共 12 类事件，Hook 可阻断、补上下文或影响权限决定。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/HOOKS.md:7-41,51-62,127-135`。
- Harness 相近能力：Harness 已安全适配 Plugin command Hook，但只接受 `PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`SubagentStop` 四类，且通用 `[hooks]` TOML 仍标为 planned。证据：`packages/agent/harness_agent/plugins/runtime.py:70-72`；`packages/agent/harness_agent/config/config_manifest.py:67`；`docs/user/插件管理.md:14-15,324-330`。

**判断。** 不能写成“Harness 没有 Hooks”；准确结论是作用域只到受信 Plugin，事件矩阵也明显较窄。置信度：高。

### 3.12 任意 Python extension API

**dcode 能力。** 实验性 extension 可注册 LangChain middleware、模型工具、虚拟 Backend route 和 shutdown callback；来源可来自用户目录、项目目录、临时 `-e`、Plugin 或 Python entry point。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/EXTENSIONS.md:1-4,35-54,84-112`。
- Harness 相近能力：Harness Plugin 支持 Skill、Command、MCP、Agent、Policy、Team、受控 Hook、stdio LSP 和 Monitor；证据：`docs/user/插件管理.md:23-33,51-54`。没有执行任意 Python module 并注入 middleware/tool/backend route 的公开 extension seam。

**判断。** 完整缺失，但 dcode 也明确标为实验能力并要求显式环境开关。对 Harness 而言这是扩展自由度差距，同时也是主动缩小任意代码执行面的安全取舍。置信度：高。

### 3.13 Plugin marketplace、远程源与更新

**dcode 能力。** Plugin CLI 可增删 marketplace、列出可用 Plugin、安装/卸载/启停；marketplace source 接受 GitHub shorthand、Git URL、HTTPS JSON、文件或目录，并有缓存和自动更新路径。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/plugins/commands_cli.py:11-19,52-89`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/plugins/marketplace.py:49-51,126-213`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:187-190`。
- Harness 相近能力：Harness Plugin 可以校验、复制安装、启停、设置和删除，但来源仅本地目录/ZIP，不扫描远程市场；证据：`docs/user/插件管理.md:3,56-65`；`packages/agent/harness_agent/plugins/store.py:99-121`。Harness 的 Skill 另有 market provider 接口，不应误算为 Plugin marketplace 已交付。

**判断。** Plugin 本体已有，发现、分发和更新链明显较弱。置信度：高。

### 3.14 远程 managed config 与配置 Provider

**dcode 能力。** dcode 支持操作系统级 `managed_config.toml`，也可把它作为 HTTPS remote policy descriptor；配置解析使用有 rank 的 provider，并对远端策略做独立健康检查。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/configuration/paths.py:103-145`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/configuration/service.py:688-739,941-962`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/configuration/resolver.py:140-153`。
- Harness 相近能力：Harness 有来源优先级、原子写和 `ManagedConfigLock` 注入 seam，但当前只读取用户配置和显式配置，项目可信机制也未交付；证据：`docs/user/模型配置.md:3`；`docs/developer/architecture/配置架构.md:64,91`；`packages/agent/harness_agent/config/config_change_service.py:114-123`。

**判断。** 对企业集中策略管理是实质差距。置信度：高。

### 3.15 图片/视频直接附件

**dcode 能力。** TUI 能跟踪粘贴/拖入的图片和视频，显示 placeholder，实际按结构化 multimodal content block 发送；媒体大小上限为 20 MiB。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/input.py:1,59-70,109-143`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/media_utils.py:1,53-65,182-218`。
- Harness 相近能力：Harness Model Profile 可声明 vision；用户可 `@` 图片路径，图片不会内联，只把路径交给 Agent 后续读取，证据：`docs/user/模型配置.md:35,62`、`docs/user/交互使用.md:31-42`。Agent 的 `read_file` 对图片会返回 multimodal block，证据：`packages/agent/harness_agent/tools/snapshot_file_contract.py:268-273`。但 Composer 没有剪贴板/拖放媒体 attachment，也没有视频消息链。

**判断。** 图片理解并非完全缺失；缺的是直接、多媒体、一次消息绑定的输入体验，视频尤其缺失。置信度：高。

### 3.16 JS interpreter / Programmatic Tool Calling

**dcode 能力。** 本机默认启用 `js_eval` middleware，并用 `--interpreter-tools` 控制它可编排的 safe/all/显式工具集合；写/Shell 工具需要额外确认。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/main.py:2796-2813`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/agent.py:2407-2408,2515-2532,2986-3008`。
- Harness 相近能力：Harness 有 Shell、`tool_search`、普通工具调用和子代理，但公开工具表只增加 web、LSP、tool search、memory、plan 等工具；证据：`packages/agent/harness_agent/tools/harness_tools.py:73-212`。生产源码没有 `js_eval`/PTC middleware。

**判断。** 完整缺失。它不是 Shell 的同义词：PTC 允许模型在一次程序执行中组合受限工具并处理中间结果，减少模型往返。置信度：高。

### 3.16.1 远端异步 Subagent

**dcode 能力。** `[async_subagents.*]` 可声明远端 URL、graph ID、Header 和说明，主 Agent 将其作为异步委派工具；TUI 有独立 Subagent Panel 查看运行。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/agent.py:1108-1129,1161-1188`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/app.py:3126-3128,16087-16089`。
- Harness 相近能力：Harness 有本机 Inline/Managed Agent、并行 Team 和独立子时间线，证据：`docs/user/交互使用.md:5,16,129-137`。Agent Catalog 没有远端 graph URL/headers 形状，Team worker 也在当前 Host 执行。

**判断。** 完整缺失。它适合把专用 Agent 部署为独立服务，但会新增远端认证、数据出境、取消和所有权边界。置信度：高。

### 3.17 LangSmith 在线 tracing

**dcode 能力。** dcode 把同一轮多次模型请求归入共享 trace metadata，支持 LangSmith 多副本/端点，并用 `/trace` 直接打开当前 Thread。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/_tracing.py:16-40`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/command_registry.py:251-252`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/config.py:666-770`。
- Harness 相近能力：Harness 有脱敏、本地、可按 Thread/Run 查询的 JSONL 诊断日志；证据：`docs/user/日志与诊断.md:83-174`、`docs/user/故障排查.md:243`。没有远端 trace backend 或一键在线查看入口。

**判断。** 本地可观测性不弱，但缺外部 APM/协作分析集成。部分相近但不等价。置信度：高。

### 3.17.1 `!` / `!!` 直接 Shell

**dcode 能力。** 用户输入 `!command` 可绕过模型直接在 TUI 后台执行，并把输出放进下一次模型上下文；`!!command` 同样执行和渲染，但明确不进入模型上下文，适合检查含敏感或无关噪声的本地状态。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/config.py:1459-1471`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/app.py:12270-12316,12402-12409`。
- Harness 相近能力：Agent 能通过受控 Shell 工具执行命令，但 Interactive Registry 和输入分发没有 `!`/`!!` 直出语法；`packages/cli/src/interactive/commands.ts:208-228` 是当前静态入口全集。

**判断。** 完整缺失。它减少简单命令的模型往返，`!!` 还提供“不写进上下文”的显式隐私语义。置信度：高。

### 3.17.2 Skill 自助创建、删除与内置维护 Skill

**dcode 能力。** `skills list/create/info/delete` 可选择 user/project scope；TUI 内置 `skill-creator`、`remember` 和只读 `deepagents-thread-inspector`，分别帮助生成 Skill、把经验写入 AGENTS/Skill、检查本地 Thread checkpoint。

- dcode 证据：`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/skills/commands.py:998-1150`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/built_in_skills/skill-creator/SKILL.md:1-22`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/built_in_skills/remember/SKILL.md:1-47`；`/Users/zhangjingxin/Code/OpenSource/deepagents/libs/code/deepagents_code/built_in_skills/deepagents-thread-inspector/SKILL.md:1-35`。
- Harness 相近能力：Harness 可 list/inspect/enable/disable/trust/install/update Skill，并已有 `memory_save/memory_search`；证据：`packages/cli/src/args.ts:69-113`、`packages/agent/harness_agent/tools/harness_tools.py:172-201`。当前 bundled catalog 只有工程流程 Skill，没有上述自维护工作流；创建和删除仍需手工管理目录。

**判断。** 底层能力部分存在，缺自助入口与 bundled workflow。明显较弱。置信度：高。

### 3.18 Thread 管理与 TUI 便利能力

这些能力分别存在，不应只因都很小就说成一个模糊的“UI 不如 dcode”。

| dcode 功能 | dcode 证据 | Harness 相近能力与差距 |
| --- | --- | --- |
| CLI 列出/筛选/删除 Thread；恢复不同 cwd 的 Thread 时可切目录 | `deepagents_code/main.py:2301-2374`；`deepagents_code/tui/widgets/cwd_switch.py:1-44,157-172` | `/resume` 只浏览当前 project，不能按 ID 删除或跨 project/cwd 恢复；`docs/user/交互使用.md:231-233` 只提供手动删除整个 SQLite |
| 搜索并复用历史 Prompt | `deepagents_code/command_registry.py:193-194` | Harness 保存最近 50 条并只用上下键回填；`packages/cli/src/tui/application/prompt-history.ts:7,23-27,100-138`，没有搜索 UI |
| 用 `$EDITOR` 编辑当前 Prompt | `deepagents_code/command_registry.py:168-169` | Harness Composer 只有内建输入栏，无外部编辑器入口 |
| 一键复制最近 Assistant 回复 | `deepagents_code/command_registry.py:126-127` | Harness 支持任意文本拖选复制和 BTW 回答复制，`docs/user/交互使用.md:63-67,101`；没有“复制最近回复”动作 |
| 通知中心与告警设置 | `deepagents_code/command_registry.py:211-214` | Harness 有临时 Toast，没有可回看通知中心或用户告警设置 |
| TUI 主题、消息时间戳、diff 行号、聊天滚动条开关 | `deepagents_code/command_registry.py:288-307` | Harness Web 有明暗主题，TUI 风格固定；diff 有行号但不能切换，消息无时间戳开关 |
| UI 内查看全部工具 | `deepagents_code/command_registry.py:268-269` | Harness `/status` 展示 MCP/扩展统计，`/agents` 展示角色边界，但没有当前 Agent 的完整有效工具清单 |
| `/reload`、`/restart` 与 `/model` 编辑设置 | `deepagents_code/command_registry.py:199-200,232-233,282-283` | Harness 只有 `config show/path`，没有任意 `/config set`；Plugin catalog 变更要求重启进程，证据：`packages/cli/src/args.ts:45-49`、`docs/user/安全与沙箱.md:146`、`docs/user/插件管理.md:153` |

**判断。** 都是真差距，但优先级低于模型、认证、sandbox、ACP 和 CI。置信度：高。

## 4. 已确认不是差距的能力

下列旧结论或表面差异在当前 Harness 源码上已经不成立：

| 能力 | Harness 当前证据 | 结论 |
| --- | --- | --- |
| Web 搜索和 URL 获取 | `packages/agent/harness_agent/tools/tools_web.py:1,86-146`；`packages/agent/harness_agent/tools/harness_tools.py:73-103` | 已有 `web_search/web_fetch`，不是差距 |
| 跨会话记忆 | `packages/agent/harness_agent/tools/tools_memory.py:12-67`；`packages/agent/harness_agent/tools/harness_tools.py:172-201` | 已有 save/search；缺的只是 `/remember` 入口和 bundled workflow |
| 自定义 Agent | `docs/user/插件管理.md:294-330`；`packages/agent/harness_agent/runtime/agent_catalog.py:510-578` | Plugin/Claude/Qwen Agent 已进入 Managed 委派；只缺根 Agent 切换 |
| Skill 与 Plugin | `docs/user/插件管理.md:23-33,224-330`；`packages/cli/src/args.ts:94-113` | 核心能力已有；差距是 Plugin marketplace/更新和 Python extension 自由度 |
| MCP 管理与热连接 | `docs/user/交互使用.md:121-127` | stdio/HTTP/SSE 增删已有；只缺 OAuth |
| 审批模式 | `docs/user/安全与沙箱.md:54-67` | plan/default/auto-edit/auto/yolo 已覆盖 dcode manual/auto/yolo |
| 多选问答 | `packages/protocol/schema/v3.json:1478-1488`；`packages/cli/src/interactive/features/interaction-feature.ts:195-301` | 已有 multi-select，不是差距 |
| Context 压缩和用量 | `docs/user/交互使用.md:98,118-119,233-237` | Harness 压缩、原文归档、Token 水位和缓存指标更完整；缺成本/doctor，不缺压缩本身 |
| 图片理解 | `packages/agent/harness_agent/tools/snapshot_file_contract.py:268-273` | Agent 能读图片；缺直接 clipboard/video attachment |
| 远端 sandbox 抽象 | `docs/user/安全与沙箱.md:154-183` | 抽象和 fail-closed 已有；缺内置 provider 生态 |
| 子代理流式可见性 | `docs/user/交互使用.md:5,16` | 已有独立子时间线，不是差距 |
| 模型切换与 reasoning | `docs/user/模型配置.md:58,74-86` | Model Profile Picker 和 reasoning effort 已有；缺原生 provider、临时 effort 和独立摘要模型 |
| Hooks | `packages/agent/harness_agent/plugins/runtime.py:70-72` | 有 4 类受控 Plugin Hook；差距是作用域和事件矩阵 |

## 5. 建议的候选顺序

### 第一组：让非开发者和外部客户端真正可用

1. 安装包、版本检测与可回滚更新；
2. Provider adapter + 统一认证，先按真实用户选择 2～3 个，不照抄全部 22 个；
3. MCP OAuth；
4. 至少一个开箱即用远端 sandbox，并保留现有 fail-closed factory；
5. Headless timeout/max-turn/stdin，再决定是否交付 ACP。

### 第二组：提高长任务可控性与企业运维

1. Thread 成本与上下文来源审计；
2. 独立摘要模型；
3. 根 Agent 选择；
4. 远程 managed config；
5. Goal/Rubric 是否进入 Build，应先与 Compose 的单一事实源和用户确认语义做产品取舍，不能并行维护两套互相冲突的“完成”状态。

### 第三组：扩展和交互便利性

1. Plugin marketplace/更新；
2. 扩展 Hook 事件前先补明确信任模型；
3. 媒体直接附件；
4. Prompt 搜索、外部编辑器、通知中心、TUI 主题等。

Python extension 和 `js_eval` 都扩大任意代码/工具组合执行面。除非出现明确的企业使用方，不建议仅为“追平 dcode”直接复制。

## 6. 最终评价

dcode 的领先点已经从早期的“Agent 内核功能多”转向“产品交付与生态闭环完整”：安装、认证、Provider、sandbox、MCP OAuth、更新、ACP、CI 控制、成本诊断、市场和 TUI 自助入口连接成了一条完整使用链。

Harness 当前并不是能力空壳。它在 Thread/Run 审计、上下文持久化、文件 Snapshot、工作区边界、Plugin 安全适配、Team、Compose、Web 接管、撤销/重做和子代理时间线方面已经有不少 dcode 不具备或语义更严格的能力。真正的问题是：企业级骨架已经很深，但“拿到产品即可登录、隔离运行、接 IDE/CI、更新与诊断”的最后一公里仍比 dcode 短。

因此后续不应按 dcode 的 Slash Command 表机械补命令，而应按用户旅程补闭环：

```text
安装 → 认证/选模型 → 可信执行环境 → 接工具与 IDE/CI
    → 控制长任务成本/目标 → 更新与诊断 → 扩展市场
```
