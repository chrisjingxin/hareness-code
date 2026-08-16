# HC-154 执行清单：隐藏内置工作流Skill

## 阶段一：SkillRegistry 与 Host 过滤内置 Skill（停点 1）
- [x] 编写失败测试：验证 `SkillRegistry.list()` 默认过滤 `builtin` 来源，且传入 `include_builtin=True` 时仍可包含；验证 `resolve` 与 `load` 依然可正常读取 `builtin` 项
- [x] 修改 `SkillRegistry.list` 实现，增加 `include_builtin: bool = False` 过滤逻辑
- [x] 验证 `agent_host.py` 中 `skills.list` RPC 派发行为
- [x] 运行聚焦测试：`pytest tests/extensions/test_skills.py`

## 阶段二：全量测试验证与交付（停点 2）
- [x] 运行全量 Python 测试：`pytest`（2178 passed）
- [x] 运行 TypeScript 类型检查与全量测试：`bun run typecheck && bun run test:ts`（732 passed）
- [x] 同步更新交接文档与任务归档
