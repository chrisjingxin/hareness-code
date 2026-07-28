# Checklist

## 工具风险分级
- [x] `ToolKind` 枚举定义完整（Read/Edit/Delete/Execute/Agent/Interact/Plan/Fetch 共 8 类）
- [x] 所有现有工具和新工具均有 ToolKind 映射
- [x] 未知工具 fail-closed 为 Execute 级别
- [x] `is_read_only()` 正确标识只读工具

## 权限规则持久化
- [x] 支持 session/project/user 三层级规则存储
- [x] 通配符匹配正确（`git *` 匹配 `git status`）
- [x] 最后匹配优先策略实现正确
- [x] deny 规则在任何模式下不可覆盖
- [x] `.harness/settings.json` 和 `~/.harness/settings.json` 读写正确

## 多级审批流水线
- [x] L2 deny 硬拦截在 yolo 模式下仍生效
- [x] L3 只读工具（READ/INTERACT/PLAN）在 default 模式下免审批
- [x] L5 各模式按 ToolKind 查表行为正确
- [x] 敏感路径 safetyCheck 在 yolo 模式下仍触发审批
- [x] PlanModeMiddleware 兼容性保持

## 审批选项扩展
- [x] `approve_always` 正确写入持久化规则并恢复执行
- [x] `reject_with_feedback` 将反馈文本注入 ToolMessage
- [x] TypeScript 协议类型同步更新
- [x] CLI 审批 UI 展示 5 种选项

## 拒绝追踪
- [x] 连续 3 次拒绝后注入系统提示
- [x] 审批通过后计数器重置

## 新增工具
- [x] web_search 返回结构化搜索结果，ToolKind=READ
- [x] web_fetch 支持 text/markdown/html 格式，ToolKind=FETCH
- [x] delete_file 受工作区边界约束，ToolKind=DELETE
- [x] apply_patch 正确解析 unified diff，ToolKind=EDIT
- [x] lsp 支持 definition/references/diagnostics/hover，ToolKind=READ
- [x] enter_plan_mode/exit_plan_mode 正确切换审批模式，ToolKind=PLAN
- [x] task_output/task_stop 管理后台任务生命周期
- [x] monitor 后台持续执行命令
- [x] tool_search 搜索已注册 MCP 工具
- [x] memory_search/memory_save 跨会话持久化

## 集成验证
- [x] `concurrency.py` 新工具并发安全性分类正确
- [x] `workspace_boundary.py` 覆盖 delete_file/apply_patch
- [x] `bun run typecheck` 通过
- [x] `bun test` 通过（124 pass / 2 fail 为已有平台问题）
- [x] `pytest` 通过（446 pass / 3 fail 为已有 Windows 平台问题）
- [x] 无真实 API Key 硬编码（测试使用 mock）
