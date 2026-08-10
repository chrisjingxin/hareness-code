# Tool Search 整改设计方案

> 状态：设计稿（未实施，等待人工审查）
> 本文件为整改设计，正式实施时按仓库规范迁入 `docs/developer/designs/<任务ID>.md` 并关联任务源。

## 1. 问题背景（通俗说明）

Harness Code 给 Agent 注册了一个名为 `tool_search` 的工具，设计意图是"模型按关键词搜索当前可用的 MCP 外部工具"。但当前它**不生效**：

- `harness_tools.py:150` 硬编码 `available_tools=None`，实现函数 `tools_intelligence.py` 遇到空列表直接返回"无已注册的 MCP 工具"。
- 模型调用它永远得到空结果，对 Agent 无实际价值；而 MCP 工具本身是全量注入的，模型每次请求都背着全部工具 schema。

调研 8 个参考项目（Qwen Code、Claude Code、Codex、Grok Build、DeepSeek Reasonix、Oh My Pi、DeepAgents、MiMo）发现：主流实现（Qwen/Claude/Codex/Grok）都是"**deferred 延迟注入 + 搜索后按需浮出**"配套设计——低频工具（尤其 MCP）不注入模型，由 `tool_search` 作为发现入口按需加载；搜索工具的价值是**发现**而非**筛选**。只有两个项目（DeepAgents、MiMo）与 Harness 现状相同（全量注入、无搜索）。

本次整改分两阶段：**Phase 1 让 tool_search 真正生效**（必做）；**Phase 2 deferred 延迟注入**（对齐主流形态，涉及构图架构约束，需单独决策）。

## 2. 设计目标与非目标

### 目标
- Phase 1：模型调用 `tool_search` 能返回当前运行时真实可见的 MCP 工具（名称、描述、参数 schema），搜索按质量打分排序。
- Phase 1：搜索候选与模型实际可见的工具集合一致（能力视图过滤），不泄露被策略隐藏的工具。
- Phase 2（候选）：MCP 工具默认不注入模型 schema，改为搜索命中后按需可见，控制 prompt 体积。

### 非目标（本次不改）
- 不改 Protocol（`tool_search` 是 Agent 内部工具，不跨进程；`mcp.status` 已有 tool_names，不动）。
- 不改 MCP 连接/审批/权限规则体系本身（Phase 2 只做可见性调整，执行侧审批保持）。
- 不引入新第三方依赖（打分用轻量客户端逻辑，参考 Qwen/Claude，不用 BM25 库）。
- 不实现供应商私有 tool_search（OpenAI Responses 的 hosted tool search 等）。

## 3. 目标流程

```text
Phase 1（本次）：
  Agent 构图 ──► create_harness_agent(tools=mcp_tools)
                     │
                     ├─► 全量注入（现状保持）
                     └─► create_harness_tools(mcp_tools=过滤后的工具)
                              │
模型调用 tool_search(query)
  ──► 闭包把 BaseTool 投影为 metadata（name/description/search_hint）
  ──► 打分匹配（名称精确 > 名称子串 > search_hint > 描述）
  ──► 返回 {"results": [{name, description, input_schema}]}
  ──► 模型凭结果定位并调用真实 MCP 工具

Phase 2（候选，需决策）：
  MCP 工具 schema 不绑定模型
  ──► prompt 注入 deferred 摘要（名字+一行描述，无 schema）
  ──► tool_search 命中返回完整 schema
  ──► 执行侧保持全量注册 + 审批（路线见 D7）
```

## 4. 关键设计决策

### D1：数据源 = 运行时真实 MCP 工具（复用现有注入链）
`create_harness_tools` 增加参数 `mcp_tools: Sequence[BaseTool] | None = None`。数据来源是 `agent.py` 构图时已有的 `tools` 参数（`agent_host.py:2639` 传入 `tools=mcp_tools or None`，来自 `McpConnectionManager.get_tools()`）。

理由：与 Codex/Grok 一致（从真实注册表构建索引）；无新配置、无状态同步——MCP 工具变化时每次构图自然反映；不新增数据通路。

```python
# agent.py 现有代码（约 795-809 行）
all_tools = list(tools) if tools else []
all_tools.extend(create_harness_tools(root, lsp_manager=...))   # 改：增加 mcp_tools=visible_mcp
if capability_view is not None:
    all_tools = [t for t in all_tools if capability_view.allows_tool(t.name)]
```

