# ZC-141 Agent Plugins 1.0.0 规范追踪矩阵

> 本文是备用仓库 `harness-code-feature` 的 ZC-141 Todo 1 研究证据，不是规范本身。
> 规范事实源固定为规划工作区 `agent-plugins-spec` 的提交
> `bd383552095128f6effe895b9257cfd580a6d179`；优先服从
> `spec/1.0.0.md`，机器可读 schema 只作辅助。

## 读取范围

矩阵覆盖规范 §4--§11 中适用于 Harness 本地目录/ZIP client 的 MUST/MUST NOT，并单列 SHOULD、
OPTIONAL 和当前非范围。以下状态是 2026-08-13 收尾复核后的状态，依据当前 dirty worktree 的源码、
fixture 和真实测试结果；未验证的项目级检查与客户端自定义能力单独标为剩余风险或非范围。

状态含义：

- **已实现**：目标仓库当前代码和已有测试已形成可复核证据。
- **部分实现/剩余风险**：Harness 边界已实现，但规范只作 SHOULD/OPTIONAL、认证依赖外部服务，
  或项目级验证受本地依赖环境阻断；不表示 portable MUST 尚未实现。
- **非范围/客户端自定义**：规范不要求 Harness 为所有外部客户端实现内容；只验证未知扩展不会改变 portable core。

## §4 Plugin package model

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §4.1(1) | Plugin 是单一 filesystem root 下的目录；client 至少能从目录加载。 | `packages/agent/harness_agent/plugins/store.py: PluginStore.stage` | `packages/agent/tests/test_plugins.py: test_portable_install_is_disabled_and_enable_requires_current_fingerprint`；`tests/test_plugin_fixtures.py` 的目录导入 | 已实现 |
| §4.1(2) | 根目录 MUST 有 `plugin.json`。 | `plugins/adapters.py: load_plugin_descriptor`；`plugins/portable.py: load_portable_plugin` | 所有 portable fixtures 的根 `plugin.json`；Google 目录/ZIP 导入测试 | 已实现 |
| §4.1(3) | 发现、读取、执行的包路径解析后 MUST 留在 plugin root；越界 symlink/junction 等 MUST 拒绝。 | `plugins/store.py: _copy_directory_secure, _extract_zip_secure, package_digest`；`plugins/common.py: safe_package_path` | `test_directory_symlink_and_hardlink_are_rejected`；`test_zip_with_parent_traversal_is_rejected_before_validation`；`malicious-paths/mcp.json` | 已实现 |
| §4.1(4) | 规范定义的 plugin-relative path MUST 以 `./` 开头，解析后仍在 root 内。 | `plugins/mcp_schema.py: validate_stdio_command/validate_stdio_cwd`；`plugins/mcp_adapter.py: _portable_server_config` | `test_malicious_mcp_paths_are_isolated_before_runtime`；malicious path fixture | 已实现 |
| §4.1(5) | command args/env 等非 path 配置是 opaque strings；MUST NOT 按包路径强制解释。 | `plugins/mcp_adapter.py: _portable_server_config`；`plugins/mcp_schema.py` | `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog`；unknown placeholder 断言 | 已实现 |
| §4.1 failure boundary | 根 manifest 越界拒绝；固定位置错误只隔离组件；坏 `SKILL.md` 跳过；MCP command/cwd 错误只隔离 server；其他包路径拒绝访问。 | `plugins/store.py`、`plugins/portable.py`、`plugins/common.py`、`plugins/mcp_schema.py`、`plugins/mcp_adapter.py` | symlink/ZIP 安全测试；`partial-components` 和 `malicious-paths` fixture；149 项 focused ZC-141 测试 | 已实现 |
| §4.2 | 标准 layout 固定根 `plugin.json`、`skills/`、`mcp.json` 与 top-level client namespace。 | `plugins/portable.py` 固定发现；Adapter 分离 native manifest | `google-spanner-0.3.4`、`google-alloydb-0.2.0` 的 `SOURCE.md` 与目录/ZIP 形状测试 | 已实现 |

