# TDD 方法（Compose 私有资产；行为/缺陷/重构任务强制）

本任务属于行为、缺陷修复或重构（change_kind：behavior | bug | refactor），
必须按 TDD 流程执行：

## 步骤

1. **RED**：先写一个能复现期望行为的 focused 测试（或修改现有测试），
   运行它并确认它失败。把失败输出如实记入 red_evidence —— 这是本任务
   完成的前提，缺 RED evidence 的任务会被判为失败。
2. **GREEN**：用最小实现让该测试通过；只写让测试变绿的代码。
3. **REFACTOR**：在测试保持绿色的前提下清理实现。
4. 运行任务声明的 verification_commands，把结果写入 focused_test_evidence。

## 规则

- red_evidence 必须包含：测试名 + 真实失败摘要（例如
  `test_search_returns_results FAILED: assert [] == [...]`）。
- 不允许跳过 RED 直接写实现，也不允许用跳过测试的方式“通过”。
- 若 RED 阶段测试意外通过：说明行为已存在，写入 red_evidence 的说明，
  然后专注该任务真正缺失的部分。
