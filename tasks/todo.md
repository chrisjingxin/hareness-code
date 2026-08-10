# Tool Search 整改任务清单

> 状态：设计稿（等待人工审查，未开始实施）
> 详细设计见 [tasks/plan.md](plan.md)

## Phase 1：tool_search 生效（必做）

- [ ] TS-1: 接通数据源——create_harness_tools 增加 mcp_tools 参数，_tool_search 投影真实 MCP 工具 metadata，capability 过滤提前共用（M，依赖：无）
- [ ] TS-2: 搜索质量增强——多关键词 AND、名称/描述/search_hint 权重打分、select: 精确模式、返回 input_schema（M，依赖：TS-1）
- [ ] TS-3: 模型侧引导——提示/能力说明补 tool_search 用途引导，核对 default_tool_schemas，用户文档提及（S，依赖：TS-1）

### 检查点 1（人工审查）
- [ ] agent 全量测试通过
- [ ] 手工验证：真实 MCP server 下模型可经 tool_search 找到工具
- [ ] 用户决策 Phase 2 路线（A/C/B）

## Phase 2：deferred 延迟注入（候选，需决策后开工）

- [ ] TS-4: deferred 技术验证——spike ToolCallRequest 动态工具可行性，确定路线 A/C（M，依赖：检查点 1 决策）
- [ ] TS-5: deferred 注入落地——MCP 工具 schema 不绑定模型，prompt 注入 deferred 摘要，搜索命中后可调用（XL→按路线拆分，依赖：TS-4）
- [ ] TS-6: 审批、能力视图与子代理适配——deferred 语义下审批/可见性/子代理工具集行为不变（L，依赖：TS-5）

### 检查点 2
- [ ] agent 全量测试 + CLI/Web MCP 发现→调用→审批全链路冒烟
- [ ] deferred 收益（prompt token 对比）与风险人工审查