### D2：候选集合与可见性一致（capability 过滤）
`capability_view.allows_tool(name)` 决定模型可见/可调的工具。当前过滤发生在 `create_harness_tools` 之后；改为先把 MCP 工具按能力视图过滤，再同时用于注入和 tool_search 候选。**搜索不泄露被策略隐藏的工具**（能力视图 `mcp_tool_names` 是策略与真实资源收敛的结果）。

### D3：metadata 投影与 search_hint
`BaseTool` 投影为现有 `available_tools` 形状（`list[dict[str, str]]`，name/description），并增加 `search_hint`：
- MCP 工具名带 `{server}_` 前缀（`mcp.py:444`），`search_hint` 填服务器名，提升"发个 slack 消息"这类按服务器描述的查询命中率（参考 Qwen `mcp-tool.ts:527-529`）。
- 返回项增加 `input_schema`（从 `tool.args` 提取 JSON Schema），参考 Codex/Grok 返回完整 schema。

### D4：打分算法（无新依赖，参考 Qwen Code `tool-search.ts` / Claude Code `ToolSearchTool.ts`）
对 `tool_search` 实现升级（`tools_intelligence.py`），对照 Claude `searchToolsWithKeywords`（ToolSearchTool.ts:259-301）与 Qwen `scoreTool`（tool-search.ts:562-569）：

- **双查询模式**：
  - `select:name1,name2`：精确按名选择（逗号分隔），不走打分；缺失名忽略并提示（Qwen Mode1 / Claude :358-406）
  - 裸工具名精确匹配快路径：query 与某工具名完全一致时直接返回（Claude :194-205，处理子代理/压缩后模型直接输名字的情况）
  - 自由关键词：打分排序（默认模式）
- **tokenize**：小写、按空白拆分、停用词过滤（Qwen `TOOL_SEARCH_STOP_WORDS`，长度 <2 丢弃）
- **必选词预筛**：`+word` 前缀标记必选，必选词必须在名称部件/描述/search_hint 中全部命中才入候选（Qwen :185-209 / Claude :220-257）；**无 `+` 的词全部可选，只影响分数，不做 AND 过滤**
- **名称部件拆分**：MCP 名拆 `mcp__server__action` 三段、普通工具拆 CamelCase/下划线为部件（Claude `parseToolName` :148-153）——"git commit" 能命中 `github__create_commit` 的部件而非整串
- **打分权重**（每词累加，Claude :270-291 / Qwen :44-53 常量）：

  | 命中位置 | MCP 工具 | 内置工具 |
  |---|---|---|
  | 名称部件精确 | 12 | 10 |
  | 名称部件子串 | 6 | 5 |
  | 全名兜底（其余未命中时） | 3 | 3 |
  | search_hint 命中（词边界） | 4 | 4 |
  | 描述命中（词边界） | 2 | 2 |

  MCP 权重更高（Qwen :44-46 注释）：MCP 工具永远 deferred，发现是唯一到达途径
- **词边界正则**：描述/search_hint 匹配用预编译词边界正则（Claude `compileTermPatterns` :167），避免 "git" 误命中 "digit"
- **排序与截断**：score > 0 才入选，降序，上限 20（现 `_MAX_SEARCH_RESULTS` 保持；Qwen 默认 5 硬上限 20，Harness 无 deferred 阶段默认取 20 更实用）

### D5：搜索工具自身与提示
- `tool_search` 保持始终可见（现状已在 `_BUILTIN_TOOL_SHAPES` 与注入列表）。
- Phase 1 在系统提示/能力说明中补充一句用途引导（"可用 tool_search 按关键词查找 MCP 工具"），位置在 prompt 工具清单附近；核对 `threads/prompting.py` 的能力块与 `default_tool_schemas` 是否已含 tool_search（已含，仅补描述引导）。

### D6：返回形状与失败语义
- 成功：`{"results": [{"name", "description", "search_hint", "input_schema"}], "total": N}`
- 无工具：保持 `{"results": [], "note": "无已注册的 MCP 工具"}`
- 无命中：`{"results": [], "note": "未找到匹配工具，可尝试更宽泛的关键词"}`
- 形状变更需同步 `test_tools_intelligence.py` 现有断言（保持向后兼容优先：旧字段不删，新增字段）。