## §5 Manifest

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §5.1 | MUST 检查 root `plugin.json`；portable core 只有一个根 manifest，不能由其他文件替换、补充或覆盖；先校验 manifest 再发现组件/应用 client behavior。 | `plugins/adapters.py: load_plugin_descriptor`；`plugins/portable.py: load_portable_plugin` | Google fixtures 同时含 `.claude-plugin/`、`.codex-plugin/`、`gemini-extension.json`；导入测试断言 root manifest 优先 | 已实现 |
| §5.2 object/closed schema | `plugin.json` MUST 是 JSON object；允许字段固定；除未知顶层字段和非 object `extensions` 外的 schema 违规 MUST fatal，且不得发现/执行组件。 | `plugins/common.py: read_json_object`；`portable.py: _validate_metadata` | `test_portable_manifest_schema_and_name_errors_block_component_discovery`；`test_portable_manifest_core_type_errors_block_component_discovery`；nonfatal fixture | 已实现 |
| §5.2 unknown fields | 未知顶层字段 MUST 报告、忽略、继续加载；MUST NOT 赋予语义。 | `portable.py: _KNOWN_MANIFEST_FIELDS` 与 `diagnostics` | `nonfatal-manifest/plugin.json` 的 `x-fixture-unknown`；fixture 测试断言诊断并保留 Skill | 已实现 |
| §5.2 schema selection | `$schema` MUST 为 canonical 1.0.0 identifier；client MUST 用本地已识别值选择规则；MUST NOT 加载时联网取 schema；不支持的版本 MUST reject。 | `portable.py: PLUGIN_SCHEMA_ID`；`plugins/adapters.py` | `test_portable_manifest_schema_and_name_errors_block_component_discovery`；所有 portable fixtures 固定 canonical `$schema` | 已实现 |
| §5.3 | `$schema`、`name` 缺失/类型错误/空值/约束违规 MUST reject，且 MUST NOT discover/execute components。 | `portable.py: load_portable_plugin`；`common.py: require_portable_plugin_name` | `test_portable_manifest_schema_and_name_errors_block_component_discovery`；`test_portable_manifest_accepts_period_in_name` | 已实现 |
| §5.4 types | `version/description/homepage/repository/license` MUST 为 string；`keywords` MUST 为 string[]；`author` MUST 是只含 `name/email/url` 且值为 string 的 closed object。 | `portable.py: _validate_metadata`；`common.py: optional_string` | `test_portable_manifest_rejects_invalid_author_shape`；Google/nonfatal metadata fixtures | 已实现 |
| §5.4 metadata semantics | 除显式约束外只按 JSON type 校验；MUST NOT 因非 SemVer version、非标准 URL/email/SPDX 而 reject。 | `portable.py: _validate_metadata` | `test_portable_manifest_accepts_non_semver_version_string` | 已实现 |
| §5.5 name | name MUST 为 1--64 字符、只含小写字母/数字/`-`/`.`、首尾 alphanumeric、不得含 `--`/`..`；句点合法。 | `common.py: PORTABLE_PLUGIN_NAME_RE, require_portable_plugin_name` | `test_portable_manifest_accepts_period_in_name` | 已实现 |
| §5.6 | `extensions` 用于 client-specific manifest data；其内容语义由 §8 处理。 | `portable.py: load_portable_plugin` | Google root `extensions` 含未实现 Google namespace；nonfatal 非 object fixture | 已实现 |

## §6 Component discovery / fixed locations

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §6.1 | 每种 supported component MUST 从固定位置发现；`plugin.json` 不得 override location 或 inline component config。 | `portable.py: load_portable_plugin` 固定检查 `skills/`、`mcp.json` | Google、partial、empty fixtures；现有 `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog` | 已实现 |
| §6.2 missing | 固定位置缺失 MUST NOT 是错误。 | `portable.py: load_portable_plugin` 仅在存在时追加报告 | `empty-components/` 无 `skills/`/`mcp.json`；`test_zero_effective_plugin_install_is_disabled...` | 已实现 |
| §6.2 wrong kind | 固定位置存在但 filesystem kind 错误 MUST 只使该 component type invalid，并继续其他类型。 | `common.py: validate_skill_manifests/read_json_object`；`portable.py: _validate_mcp` | `test_portable_wrong_kind_skills_component_does_not_block_mcp`；partial fixture | 已实现 |

