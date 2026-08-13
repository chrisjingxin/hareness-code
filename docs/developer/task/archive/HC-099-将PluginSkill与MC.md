---
id: HC-099
title: 将 Plugin Skill 与 MCP 接入 Host 启动快照
priority: P0
status: 已完成
owner: codex
branch: master
scope: 让 HC-098 已启用且 trust 有效的 Plugin Skill 和 MCP 进入 Host 启动时的统一 SkillRegistry 与 McpConfigSnapshot，兼容 Agent Plugins 1.0 和 Claude 格式，并保持命名空间、最小进程环境和组件失败隔离。
acceptance: 重启 Harness 后可调用已启用 Plugin 的 Skill 并连接其 MCP；同名能力不会覆盖现有来源；Plugin MCP 进程只收到明确的最小环境和路径变量；坏 Skill 或坏 MCP 只禁用对应组件；Plugin 更新会产生新的 Skill/MCP/AgentEngine 指纹。
user_docs: docs/user/插件管理.md、docs/user/交互使用.md
developer_docs: docs/developer/architecture/架构总览.md、docs/developer/扩展与插件机制设计方案.md
test_evidence: Python: 531 passed, 1 skipped; TypeScript: 116 passed, 1 skipped; typecheck passed; protocol:check passed; docs/project check only blocked by a missing historical task reference in user-owned HC-079
references: HC-087、HC-091、HC-098
completed_at: 2026-07-30
---

## 背景

HC-098 已能安全识别、安装、停用、启用 Agent Plugins 1.0 和 Claude Code Plugin，但组件报告
明确标记为 `effective=false`。当前 `SkillRegistry` 仍只扫描 builtin/user/project/market，
`ConfigChangeService` 仍只从 TOML 生成 MCP snapshot；安装 Plugin 后即使重启也不会增加模型能力。

本任务只接入依赖最少且已有成熟运行路径的 Skill 与 MCP：

```text
enabled + trusted Plugin catalog
  ├─ PluginSkillSource[] → SkillRegistry
  └─ PluginMcpSource[]   → McpServerConfig[] → McpConfigSnapshot
```

PluginManager 负责给出显式来源；SkillRegistry 和 MCP 装配器不能自行扫描 PluginStore。

## 实施步骤

1. 从 enabled catalog 生成只读 Plugin Skill 来源，canonical ID 包含 Plugin 身份和 Skill 名。
2. 拆分 Skill front matter 解析策略：保留现有 Harness 严格模式，增加 Agent Skills/Claude
   兼容字段；`allowed-tools` 只记录请求，不授予权限。
3. 修正 `/.harness/skills/` 对多段 canonical ID 的解析，正文与资源仍通过统一 read_file 按需读取。
4. 从 portable `mcp.json` 与 Claude `.mcp.json`/inline `mcpServers` 生成统一 McpServerConfig，
   transport、相对命令和 Plugin 路径变量只在 Adapter 中转换。
5. 为 Plugin MCP 生成不可冲突的 server/tool namespace，并将 package digest 放入 MCP 身份。
6. Plugin stdio MCP 使用最小环境；不继承 API Key 等任意 Host 环境，只暴露 PATH、Plugin root/data、
   workspace 和 manifest 明确声明的值。
7. Host initialize 先读取一次 ExtensionCatalogSnapshot，再用同一快照构建 SkillRegistry 和
   McpConfigSnapshot；当前生命周期不安全支持热更新，管理响应统一声明下次 Host 启动生效。
8. 覆盖 portable/Claude Skill、MCP 转换、命名冲突、坏组件隔离、环境泄露、snapshot 指纹和
   Host 关闭测试。

## 范围

- user scope 已启用 Plugin 的 Skill 和 MCP。
- Agent Plugins 1.0 与 Claude Code Plugin。
- Host 启动期不可变装配。
- Plugin Skill/MCP 的 canonical namespace、路径变量和最小进程环境。

## 非范围

- 不实现运行中 Plugin 热重载；HC-095 完成资源 ownership 前统一要求重启。
- 不实现 Plugin Agent、Command、Hook、LSP、Monitor、workflow、channel 或 Team。
- 不把 Skill `allowed-tools` 作为 Policy 或审批豁免。
- 不提供 MCP OAuth、凭据注入 UI、签名或 managed policy。

## 验收清单

- [x] enabled + trusted Plugin Skill 在下次 Host 启动后可列出、读取和显式调用。
- [x] Claude Skill 的调用控制字段被正确解释，未知兼容字段不导致整个 Plugin 失效。
- [x] 多段 Plugin Skill ID 的正文和资源都可经 `/.harness/skills/` 安全读取。
- [x] portable 和 Claude MCP 进入同一个 McpConfigSnapshot 与连接管理器。
- [x] Plugin MCP server/tool 名具有稳定 namespace，不覆盖用户 MCP。
- [x] Plugin stdio MCP 不继承 Host 的任意秘密环境变量。
- [x] 一个坏 Skill 或 MCP 不影响同包其他有效组件。
- [x] Plugin package/config 变化会改变相应 snapshot 和 AgentEngineProfile。

## 实现结果

输入到输出的生产链路已经固定为：

```text
enabled + trusted ExtensionCatalogSnapshot
  → PluginManager 复核 store digest
  → PluginSkillSource[] + namespaced McpServerConfig[]
  → SkillRegistry + 合并后的 McpConfigSnapshot
  → AgentEngineProfile
```

- `skills.py` 增加 Plugin 显式来源、portable/Claude front matter 方言、模型调用控制、
  `requested_tools` 和 package digest；普通 Harness Skill 的严格解析规则不变。
- `virtual_files.py` 使用最长 canonical 前缀解析
  `plugin/<source>/<plugin>/<skill>/...`，正文和资源继续走同一个只读后端。
- `plugins/mcp_adapter.py` 支持 Agent Plugins `mcp.json` 与 Claude `.mcp.json`、manifest
  path/inline 配置；每个 Server 独立转换并生成 `plugin__...` namespace。
- Plugin stdio MCP 只继承最小系统环境；root、data、workspace placeholder 只在 Adapter
  边界展开，包内命令复核普通文件与可执行位。
- `AgentHost` 只捕获一次 Plugin catalog，并在初始化与用户 MCP 热重建时保留同一组 Plugin
  MCP；Plugin 管理响应明确声明下一次 Host 启动生效。
- 组件报告已把实际接入的 Skill/MCP 标记为 `supported/effective=true`；坏项只形成
  diagnostics，不连带停用同包有效项。

版本影响：当前项目尚未正式发版，本任务不修改版本号或 Changelog。

## 前置

- HC-087
- HC-091
- HC-098