### D7：Phase 2 deferred 注入（候选，两个路线需决策）
Harness 与 Qwen/Claude/Codex 的架构差异：**LangGraph 编译图，工具集在编译时固定**（`create_deep_agent`），不能像请求式 API 那样每轮改 tools 参数。因此"搜索命中后 reveal 进下一轮"不能直接照搬，两个候选路线：

| 路线 | 机制 | 代价 |
|---|---|---|
| A. 网关工具（Grok 模式） | MCP 工具不进模型绑定；新增 `use_tool(name, args)` 统一调度；审批/权限判断移入 use_tool 内部按名查规则 | 改动大：审批预检（`approval_policy`/`composite_preflight`/`rules_provider`）、工具事件流（`tool.started` 名字语义）、子代理继承、能力视图 |
| B. 保持全量注入 | tool_search 仅作发现辅助（Phase 1 形态即终态） | 无 deferred 收益，prompt 体积不变 |

另外可探索 C：langchain `ToolNode` 的 `ToolCallRequest` 动态工具机制（`agent.py:12` 已 import），评估能否支撑运行期 reveal——这是落地前必须做的小型技术验证（spike），放入 TS-4。

### D8：Phase 2 搜索候选集 = MCP 工具 + 内置低频工具（不区分来源，只看可见性）
参考 Qwen/Claude 的规则：**defer 的对象不是"外部工具"，而是"低频/场景特定工具"**；搜索候选 = 所有 deferred（模型不可见）工具。内置工具按调用频率与工作流绑定度分档：

**常驻（不 defer，始终可见）—— 对应 Qwen/Claude 的 alwaysLoad 档：**

| 工具 | 理由 |
|---|---|
| `ls`/`read_file`/`write_file`/`edit_file`/`glob`/`grep`/`execute`/`write_todos`/`task` | 文件与执行原语，每轮都可能用（无任何项目 defer 过） |
| `ask_user` | 交互通道，隐藏会让模型退化为散文式提问 |
| `enter_plan_mode`/`exit_plan_mode` | 模式切换；exit 必须 alwaysLoad（模式提示词要求模型直接调用） |
| `tool_search` | 发现入口自身，不能被自己隐藏 |
| `apply_patch`/`delete_file` | 编辑审批流程常用，保守常驻（Qwen 无直接对应物，不冒险） |

**defer（Phase 2 纳入搜索候选）—— 低频/场景特定：**

| 工具 | 理由（对照 Qwen 同类工具的 shouldDefer=true 决策） |
|---|---|
| `lsp` | 语言服务查询低频（Qwen lsp.ts:1151 同款 defer） |
| `monitor` | 后台监控场景特定（Qwen monitor.ts:719 / Claude Monitor 同款 defer） |
| `task_output`/`task_stop` | 仅在有后台任务后使用（Qwen task-stop.ts:252 同款 defer） |
| `web_search`/`web_fetch` | 网络操作低频（Qwen web-fetch.ts:301 同款 defer） |
| `memory_save`/`memory_search` | 记忆存取低频 |

**实现形态**：`create_harness_tools` 为内置工具增加 `should_defer` 标记（默认 False，上述清单显式 True）；构图时 deferred 内置不进模型绑定（Phase 2 路线确定后），`tool_search` 候选 = `mcp_tools + deferred 内置`（metadata 投影同 D3，`is_mcp=False` 走内置权重）。常驻集合即"注入少数核心"的最终答案：框架内置 9 + ask_user + 模式切换 2 + tool_search + apply_patch/delete_file = 15 个常驻，defer 9 个内置 + MCP 全部。

## 5. 依赖图

```text
McpConnectionManager.get_tools()          （已存在，不动）
        │
        ▼
agent_host.py mcp_tools ──► agent.py create_harness_agent(tools)
        │                                  │
        │                                  ├──► 模型绑定（Phase 1 全量，Phase 2 视路线）
        │                                  └──► create_harness_tools(mcp_tools=…)   ← TS-1 新增参数
        │                                            │
        │                                            ▼
        │                                    _tool_search 闭包投影 metadata ──► tools_intelligence.tool_search 打分 ← TS-2
        │
        └── capability_view.allows_tool 过滤（TS-1 提前过滤，注入与候选共用）

prompt/能力说明（TS-3）—— 与 tool_search 用途引导
```

