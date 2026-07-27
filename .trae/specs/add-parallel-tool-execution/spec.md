# 并发工具执行 Spec

## Why

当前项目底层依赖 LangGraph ToolNode，其异步路径通过 `asyncio.gather` 已天然支持对同一 AIMessage 中多个 tool_calls 的无差别并行执行。但实际体验为纯顺序执行，原因是：
1. 系统提示词未引导模型在单轮发出多个 tool_calls（模型总是逐个调用）
2. ToolNode 的 gather 是无差别的——不区分读写，缺乏并发安全保护
3. CLI 端未针对并发工具事件做展示优化

竞品（qwen-code、claude-code、opencode）均已实现不同程度的并发工具执行，显著提升了多文件读取、多命令执行等场景的响应速度。

## 架构原理

### ToolNode 并行执行机制（已有能力）

LangGraph ToolNode（`langgraph/prebuilt/tool_node.py`）的执行逻辑：

- **异步路径**（本项目使用）：`asyncio.gather(*coros)` — 将同一 AIMessage 的所有 tool_calls 协程同时启动
- **同步路径**：`executor.map(self._run_one, tool_calls, ...)` — ThreadPoolExecutor 线程池并行

关键特性：**无差别并行**，不做任何安全判定，有多少 tool_calls 就同时启动多少。

### 简单工具并行 vs 子 Agent 并行：同一机制

两者走完全相同的执行路径（ToolNode gather），区别仅在粒度：

| 维度 | 简单工具并行 | 子 Agent（task）并行 |
|------|-------------|---------------------|
| 模型发出的 tool_calls | `[read_file, read_file, grep]` | `[task, task, task]` |
| 执行体 | 原子操作，毫秒~秒级 | 完整 ReAct 循环，秒~分钟级 |
| 上下文 | 共享父 Agent 状态 | 隔离上下文（子 Agent 独立状态） |
| 并发安全 | 需要分类（写操作不能并行） | 天然安全（无共享状态） |

**不存在运行时分发器**。模型通过工具描述和 prompt 指导自行决定调用简单工具还是 task 工具。运行时的唯一职责是：对同一批 tool_calls 做并发安全分区，确保写操作不与其他工具并行。

### 设计原则

- **模型决定"调什么"**：通过 prompt 引导 + 工具描述，让模型在独立操作时发出多个 tool_calls
- **运行时决定"怎么安全地并行"**：并发安全分区在 ToolNode gather 之前拦截，将不安全的调用拆为顺序批次
- **复用而非重建**：不替换 ToolNode，而是在其上层添加分区逻辑

## 竞品调研摘要

| 项目 | 并行判定 | 执行策略 | 并发上限 |
|------|----------|----------|----------|
| qwen-code | 工具 Kind 白名单 + Shell 只读分析 | 连续安全工具合并为 parallel batch，非安全独立 sequential batch | 环境变量，默认 10 |
| claude-code | 每个工具声明 `isConcurrencySafe(input)`，Bash 动态判定 | 同 qwen-code 分区算法 + 流式提前执行 | 环境变量，默认 10 |
| opencode | 不做运行时判定，完全由模型决定 | 每个 tool-call 立即 fork 为 Fiber，无限制并发 | 无上限 |
| deepagents | 完全委托 LangGraph ToolNode | ToolNode 通过 asyncio.gather 无差别并行执行所有 tool_calls | 无显式上限 |

**核心结论**：ToolNode 的 `asyncio.gather` 已提供并行能力，本项目需要：
1. 通过 prompt 引导模型发出并行 tool_calls（当前模型总是逐个调用，这是体验为"顺序"的主因）
2. 在 ToolNode 之前添加并发安全分区层（防止写操作冲突）
3. CLI 端正确展示并发工具状态
4. 子 Agent（task）并行无需额外处理——天然并发安全，直接享受 gather 并行

## What Changes

- 在系统提示词中增加并行工具调用指导（引导模型对独立操作发出多个 tool_calls，对复杂多步任务发出多个 task 调用）
- 为内置工具添加并发安全分类元数据（`concurrency_safe` 标记）
- 在 ToolNode 之前添加并发安全分区层：将同一批 tool_calls 按安全性拆分为交替的 parallel/sequential 批次，再分别交给 ToolNode 的 gather 执行
- 确保 HITL 审批正确打包多个并行待审批工具
- CLI 端支持同时展示多个正在执行的工具状态
- 添加并发上限配置（环境变量 `HARNESS_MAX_TOOL_CONCURRENCY`，默认 10）

