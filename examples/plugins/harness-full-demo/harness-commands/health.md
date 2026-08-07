---
description: 运行完整 Demo Plugin 的项目健康检查
argument-hint: "[目录或关注点]"
---

请按 `project-health` Skill 的流程检查当前项目。

优先关注：$ARGUMENTS

必须尝试调用 Demo MCP Plugin 库存工具；若工作区存在 `.demo` 文件，再调用 `lsp` 工具验证
Demo Language Server。禁止修改文件。