## 6. 实施任务

### Phase 1：tool_search 生效（必做）

**TS-1：接通数据源（M，核心切片）**
- 描述：`create_harness_tools` 增加 `mcp_tools` 参数；`_tool_search` 闭包把 BaseTool 投影为 metadata 列表（name/description/search_hint）；`agent.py` 构图处把经 capability 过滤的 MCP 工具传入；能力视图过滤提前，注入与候选共用同一集合。
- 验收：
  - [ ] 无 MCP 工具时 `tool_search` 返回"无已注册的 MCP 工具"（现状语义保持）
  - [ ] 有 MCP 工具时 `tool_search("xxx")` 返回真实工具（名称/描述/search_hint），不再恒为空
  - [ ] 被 `capability_view` 隐藏的工具不出现在搜索结果中
- 验证：
  - [ ] `cd packages/agent && .venv/bin/python -m pytest -q tests/tools/`
  - [ ] 新增测试：`test_harness_tools.py`（或并入现有测试文件）——构造 mock MCP BaseTool 列表传入 `create_harness_tools`，调用工具断言结果含工具名
  - [ ] 构图级测试：mock MCP 工具 + capability 视图，断言搜索候选与可见集合一致
- 依赖：无
- 涉及文件：
  - `packages/agent/harness_agent/tools/harness_tools.py`
  - `packages/agent/harness_agent/runtime/agent.py`
  - `packages/agent/tests/tools/test_tools_intelligence.py`（兼容性断言）
  - `packages/agent/tests/`（新增 harness_tools 测试）

**TS-2：搜索质量增强（M）**
- 描述：实现 D4 打分（双模式 select:/裸名快路径/关键词、+必选词预筛、名称部件拆分、权重 12/10/6/5/3/4/2、词边界、停用词）、返回 `input_schema`（D6）；更新函数文档与测试。
- 验收：
  - [ ] `tool_search("select:foo,bar")` 精确返回 foo/bar 完整条目；缺失名忽略并提示
  - [ ] `tool_search("git commit")` 名称部件命中（如 `github__create_commit`）排在仅描述命中的前面
  - [ ] `tool_search("+git commit")` 时不含 "git" 部件的工具被排除
  - [ ] 描述匹配不产生词边界误命中（"git" 不匹配 "digit"）
  - [ ] 结果含 `input_schema` 字段（BaseTool 可提取时）
  - [ ] 上限 20 条、无命中 note、空候选 note、停用词过滤均有测试
- 验证：`cd packages/agent && .venv/bin/python -m pytest -q tests/tools/test_tools_intelligence.py`
- 依赖：TS-1（依赖其 metadata 投影形状）
- 涉及文件：
  - `packages/agent/harness_agent/tools/tools_intelligence.py`
  - `packages/agent/tests/tools/test_tools_intelligence.py`

**TS-3：模型侧引导与文档（S）**
- 描述：系统提示/能力说明补充 tool_search 用途引导；核对 `default_tool_schemas` 与能力块已含 tool_search；用户文档提及 MCP 工具发现方式。
- 验收：
  - [ ] 提示文本包含 tool_search 用途说明（可被测试断言）
  - [ ] 用户文档 `docs/user/` 中 MCP 相关章节说明 tool_search 用法（如无 MCP 章节则跳过并说明）
- 验证：prompt 相关测试 + `bun run docs:check`
- 依赖：TS-1
- 涉及文件：
  - `packages/agent/harness_agent/threads/prompting.py`（若能力块需补）
  - `docs/user/`（视情况）

### 检查点 1（TS-1..TS-3 后，人工审查）
- [ ] `cd packages/agent && .venv/bin/python -m pytest -q`（全量 agent 测试）
- [ ] 手工验证：配置一个 MCP server（如文件系统 server），对话中让模型用 tool_search 找到其工具
- [ ] 与用户确认 Phase 2 路线（A/B/C）后再开工 TS-4

### Phase 2：deferred 延迟注入（候选，需决策）

**TS-4：deferred 技术验证与路线确认（M）**
- 描述：spike 验证 langchain `ToolCallRequest` 动态工具机制是否支撑运行期 reveal（路线 C）；输出路线 A/C 的最终选择与影响面清单。
- 验收：
  - [ ] spike 结论文档化：C 路线可行性（模型绑定集合能否动态扩展）结论明确
  - [ ] 选定路线后更新本设计文档