## §7 Component types

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §7 general | v1 portable core 只有 Skills/MCP；client MUST ignore unsupported component types。 | `portable.py` 只发现两类；`adapters.py` 单独处理 Harness/Claude extension | Google 的 `.codex-plugin/`、`gemini-extension.json`、`com.google.gemini-cli/` 共存测试；`harness-full-demo` 回归 | 已实现 |
| §7.1 | Skill MUST conform to Agent Skills specification；固定 `skills/` 下每个 immediate child 目录中恰好按名发现 regular `SKILL.md`；MUST NOT recursive search。 | `common.py: validate_skill_manifests` | direct-child/path boundary 测试；Google Skill 目录形状；`test_portable_skills_use_direct_children_and_isolate_invalid_entries` | 已实现 |
| §7.1 failure | 无效 Skill MUST skip 并继续其他 Skill/组件，SHOULD report。 | `common.py: validate_skill_manifests`；`portable.py` diagnostics | `partial-components/skills/broken-skill` 与 `valid-skill`；`test_invalid_plugin_items_are_isolated_from_valid_skill_and_mcp` | 已实现 |
| §7.2 discovery | MCP MUST 只从 root `mcp.json` 读取；MUST NOT inline 到 `plugin.json` 或使用其他 core path。 | `portable.py: _validate_mcp`；`mcp_adapter.py: _load_portable` | Google/partial/malicious `mcp.json`；现有 portable runtime test；Claude inline 只在 Claude Adapter 中处理 | 已实现 |
| §7.2 top-level | `mcp.json` MUST 是 closed object，必须有 canonical `$schema`、`mcpServers`；`mcpServers` MUST object；空 object 合法。 | `plugins/mcp_schema.py: validate_mcp_document`；`portable.py: _validate_mcp` | `test_mcp_top_level_schema_is_closed`；Google/empty mcp | 已实现 |
| §7.2 schema | MCP `$schema` MUST 被本地识别且与 plugin 版本一致；client MUST NOT 联网取 schema。 | `plugins/mcp_schema.py: MCP_SCHEMA_ID`；`mcp_adapter.py: _load_portable` | `test_mcp_top_level_schema_is_closed`；Google/partial canonical mcp fixtures | 已实现 |
| §7.2 union | 每个 server MUST 有 `type` 并精确匹配一个 closed variant；未知 field/type/跨 variant field MUST 使该 entry invalid。 | `plugins/mcp_schema.py: validate_mcp_server` | `test_mcp_closed_schema_isolates_invalid_and_unsupported_servers` | 已实现 |
| §7.2 stdio command | `command` MUST 是单一 executable token；只能是 bare name 或 `./` plugin-relative path；MUST NOT placeholder expansion；包内命令按 root 解析。 | `plugins/mcp_schema.py: validate_stdio_command`；`mcp_adapter.py: _portable_server_config` | `test_malicious_mcp_paths_are_isolated_before_runtime`；`test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog` | 已实现 |
| §7.2 stdio cwd | cwd 缺省 MUST 为 plugin root；显式值只能是 `./`、`${PLUGIN_ROOT}` 或 `${PLUGIN_DATA}` 根路径，展开并 containment 校验后才有效。 | `plugins/mcp_schema.py: validate_stdio_cwd`；`mcp_adapter.py: resolve_portable_cwd` | portable cwd/data 断言；malicious path fixture | 已实现 |
| §7.2 args/env/cwd | stdio 的 args/env/cwd MUST 支持两个保留 placeholder；未知 placeholder 不得被宿主环境替换。 | `plugins/mcp_schema.py`、`mcp_adapter.py`、`extensions/mcp.py` | `test_plugin_stdio_keeps_unknown_placeholders_and_forces_reserved_env`；portable runtime test | 已实现 |
| §7.2 HTTP/SSE URL | url MUST 是无 userinfo/fragment 的绝对 HTTP(S) URL；非 loopback MUST HTTPS；loopback 可 HTTP；URL 不得含 placeholder。 | `plugins/mcp_schema.py: validate_http_url` | `test_mcp_http_url_and_header_validation_is_strict`；portable fixture validation | 已实现 |
| §7.2 HTTP headers | header name/value MUST 是合法 HTTP fields；名称大小写不敏感且重复 casing invalid；不得 placeholder/环境展开；不得内置秘密；client header 覆盖同名配置；跨源 redirect/SSE 不转发配置 header。 | `plugins/mcp_schema.py: validate_http_headers`；`extensions/mcp.py: _create_plugin_http_client` | HTTP/SSE redirect/header tests；strict URL/header test | 已实现 |
| §7.2 auth | v1 不定义 OAuth/portable credential fields；认证失败按 server connection failure，不改变静态 Plugin validity。 | `extensions/mcp.py: McpConnectionManager` | isolated server auth failure test；OAuth UI 为非范围 | 部分实现/剩余风险 |
| §7.2 transport | 支持 MCP 的 client MUST 支持 stdio 或 streamable-http 至少一种；SHOULD 两种；SSE OPTIONAL；初始连接 MUST 使用声明 transport，规范不定义 fallback。 | `plugins/mcp_adapter.py`；`extensions/mcp.py` | `tests/extensions/test_mcp.py` 覆盖 stdio/http/sse；per-server isolation | 已实现 |
| §7.2.2(1) | MCP client MUST 只从 root `mcp.json` load。 | `mcp_adapter.py: _load_portable` | `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog`；Google root mcp | 已实现 |
| §7.2.2(2) | mcp JSON/schema/version/top-level 无效 MUST disable MCP、继续其他组件；SHOULD report。 | `portable.py: _validate_mcp` 捕获为 component report | `test_mcp_top_level_schema_is_closed`；partial component fixture | 已实现 |
| §7.2.2(3) | 单 server 无效 MUST skip，继续其他 server/组件；SHOULD report。 | `portable.py: _validate_mcp` 逐 entry try；`mcp_adapter.py` 逐 entry隔离 | `partial-components` valid/broken server；现有 `test_invalid_plugin_items_are_isolated...` | 已实现（基础隔离） |
| §7.2.2(4) | 不支持 transport 的 valid entry MUST skip，不影响其他；SHOULD report unsupported。 | `plugins/mcp_schema.py: _UNSUPPORTED_TRANSPORTS`；`portable.py` report | `test_mcp_closed_schema_isolates_invalid_and_unsupported_servers` | 已实现 |
| §7.2.2(5) | server start/connect/auth/handshake failure MUST 继续其他 server/组件；SHOULD report connection failure。 | `extensions/mcp.py: McpConnectionManager.connect_all` | `tests/extensions/test_mcp.py` 的 failed/all_connected；不启动真实 Google MCP | 已实现（runtime 基础隔离） |

