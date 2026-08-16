# HC-154 实施计划：隐藏内置工作流Skill

## 实施阶段

### 阶段一：SkillRegistry 与 Host RPC 默认隐藏 Builtin Skill（TDD）
- **改什么**：
  1. 在 `packages/agent/tests/extensions/test_skills.py`（及相关 host 测试）中添加针对 `list(include_builtin=False)` 及隐藏 builtin 的失败单测；
  2. 修改 `packages/agent/harness_agent/extensions/skills.py` 中的 `SkillRegistry.list`，增加 `include_builtin: bool = False` 并过滤 `record.source == "builtin"`；
  3. 检查并确保 `agent_host.py` 中的 `skills.list` 处理器调用过滤后的列表。
- **验证方式**：`pytest tests/extensions/test_skills.py tests/host/test_server.py` 全部通过。
- **可演示停点**：运行 `harness` 或通过 RPC 获取到的 Skill 列表中不再包含任何 `source: "builtin"` 项。

### 阶段二：全仓测试回归与集成验证
- **改什么**：针对原有断言 builtin 项出现在 `list()` 中的单测进行针对性调整与适配。
- **验证方式**：运行 Python 与 TypeScript 全量测试套件。
- **可演示停点**：TUI 侧边栏技能面板仅展示用户项目 Skill。