- 依赖：检查点 1 的用户决策

**TS-5：deferred 注入落地（XL→按路线拆分子任务）**
- 描述：按选定路线实现——**候选集 = MCP 工具全部 + 内置低频工具（D8 名单：lsp/monitor/task_output/task_stop/web_search/web_fetch/memory_save/memory_search，经 `should_defer` 标记）** 的 schema 不绑定模型（或走 use_tool 网关）；prompt 注入 deferred 摘要块（名字+一行描述，无 schema）；tool_search 命中返回完整 schema；常驻 15 个工具（D8 表格）保持可见。
- 验收：（按路线细化，先列通用项）
  - [ ] 构图后模型绑定集合不含 MCP 工具与 deferred 内置工具的 schema（常驻 15 个仍可见）
  - [ ] prompt 含 deferred 工具摘要（无 schema，只有名字与描述）
  - [ ] `tool_search` 可搜到 MCP 工具与 deferred 内置工具（含 lsp/monitor/web_search 等）
  - [ ] 搜索命中后模型可正常调用对应工具（执行路径通）
- 依赖：TS-4

**TS-6：审批、能力视图与子代理适配（L）**
- 描述：审批预检（composite_preflight/approval_policy/rules_provider）、能力视图过滤、子代理工具集与 MCP_STATUS 协议返回与 deferred 语义一致。
- 验收：
  - [ ] default/auto-edit 模式下 MCP 工具调用仍需审批（行为不变）
  - [ ] plan 模式/能力视图下被隐藏工具不可调用、不可搜索到
  - [ ] 子代理工具集与主代理规则一致（现有 SUBAGENT_EXCLUDED_TOOLS 语义不破坏）
- 依赖：TS-5

### 检查点 2（TS-4..TS-6 后）
- [ ] agent 全量测试 + CLI/Web 冒烟（MCP 工具发现→调用→审批全链路）
- [ ] 人工审查 deferred 收益（prompt token 对比）与风险

## 7. 风险表

| 风险 | 影响 | 缓解 |
|---|---|---|
| 搜索候选与注入集合不一致导致"搜得到调不了"或泄露 | 高 | TS-1 强制共用 capability 过滤后的集合（D2），构图级测试覆盖 |
| LangGraph 编译图无法动态 reveal（Phase 2） | 高 | TS-4 先行 spike；备选路线 A（use_tool 网关） |
| 内置工具 defer 后模型绑定与执行入口不一致（搜到但调不了） | 高 | 执行侧保持全量注册（路线 C）或 use_tool 网关统一调度（路线 A）；TS-5 验收含"搜索命中后可调用" |
| 打分算法回归（多关键词 AND 过严导致空结果） | 中 | 保留子串兜底：单关键词时退化为宽松匹配；测试覆盖边界 |
| 返回 input_schema 体积过大（工具参数多） | 低 | 上限 20 条 + 按分排序；schema 原文返回不做截断（与 Codex/Grok 一致） |
| capability 过滤提前影响其他工具注入路径 | 中 | 过滤逻辑收敛为单一函数，TS-1 构图级测试覆盖 |

## 8. 开放问题（需人工决策）

1. **Phase 2 是否纳入本次整改？** 建议先落地 Phase 1（tool_search 独立生效，风险低），Phase 2 按检查点 1 决策。
2. **Phase 2 路线偏好**：A（use_tool 网关，Grok 模式，改动大但对齐主流）vs C（ToolCallRequest 动态 reveal，需 spike 验证）vs B（不 defer，维持全量注入）。
3. **打分权重**：按 D4 默认（12/6/4/2）还是更保守（仅名称+描述）？默认采用 D4，可调。
4. **搜索结果是否需要 `input_schema`**：Phase 1 工具已全量注入（模型本就有 schema），返回 schema 是冗余但无害；若追求最小改动可只在 Phase 2 返回。默认 Phase 1 就返回（与 Codex/Grok 一致，为 Phase 2 铺路）。
5. **内置 defer 名单（D8）是否照单执行？** 默认采用；`apply_patch`/`delete_file` 保守常驻，若后续观测到 prompt 体积压力可再评估。名单落地前不需要额外决策，随 TS-5 一并实现。
