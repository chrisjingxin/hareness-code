# Tool Search 整改任务清单

> 状态：**Phase 1 已完成（2026-08-10）**；Phase 2 待人工决策后开工
> 详细设计见 [tasks/plan.md](plan.md)

## Phase 1：tool_search 生效（✅ 已完成）

- [x] TS-1: 接通数据源——create_harness_tools 增加 mcp_tools 参数，_tool_search 投影真实 MCP 工具 metadata，capability 过滤共用
- [x] TS-2: 搜索质量增强——Qwen 方案：select:/裸名快路径/关键词三模式、+必选词预筛、名称部件拆分、权重打分、词边界、停用词、返回 input_schema
- [x] TS-3: 模型侧引导——tool_search description 补充用法说明，用户文档（交互使用.md、subagent.md）更新

### 检查点 1（已完成）
- [x] agent 全量测试通过（1705 passed, 2 skipped）
- [x] 手工冒烟：构图级验证 tool_search 返回真实 MCP 工具、capability 隐藏工具不可搜索
- [ ] 用户决策 Phase 2 路线（A/C/B）——待决

## Phase 2：deferred 延迟注入（候选，需决策后开工）

- [ ] TS-4: deferred 技术验证——spike ToolCallRequest 动态工具可行性，确定路线 A/C（M，依赖：检查点 1 决策）
- [ ] TS-5: deferred 注入落地——MCP 全部 + 内置低频工具（lsp/monitor/task_output/task_stop/web_search/web_fetch/memory_save/memory_search，D8 名单）不绑定模型，prompt 注入 deferred 摘要，搜索命中后可调用（XL→按路线拆分，依赖：TS-4）
- [ ] TS-6: 审批、能力视图与子代理适配——deferred 语义下审批/可见性/子代理工具集行为不变（L，依赖：TS-5）

### 检查点 2
- [ ] agent 全量测试 + CLI/Web MCP 发现→调用→审批全链路冒烟
- [ ] deferred 收益（prompt token 对比）与风险人工审查