## §8 Client extensions

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §8 | client manifest data MUST 放在 reverse-domain namespace；client-specific files MUST 放在同名 top-level directory；可二者并存。 | `portable.py` 处理 Harness namespace；`adapters.py` 分离未知/Claude/native manifests | Google fixtures 的多 client namespace 与 top-level directory；结构测试 | 已实现（Harness boundary） |
| §8 namespace guidance | namespace SHOULD 基于 client 控制的 domain，且 SHOULD 稳定。 | 由外部 client namespace 自定义，Harness 不替外部客户端定义 | Google fixture 固定 namespace 形状 | 非范围/客户端自定义 |
| §8 semantics | portable spec 不为 extension 内容/文件赋予 discovery、validation、failure 语义，各 client 自定义。 | `adapters.py` 分离 portable/Claude；`portable.py` 仅识别 `com.za38.harness` | hybrid `harness-full-demo`；Google 私有文件静态共存测试 | 已实现（边界） |
| §8.1 | `extensions` 若存在 MUST 是 namespace→object；非 object MUST report/ignore/继续；未实现 namespace MUST ignore 且不校验 value。 | `portable.py: load_portable_plugin` | `nonfatal-manifest` 非 object；Google unknown namespace object；fixture 测试 | 已实现（未实现 namespace） |
| §8.1 Harness | 已实现 namespace 的校验/失败语义由 Harness 自定义，不得被 unknown namespace 冒充。 | `portable.py: _load_harness_extension` | `test_portable_team_file_becomes_fixed_dag_definition`；`harness-full-demo` | 已实现 |
| §8.2 | 实现 file-based namespace 的 client MUST 查找对应 top-level directory。 | `portable.py: _load_harness_extension` 安全读取 Harness extension path；Google namespace 不进入 Harness inventory | Google top-level `com.google.gemini-cli/extension.json` 静态共存测试 | 已实现（Harness boundary） |

