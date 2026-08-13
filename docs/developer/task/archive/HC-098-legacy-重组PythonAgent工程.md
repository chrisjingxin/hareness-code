---
id: HC-098-legacy
title: 重组 Python Agent 工程目录
priority: P0
status: 已完成
owner: Codex
branch: master
scope: 按 Host、Runtime、Thread、配置、Policy、工具、扩展与 Protocol 重组 Python package 和测试，拆出 Connection 与 Attachment 职责。
acceptance: Python import 只使用新的 canonical 路径，完整测试与 wheel 构建通过，CLI 启动入口和 JSON-RPC v3 行为不变。
user_docs: 不涉及
developer_docs: docs/developer/architecture/架构总览.md
test_evidence: cd packages/agent && .venv/bin/python -m pytest -q (516 passed, 1 skipped); bun run protocol:check; UV_CACHE_DIR=/tmp/za38-uv-cache uv build --wheel --out-dir /tmp/za38-wheel-check (成功，wheel 含新目录、Protocol 与 Prompt 资源)
references: 工作区目录重构（未提交）
completed_at: 2026-07-31
---

## 背景

`packages/agent/harness_agent/` 当前平铺数十个生产模块，Host adapter、Agent runtime、Thread 状态、配置、Policy 和工具只能依靠文件名前缀区分。测试也全部平铺，新增功能时难以判断应依赖哪个 module。

## 当前存在的问题

- `server.py` 同时保存 Host、Connection 和 WebSocket attachment 状态。
- package 根目录无法直接表达 module 的职责和依赖方向。
- 生成的 Protocol 文件、Prompt 资产和业务模块混放。
- 测试目录不镜像生产职责，跨 module 测试与局部测试难以区分。

## 为什么现在要修改

后续角色 Policy、共享资源和 delegation 都会增加 Python 模块。继续在 package 根目录扩展会让新实现进一步依赖 Host 私有细节，并放大每次改动的检索范围。

## 目标设计

```text
harness_agent/
├─ host/
├─ runtime/
├─ threads/
├─ config/
├─ policy/
├─ tools/
├─ extensions/
└─ protocol/
```

`AgentHost` 是组合入口；`RunCoordinator`、`ThreadPersistence` 和 `AgentEnginePool` 保持 deep module。Connection 与 attachment 从 Host implementation 中拆出，但不为每个 RPC 方法建立浅层 handler。

## 实施步骤

1. 建立职责目录并机械移动生产模块与测试。
2. 更新 Python import、mock patch target、协议生成路径和 wheel 资源配置。
3. 从 `server.py` 拆出 Connection 与 attachment 的状态和生命周期。
4. 增加最小 import 规则测试，阻止业务 module 反向依赖 Host。
5. 运行完整 Python 测试、Protocol 检查和 wheel 构建。

## 范围

- Python package、测试、生成器和打包路径。
- 不改变公开命令、配置和 JSON-RPC v3。

## 非范围

- 不实现 HC-092、HC-095 或新的 Agent 能力。
- 不按文件长度拆散现有 deep module。
- 不保留旧 import alias。

## 验收清单

- [x] package 根目录只保留入口文件和职责目录。
- [x] 旧的平铺模块 import 已删除，生产代码和测试只使用职责目录下的 canonical 路径。
- [x] Protocol 与 Prompt 资源进入 wheel。
- [x] Python 完整测试通过。
- [x] 无版本变更。

## 版本影响

无版本变更。此次迁移只调整 Python package 内部路径、测试归属和 wheel 资源路径，CLI 命令、配置格式与 JSON-RPC v3 保持不变。
