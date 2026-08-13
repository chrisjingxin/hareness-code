---
id: HC-079
title: 提供本地 Web UI 与受令牌保护的 Gateway
priority: P1
status: 进行中
owner: codex
branch: master
scope: 在 CLI 包内增加仅监听 loopback 的本地 Web 界面，将浏览器 WebSocket 请求安全桥接到既有 Python sidecar JSON-RPC，并覆盖对话、流式事件、审批、Thread 和模型选择。
acceptance: 用户可通过本地随机令牌 URL 打开浏览器界面并完成一次可取消的 Agent run；未授权 WebSocket 被拒绝；API Key 不进入浏览器；CLI、TUI 和无头模式保持兼容。
user_docs: docs/user/Web界面.md、docs/user/快速开始.md
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: -
references: -
completed_at: -
---

## 范围

- 增加 `harness --web` 本地启动入口，固定监听 `127.0.0.1`，端口可显式配置。
- Gateway 复用 CLI 已有的 sidecar 生命周期和 `IpcClient`，不增加第二套 Agent 协议。
- 浏览器界面提供 Thread 列表、消息时间线、工具卡、审批/问答、模型选择、取消和上下文压缩。
- 页面与 WebSocket 使用进程级随机令牌；敏感配置、凭据和完整初始化对象不得发送给浏览器。

## 非范围

- 不提供公网部署、远程多人访问、账号系统或企业统一认证。
- 不改变 Python sidecar 的 JSON-RPC v2 契约。
- 不替代现有 TUI、无头模式或后续 IDE/桌面客户端。

## 验收清单

- 无令牌或错误令牌访问页面/WebSocket 时拒绝请求。
- 浏览器断开或 Gateway 关闭时，待处理审批安全拒绝并优雅关闭 sidecar。
- 文本、工具、终态和交互请求按 `thread_id/run_id/sequence` 投影到浏览器。
- Web 资源可由 Bun 构建并有 Gateway 消息校验和路由测试。
- 快速开始、安全说明和架构文档明确本地边界与启动方式。