## §9 Environment variables and placeholder expansion

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §9.1 | 启动 stdio subprocess MUST 提供绝对 `PLUGIN_ROOT` 与每安装实例专属 `PLUGIN_DATA`。 | `mcp_adapter.py: _load_portable, _prepare_data_path, _portable_server_config`；`extensions/mcp.py` | `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog`；stdio placeholder regression | 已实现 |
| §9.1 data lifecycle | client MUST 在启动前创建可写 data、更新时保留；卸载可删除。 | `mcp_adapter.py: _prepare_data_path`；`plugins/store.py: data_path/remove` | `test_remove_retains_data_unless_purge_is_explicit`；更新 snapshot 测试 | 已实现（Harness 生命周期边界） |
| §9.1 environment layering | base environment 可继承/清理；展开后的 manifest env overlay base；最后 client MUST 覆盖保留变量。 | `mcp_adapter.py` 与 `extensions/mcp.py` 的 plugin minimal environment | `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog`；reserved env/unknown placeholder regression | 已实现 |
| §9.1 ambient dependency | 除 executable search 外，conformant plugin MUST NOT 依赖未声明 ambient env。 | `mcp_adapter.py` 固定不继承完整宿主环境；`extensions/mcp.py` plugin env allowlist | plugin environment isolation assertions；expanded ZC-141 tests | 已实现 |
| §9.2 placeholders | MUST 对 args 每个 string、env value、cwd 做一次非递归 `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` 替换；不得作用于 env key、command、固定位置。 | `mcp_schema.py` portable validation；`mcp_adapter.py` `_replace_known` | `test_plugin_stdio_keeps_unknown_placeholders_and_forces_reserved_env`；portable runtime test | 已实现 |
| §9.2 safety | 未知 placeholder MUST 原样保留；MUST NOT 做其他宿主环境展开；保留变量不得由 manifest env 伪造；env 是可见配置不是秘密机制。 | `mcp_adapter.py` reserved env overlay；`extensions/mcp.py` minimal env | unknown placeholder/reserved env regression；no-secret runtime tests | 已实现 |

## §10 Versioning

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §10.1 release/schema | 每个 spec release MUST 发布同版本 plugin/MCP schemas；canonical identifier MUST NOT 被重新指向不同内容。 | 规范事实源 `agent-plugins-spec/schemas/1.0.0/`；Harness 使用固定本地常量 | 规范 HEAD 与 schema 文件在本阶段只读核对；不在运行时下载 | 已实现（固定事实源） |
| §10.1 package/MCP version | `plugin.json` `$schema` 声明目标版本；若有 mcp.json，其 schema 版本 MUST 匹配；mismatch 只使 MCP invalid，不影响 Skill。 | `portable.py: PLUGIN_SCHEMA_ID/MCP_SCHEMA_ID/_validate_mcp` | Google/partial mcp canonical schema；invalid MCP 与有效 Skill 隔离测试 | 已实现 |
| §10.2 plugin version | `version` MUST 是 string；SemVer 只是 SHOULD/RECOMMENDED，client MUST NOT 以 SemVer 作为拒绝门禁。 | `portable.py: _validate_metadata` | `test_portable_manifest_accepts_non_semver_version_string`；nonfatal fixture | 已实现 |

