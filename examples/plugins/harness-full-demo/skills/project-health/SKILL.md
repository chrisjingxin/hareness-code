---
name: project-health
description: 使用只读工具、Demo MCP 和 Demo LSP 检查项目结构、风险与测试入口
argument-hint: "[检查范围]"
allowed-tools: [read_file, glob, grep, lsp]
user-invocable: true
---

# Project Health

对用户指定的范围执行一次只读项目健康检查。

1. 先读取 README、主要包清单和测试入口，说明项目用途。
2. 调用名称包含 `demo_tools_plugin_inventory` 的 MCP 工具，确认已安装 Plugin 包的组件库存。
3. 如果工作区包含 `.demo` 文件，调用 `lsp` 的 `hover` 或 `definition` 验证 Demo LSP。
4. 只报告有文件证据的风险，不修改文件，不执行安装命令。
5. 最终按“概览、证据、风险、建议验证命令”四部分输出。

若 `$ARGUMENTS` 非空，将其作为优先检查范围。