**注意**：不替换 ToolNode，不改变子 Agent（task）的执行路径。task 工具天然并发安全，直接享受 ToolNode 原生 gather 并行。

## Impact

- Affected specs: 无已有 spec 冲突
- Affected code:
  - `packages/agent/harness_agent/agent.py` — 工具注册时附加并发安全元数据
  - `packages/agent/harness_agent/prompts/` — 系统提示词增加并行指导（简单工具并行 + task 并行）
  - `packages/agent/harness_agent/concurrency.py` — 新增并发安全分区（ToolNode 前置层）
  - `packages/agent/harness_agent/server.py` — 事件翻译适配并发工具事件
  - `packages/cli/src/` — TUI 并发工具状态展示
  - `packages/protocol/` — 可能需要扩展 tool 事件 payload

## ADDED Requirements

### Requirement: 并发安全分类

系统 SHALL 为每个注册工具维护 `concurrency_safe` 分类：
- 只读工具（ls、read_file、glob、grep）：始终并发安全
- 写工具（write_file、edit_file）：非并发安全
- Shell 工具（execute）：动态判定，只读命令并发安全
- 子 Agent 工具（task）：始终并发安全

#### Scenario: 模型返回多个只读工具调用
- **WHEN** 模型在单次响应中返回 `[read_file, read_file, grep]` 三个 tool_calls
- **THEN** 三个工具并行执行，结果按原始 tool_call_id 关联返回

#### Scenario: 模型返回混合工具调用
- **WHEN** 模型在单次响应中返回 `[read_file, write_file, read_file]`
- **THEN** 执行顺序为：`read_file`（并行批次）→ `write_file`（串行）→ `read_file`（串行）

#### Scenario: Shell 命令动态判定
- **WHEN** 模型返回 `execute("git status")` 和 `execute("ls -la")`
- **THEN** 两者均判定为只读，并行执行

#### Scenario: Shell 写命令
- **WHEN** 模型返回 `execute("rm -rf /tmp/x")` 和 `execute("git status")`
- **THEN** `rm` 命令判定为非只读，两者串行执行

### Requirement: 系统提示词并行引导

系统 SHALL 在 system prompt 中包含并行工具调用指导，明确区分两种并行模式：
- 简单原子操作（读文件、搜索）：在同一轮发出多个简单 tool_calls
- 复杂多步任务（调研、实现）：在同一轮发出多个 task 调用委派给子 Agent

提示词 SHALL 明确指导：有依赖关系的操作必须顺序发出，不得并行。

#### Scenario: 多文件读取（简单工具并行）
- **WHEN** 用户要求查看多个文件
- **THEN** 模型在单次响应中发出多个 read_file 调用而非逐个调用

#### Scenario: 多方向调研（子 Agent 并行）
- **WHEN** 用户要求调研多个独立技术方案
- **THEN** 模型在单次响应中发出多个 task 调用，每个子 Agent 独立调研一个方向

#### Scenario: 有依赖的操作
- **WHEN** 用户要求"先读取配置文件，再根据内容修改代码"
- **THEN** 模型分两轮发出 tool_calls：第一轮 read_file，第二轮 write_file

### Requirement: 并发上限控制

系统 SHALL 支持通过环境变量 `HARNESS_MAX_TOOL_CONCURRENCY` 配置最大并发数，默认值为 10。

#### Scenario: 超过并发上限
- **WHEN** 模型一次返回 15 个只读 tool_calls 且并发上限为 10
- **THEN** 最多同时执行 10 个，剩余排队等待

### Requirement: CLI 并发工具状态展示

CLI SHALL 支持同时展示多个正在执行的工具的状态信息。

#### Scenario: 并行工具执行中
- **WHEN** 3 个 read_file 工具并行执行
- **THEN** TUI 同时显示 3 个工具的执行状态（名称、耗时）

### Requirement: HITL 并行审批

系统 SHALL 正确打包同一轮中多个需审批的工具调用为单个审批请求。

#### Scenario: 多个写操作需审批
- **WHEN** 模型返回 `[write_file, edit_file]` 且两者均需审批
- **THEN** CLI 收到包含 2 个 action 的单个审批请求，用户一次审批决定应用于所有挂起工具

## MODIFIED Requirements

无。

## REMOVED Requirements

无。