## §11 Client conformance and failure handling

| 规范章节 | 要求摘要（MUST/MUST NOT） | 实现位置 | 测试或 fixture 证据 | 状态 |
| --- | --- | --- | --- | --- |
| §11.1(1) | conformant client MUST 能从 directory path load。 | `plugins/store.py: stage`；`manager.py: validate/install` | Google 目录导入测试；现有 portable 目录测试 | 已实现 |
| §11.1(2) | 从 `$schema` 选择本地 manifest 规则，按 closed schema 及 §5.2 非致命例外校验。 | `adapters.py`、`portable.py` | nonfatal/Google fixtures；manifest fatal/nonfatal focused tests | 已实现 |
| §11.1(3) | MUST ignore 未实现 extension members，不校验其 values。 | `portable.py: load_portable_plugin` | Google unknown extension values 与 native manifests；结构/导入测试 | 已实现 |
| §11.1(4) | 每个 supported component type MUST 在 fixed location discover。 | `portable.py`、`mcp_adapter.py` | Google、partial、empty fixtures；existing Skill/MCP tests | 已实现（基础发现） |
| §11.1(5) | 支持 MCP 时选本地 MCP schema，并至少支持 stdio 或 streamable-http。 | `portable.py`、`mcp_schema.py`、`mcp_adapter.py`、`extensions/mcp.py` | `tests/extensions/test_mcp.py` 的 stdio/http/sse；portable closed-schema tests | 已实现 |
| §11.1(6-7) | 启动 stdio 时提供两个保留 env、展开运行字段；command 是单 token，默认 cwd 为 root。 | `mcp_schema.py`、`mcp_adapter.py`、`extensions/mcp.py` | portable connection conversion；unknown/reserved placeholder regression | 已实现 |
| §11.1(8) | client MUST 支持至少一种 component type。 | `plugins/portable.py` 与 Skill/MCP runtime | `test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog`；Google Skill | 已实现 |
| §11.2 | 可增量只支持一种 component type，但必须满足该类型全部适用规则。 | `portable.py` 同时支持 Skill/MCP；private extensions separately adapted | `empty-components`、Google/partial static inputs；zero-effective enable gate | 已实现 |
| §11.3(1) | unsupported component types MUST ignore。 | `portable.py` 未扫描 unknown portable components；Adapter 分离外部格式 | `.codex-plugin/`、`gemini-extension.json`、Google namespace fixture | 已实现（边界） |
| §11.3(2) | unknown top-level / non-object extensions non-fatal；其他 plugin.json violation fatal 且不得 discover/execute。 | `portable.py` manifest gate and diagnostics | `nonfatal-manifest`；manifest fatal/nonfatal tests；loading order assertions | 已实现 |
| §11.3(3) | component type/entry/process 的局部失败 MUST NOT 阻止独立有效组件。 | `portable.py`、`mcp_adapter.py`、`extensions/mcp.py` | `partial-components`；`test_invalid_plugin_items_are_isolated...`；MCP failed status tests | 已实现 |
| §11.3(4) | SHOULD report invalid/failures；unsupported 本身不是 error。 | `PluginComponentReport.diagnostics`、manager report | diagnostics tests；partial/nonfatal fixtures；unsupported transport test | 已实现 |

## SHOULD / OPTIONAL 记录

