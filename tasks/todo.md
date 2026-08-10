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

## Phase 2：deferred 延迟注入（✅ 已完成 2026-08-10，路线 C）

- [x] TS-4: deferred 技术验证——spike 验证通过：bind_tools 每轮动态执行、middleware override 即本轮绑定；选定路线 C（middleware 动态 reveal）；影响面清单见 plan.md
- [x] TS-5: deferred 注入落地——DeferredToolMiddleware + D8 名单不绑定模型 + prompt 摘要块 + tool_search 命中 reveal 可调用 + `[tools].tool_search_defer` 配置（auto/on/off，deepseek 自动 eager 保前缀缓存）
- [x] TS-6: 审批、能力视图与子代理适配——执行入口全量注册审批不变；能力视图隐藏工具不可搜索（候选+摘要收敛）；子代理不注入 defer middleware 保持全量（Phase 1 语义）

### 检查点 2（✅ 已完成）
- [x] agent 全量测试通过（1721 passed, 2 skipped）+ docs:check / typecheck / protocol:check
- [x] 构图级冒烟：defer 开→常驻 14 工具绑定、reveal 后下一轮可调；defer 关→全量 23 工具稳定前缀

## 遗留说明

- 子代理（general-purpose 等）不启用 deferred：单任务上下文小，全量注入可接受，`SUBAGENT_EXCLUDED_TOOLS` 语义未变
- shared_engine 多 thread 共享图：revealed 状态为构图级（跨 thread 共享可见性），执行侧仍受 capability/审批约束，不构成越权；如需 per-thread 隔离可后续将状态迁入 RunContext
- 改动未提交：12 文件（9 生产/测试 + 2 文档 + plan/todo），建议按包拆分提交
