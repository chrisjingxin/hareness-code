# HC-154 规格说明：隐藏内置工作流Skill

## 1. 领域模型与行为规格

### 1.1 SkillRegistry 列表过滤行为
在 `packages/agent/harness_agent/extensions/skills.py` 中：

- **`SkillRegistry.list(include_disabled=True, include_builtin=False)`**：
  - 默认参数 `include_builtin: bool = False`；
  - 当 `include_builtin is False` 时，过滤掉 `record.source == "builtin"` 的项；
  - 仅返回 `source in ("project", "user", "market")`（或其他非 builtin 来源）的 Skill 摘要；
  - 若调用方显式传入 `include_builtin=True`，则返回全量（包括内置）。

### 1.2 按需解析与执行能力保持（Invariants）
- **`resolve(value, include_disabled=False)`** 与 **`load(value, args="")`**：
  - 保持全量查找，不受 `include_builtin` 过滤影响；
  - Compose Workflow 或 Agent 执行引擎指定 `spec-driven-development`、`codebase-design` 等内置 Skill ID 时，正常解析并生成 Prompt/执行。

### 1.3 用户与项目覆盖机制
- 当用户在工作区 `.harness/skills/<name>` 或用户目录 `~/.harness/skills/<name>` 显式放置了同名 Skill 时：
  - 扫描时该记录的 `source` 为 `"project"` 或 `"user"`，`skill_id` 为 `project/<name>` 或 `user/<name>`；
  - 该记录由于 `source != "builtin"`，会正常出现在 `list()` 与 RPC 返回中。

### 1.4 跨进程 RPC 契约
- `skills.list` RPC 方法保持参数结构（支持 `include_disabled`），后端默认调用 `registry.list(include_disabled=..., include_builtin=False)`；
- 前端（CLI / TUI）无感知过滤，收到的列表即为对用户可见的非内置技能。