| 章节 | SHOULD/OPTIONAL 要求 | 当前证据与状态 |
| --- | --- | --- |
| §5.3、§5.4、§7.1、§7.2.2 | SHOULD 报告指定字段、坏 Skill、MCP config/entry/connection failure。 | `diagnostics`、partial fixture、invalid/unsupported/per-server tests 已覆盖 Harness 边界。已实现。 |
| §7.2.1 transport | SHOULD 同时支持 stdio 与 streamable-http；SSE OPTIONAL。 | Harness Adapter/runtime 有 stdio、streamable HTTP、SSE 测试输入；按声明 transport 连接。已实现。 |
| §8 namespace | SHOULD 使用自有 domain 并保持稳定。 | 外部 Google namespace 只作互操作形状，Harness 不替第三方宣称所有权。客户端自定义。 |
| §10.2 | Plugin `version` SHOULD 使用 Semantic Versioning；license/SPDX 推荐。 | Google fixtures 使用版本号和 Apache-2.0；非 SemVer 仅按 string 接受，未强制 SHOULD。已实现。 |
| §11.3 | Client SHOULD 报告 invalid/failure，MAY 报告 partial unsupported，但 unsupported 本身不是 error。 | diagnostics 已区分 invalid、unsupported 和 runtime failed；unsupported transport 有 focused test。已实现。 |

## Fixtures and scope notes

- Google fixture provenance is recorded in `packages/agent/tests/fixtures/agent_plugins/google-*/SOURCE.md`。
  固定提交分别为 Spanner `44e39adf3e3eea515b36390a3ff62601ac5fbf1d`（0.3.4）和 AlloyDB
  `be8f5ca61cf8cbb4ac350bcfef7fa63fbd18401c`（0.2.0），上游归属为 Apache-2.0。
- 两组 Google fixture 是手工最小脱敏形状，不是在线 clone 或上游源码 vendoring；root `mcp.json`
  使用空 `mcpServers`，因此测试不会启动真实 MCP、访问 Google Cloud 或读取凭据。
- `tests/test_plugin_fixtures.py` 验证静态目录、ZIP staging、portable root/manifest/MCP 语义和边界输入；
  2026-08-13 直接运行结果为 `99 passed`，扩展 ZC-141 focused 集合最终为 `149 passed in 10.06s`。
- ZC-141 实现已覆盖 `portable.py`、`mcp_schema.py`、`mcp_adapter.py`、`store.py`、`manager.py`、
  `common.py`、`model.py`、`adapters.py`、`claude.py` 及对应回归测试；没有新增 Protocol/公开数据字段、
  JSON-RPC 方法或 VERSION/CHANGELOG 版本变更。

## 2026-08-13 收尾复核与真实验证

- 直接测试：
  `cd packages/agent && PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/zc-141-pycache
  .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_plugin_fixtures.py tests/extensions/test_mcp.py`
  → `99 passed in 4.17s`。
- 扩展测试：
  `tests/test_plugins.py tests/test_plugin_migration.py tests/test_plugin_fixtures.py
  tests/test_plugin_runtime.py tests/test_full_demo_plugin.py tests/extensions/test_mcp.py
  tests/host/test_server.py::test_host_startup_without_plugins_does_not_create_default_registry_lock`
  → `149 passed in 10.06s`。
- 关键边界：无 registry 的默认 `Path.home()` Host 启动不创建 `.harness/plugins` 或 `registry.lock`；
  Claude/portable MCP 的 HTTP/SSE 均使用 `follow_redirects=False` factory，同时保留 Claude
  placeholder/timeout 语义；portable URL/header 拒绝 `${...}` placeholder。
  （2026-08-14：v1→v2 迁移机制已随范围收缩移除，本矩阵不再记录迁移边界。）
- 项目级检查的真实结果、依赖阻断和全量 Agent 的历史结果写入同一任务与 `tmp/handoff.md`；当前任务
  仍保持“待认领”，没有执行 `task:claim`、`task:complete`、commit 或 push。
- 项目检查：`bun run docs:check`、`bun run tasks:check`、`bun run test:project` 通过；
  `bun run protocol:check`/`bun run project:check` 因缺少 `ajv/dist/2020` 阻断；`bun run typecheck`
  因 Bun/tsc 工具链不兼容产生解析错误；`bun run test:ts` 为 `190 pass, 1 skip, 52 fail, 45 errors`
  并受 workspace 依赖缺失影响。上述项目阻断不改写 portable MUST 的 Python focused 证据。
