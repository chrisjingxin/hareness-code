# 仓库协作规范

## 项目定位

Harness Code（命令名 `harness` / `za38`）是一个面向企业研发场景的终端 Coding Agent。用户在 Bun/OpenTUI 界面中发起对话和审批，CLI 通过 stdio 上的 JSON-RPC v3 驱动 Python sidecar，Python 端基于 deepagents、LangChain 和 LangGraph 完成模型调用、工具执行、Skill 加载、上下文管理与 Thread 持久化。

当前仓库处于源码开发阶段，不应将跨平台安装包或生产发布流程视为已交付能力。产品入口是 `README.md` 与 `docs/user/`；**面向开发者的过程文档一律放在 `docs/developer/`**。

## 发布阶段与兼容性原则

当前尚未正式发版，Python、TypeScript、Protocol、SQLite 以及内部模块之间没有外部兼容承诺。所有改造以消除技术债和保持单一 canonical 路径为第一优先级：不要为了旧内部 API、旧类名、旧参数、旧测试入口或旧数据形状保留 alias、wrapper、fallback、双写或死分支；应直接迁移调用方和测试并删除旧实现。只有用户或当前任务明确要求，或安全/数据不可逆风险确实需要时，才引入兼容或迁移逻辑，并在对应 Task 中说明原因和退出条件。

## 开发者文档目录（canonical）

面向开发者的过程文档只放在 `docs/developer/`，按职责拆分如下：

| 目录 | 职责 | 文档命名 |
|------|------|----------|
| `docs/developer/task/` | 功能源头：需求、范围、验收与状态 | `HC-XXX-功能简介.md` |
| `docs/developer/spec/` | 与 Task 一一对应的规格说明 | `HC-XXX-功能简介.md`（与 Task 同名） |
| `docs/developer/plan/` | 与 Spec 对应的实施计划 | `HC-XXX-功能简介.md`（与 Spec 同名） |
| `docs/developer/todo/` | 与 Plan 对应的可执行清单 | `HC-XXX-功能简介.md`（与 Spec 同名） |
| `docs/developer/architecture/` | 大功能架构文档（非 ADR） | 按大功能主题命名，中文 |
| `docs/developer/research/` | 竞品源码调研 | `XXX-功能简介.md` |
| `docs/developer/project/` | 工程工作流、检查清单、候选功能等 | 按主题命名 |

历史目录名 `docs/developer/task/`、`docs/developer/spec/` 已废弃；新工作一律写入上表路径。存量文档在触及时按同编号迁入新目录与新命名，不长期双写。

### 编号与功能简介

- 前缀固定为 `HC`（Harness Code）。
- `XXX` 为三位数字编号，从既有序列延续，不足三位左侧补零（例如 `HC-140`）。
- **必须**带功能简介：仅有编号无法区分主题。
- 功能简介使用中文，简短，**不超过 15 个汉字**，直接写在文件名中。
- 同一功能的 Task / Spec / Plan / Todo **文件名完全一致**，便于检索与交叉引用。
- Research 使用 `XXX-功能简介.md`（三位数字 + 中文简介），不强制 `HC-` 前缀；若调研直接服务某 Task，编号宜与该 Task 对齐。

示例：

```text
docs/developer/task/HC-140-斜杠命令补全.md
docs/developer/spec/HC-140-斜杠命令补全.md
docs/developer/plan/HC-140-斜杠命令补全.md
docs/developer/todo/HC-140-斜杠命令补全.md
docs/developer/research/140-斜杠命令补全.md
```

### 各目录细则

#### `task/` — 功能源头（最先产生）

- 一个完整功能对应一个 Task 文件；Task 是整条链路的起点。
- 回答：为什么做、用户最终得到什么、范围边界、什么算完成。
- 状态维护在 Task front matter，并由 `docs/developer/task/任务看板.md` 汇总整体状态。
- **已完成**的 Task 移入 `docs/developer/task/archive/`，不再出现在活动看板中；不得删除以抹掉历史。
- 生成 Task 前必须使用 `mattpocock:grill-me` 做需求确认，把歧义问透后再落盘。
- 简单需求不生成 Task，直接走「功能开发流程」的快速通道。

#### `spec/` — 规格（Task 确认后）

- 描述已确认的行为规格：目标流程、公开 interface、状态、错误语义、关键 invariant、可观察验收与非范围。
- 一项功能只维护一份 Spec；不按实施步骤或执行 Thread 再拆 Spec。
- 生成使用 `agent-skills:spec-driven-development`；模块/interface/seam 设计叠加 `mattpocock:codebase-design`。
- Spec 必须链接回对应 Task，不复制或替代 Task 中的原始用户结果。

#### `plan/` — 实施计划（Spec 确认后）

- 把 Spec 转为有依赖顺序的实现步骤：改什么、为什么、如何验证。
- 按真实依赖排序，不按 `cli/protocol/agent` 水平分层硬凑步骤；一次行为变更需跨层时保留在同一步，形成可验证的纵向结果。
- **必须标出可演示停点**：用户能在终端或界面上看到阶段性变化的位置（例如新颜色已生效、一种新组件已能画出来）。停点按真实可见效果切，不是「所有步骤做完」。
- 生成使用 `agent-skills:planning-and-task-breakdown`。
- Plan 不新增范围；范围变更必须先回写 Task/Spec。

#### `todo/` — 执行清单（Plan 确认后）

- Plan 的可勾选执行项；每项含明确动作与完成信号（改哪些相邻行为、跑哪条 focused test、期望观察到什么）。
- 每个可演示停点在 Todo 里单独成节，并写明用户怎么看（例如 `bun run dev` 后观察什么）。
- 文档命名与 Spec/Plan 一致。
- 与 Plan 一并使用 `agent-skills:planning-and-task-breakdown` 产出。
- 一个 Todo 项应能在不中途发明新设计的情况下完成；若仍写着「决定 / 评估 / 选择方案」，说明 Spec/Plan 未完成，不得进入实现。

#### `architecture/` — 大功能架构

- 面向**大功能**的长期架构说明，**一个大功能一份 Markdown**。
- **不使用 ADR 目录或「一决策一 ADR」流程**；架构结论直接写进对应大功能文档。
- 每次 Task 执行完毕后必须检视架构文档：
  - 若本任务引入了新的大功能边界 → 新建对应架构 MD；
  - 若已有对应大功能 MD → **增量修改**，不另起平行文档。
- `架构总览.md` 保持全局入口；细节落在各功能架构文档中。

#### `research/` — 竞品源码调研

- 针对某功能对照本地竞品源码的调研结论，服务 Spec/架构，不替代 Task 或 Spec。
- 竞品路径见下文「方案设计参考」；它们不是本仓库依赖，也不能覆盖 Task/Spec 中的用户决策。

#### `project/` — 工程过程

- 开发工作流、变更检查清单、新功能候选、依赖清单等与「单个功能交付物」无关的工程文档。

## 功能开发流程（双轨）

按需求体量分两条路径：复杂功能走完整文档链；简单需求走快速通道（见下文），不写过程文档、直接实现。

### 复杂功能：完整流程

一个功能从需求到合并固定经过：

```text
task → spec → plan → todo → implement → review
         │       │       │
         │       │       └─ agent-skills:planning-and-task-breakdown
         │       └───────── agent-skills:planning-and-task-breakdown
         └───────────────── agent-skills:spec-driven-development
                            + mattpocock:codebase-design

task 生成：mattpocock:grill-me
implement：agent-skills:test-driven-development
review：  agent-skills:code-review-and-quality
合并冲突：mattpocock:resolving-merge-conflicts
```

### 阶段职责

| 阶段 | 产出位置 | 职责 | 强制 Skill |
|------|----------|------|------------|
| **task** | `docs/developer/task/HC-XXX-….md` | 需求确认、完整用户结果、范围与验收；更新任务看板 | `mattpocock:grill-me` |
| **spec** | `docs/developer/spec/HC-XXX-….md` | 行为规格、interface、错误语义、invariant | `agent-skills:spec-driven-development` + `mattpocock:codebase-design` |
| **plan** | `docs/developer/plan/HC-XXX-….md` | 依赖有序的实现步骤与验证方式 | `agent-skills:planning-and-task-breakdown` |
| **todo** | `docs/developer/todo/HC-XXX-….md` | 可逐项执行的 checkbox 清单 | `agent-skills:planning-and-task-breakdown` |
| **implement** | 代码与测试 | 只实现已确认 Todo；TDD；做到可演示停点即停，更新勾选与证据 | `agent-skills:test-driven-development` |
| **review** | 复核结论写回 Task | 对照 Task/Spec/验收检视 diff、测试与文档 | `agent-skills:code-review-and-quality` |

合并代码或解决冲突时使用 `mattpocock:resolving-merge-conflicts`。

### 简单需求：快速通道

改动有限、不触碰契约的需求不生成 Task/Spec/Plan/Todo，需求确认后直接实现：

```text
需求确认（对话内）→ implement（TDD）→ 验证 → 提交
```

**判据（全部满足才可走快速通道，任一不满足回完整流程）**：

- 单包内改动：不触碰跨进程协议、SQLite schema、公开 interface、生命周期或数据形状；
- 改动面小：生产源码改动不超过 3 个文件（测试与文档不计）；
- 需求明确：对话内已无未决歧义，不需要方案权衡或竞品调研；
- 不改变用户可感知的行为边界（交互流程、错误语义、兼容策略）。

**规则**：

- 不写过程文档、不更新任务看板、不做停点交付；一次完成并交付。
- 质量底线不变：行为变更先写最小失败测试（TDD，规模随改动缩放）；相关 focused tests 与包级检查必须通过；用户可感知变更更新 `docs/user/`；提交沿用 Conventional Commit 并保持按包聚焦。
- **越界即回退**：实现中发现实际改动超出判据（动了协议/schema/跨包、需求膨胀、出现未决设计问题）→ 立即停下，回完整流程补 Task/Spec/Plan/Todo，不得在快速通道里硬做完。
- 快速通道原则上不产生架构文档更新义务；若确实触动了架构边界，视为越界，回完整流程。

### 流程不变量

1. **顺序不可跳**：没有确认的 Task 不写 Spec；没有确认的 Spec 不写 Plan/Todo；没有 Todo 不进入大规模实现。
2. **单一事实源**：用户结果与验收以 Task 为准；行为与 interface 以 Spec 为准；步骤以 Plan 为准；执行勾选以 Todo 为准。冲突时先修订上游文档，再改代码。
3. **实现不得扩 scope**：执行 Agent 不得自行改变范围、公开 interface、Protocol、数据形状、生命周期、错误语义或兼容策略。发现 Todo 与代码/Spec 冲突时停止实现，回写 Task 阻塞点，修订 Spec/Plan 后再继续。
4. **一个完整功能 = 一个 Task = 同名 Spec/Plan/Todo**。不为并行、缩短 context 或区分代码层而拆成多套文档树；功能过大时在同一编号下分阶段 Plan/Todo，由多个执行 Thread 接力。
5. **仅当**工作项关联性弱、各自具备独立用户价值、独立验收、独立上线且互不使对方处于不可用中间态时，才拆成多个平级 Task（各自完整走 task→…→review）。
6. Task 完成后：补齐测试与文档证据 → 更新状态 → 移入 `task/archive/` → 同步任务看板 → 检视并更新相关 `architecture/` 文档。
7. **实现按停点交付，禁止一次做完所有 Todo**：执行 Agent 一次只推进到下一个可演示停点（Plan 检查点，或一个用户已经能看见效果的工作包）。勾选本段 Todo、写明怎么查看、更新 `tmp/handoff.md` 后必须停下等用户看过；未经用户要求不得继续下一段。不得为了「把清单清完」连做整份 Todo。
8. **快速通道豁免的是过程文档与停点，不是质量底线**：简单需求判据与规则见「简单需求：快速通道」。测试、验证、提交规范与「越界即回退」在任何路径下都不可豁免；判据不满足时不得以快速通道名义绕过完整流程。

### 文档表达要求

Task / Spec / Plan / Todo 必须让不熟悉当前实现的人也能直接检视：

- 先用通俗语言说明：现在有什么问题、准备怎么解决、改完后有什么变化。
- 复杂流程优先写成 `输入 → 判断 → 执行 → 输出`，再补充文件、类型与方法名。
- 技术名词首次出现时说明用途；不得只罗列 module/class/interface 名称代替方案。
- 验收项必须是可观察结果，避免「完成重构」「优化架构」等无法验证的表述。
- 技术细节放在通俗说明之后；两者冲突时以便于用户检视为优先。

对照示例：

```text
不推荐：引入 ResolvedExecutionBinding 并统一多个 adapter。
推荐：模型选择只计算一次，运行、历史记录和界面都使用这同一个结果；实现上由 ResolvedExecutionBinding 保存该结果。
```

## Agent 开工顺序

1. 先按「功能开发流程」判据判定需求体量：简单需求直接进入实现，不读取 task/spec/plan/todo；复杂功能先读取 `README.md`、`docs/developer/architecture/架构总览.md`，以及当前功能对应的：
   - `docs/developer/task/HC-XXX-….md`
   - `docs/developer/spec/HC-XXX-….md`
   - `docs/developer/plan/HC-XXX-….md`
   - `docs/developer/todo/HC-XXX-….md`
2. 运行 `git status --short`，识别并保留用户已有改动；不得为清理工作区而回滚无关文件。
3. 确认当前所处阶段（task/spec/plan/todo/implement/review；快速通道需求无阶段概念），只做该阶段工作，使用该阶段强制 Skill。
4. 按变更归属选择包：界面和进程管理改 `cli`，跨进程契约改 `protocol`，Agent 与执行逻辑改 `agent`。
5. 修改前先找到邻近实现与现有测试；协议或生命周期变更必须同时验证 TypeScript 和 Python 两端。
6. 实现阶段使用最小相关测试快速反馈（TDD）。复杂功能到达 Plan 的可演示停点后停止，向用户说明如何查看阶段性成果（例如 `bun run dev` 后看哪一块）；未经用户要求不得继续后续工作包。快速通道需求可一次完成。整任务交付前再执行本文定义的项目级检查。
7. 任务完成后检视 `docs/developer/architecture/`：大功能新建或对已有文档增量更新。

## 项目结构与模块职责

- `packages/cli/`：`@za38/cli` TypeScript 入口；`src/tui/application/` 管工作流，`presentation/` 管视图，`platform/` 管终端和语法资源，`ipc/` 管跨进程客户端。
- `packages/protocol/`：跨进程共享的 TypeScript JSON-RPC 方法名和载荷类型。
- `packages/agent/`：`za38-agent` Python distribution workspace；`harness_agent/host/` 管连接和 Run，`runtime/` 构建 Agent，`threads/` 管持久化与上下文，其余职责按 `config/policy/tools/extensions/protocol` 分层。
- `packages/*/tests/`：包内测试并镜像源码职责；工程脚本测试位于 `scripts/project/`。
- `docs/user/`：最终用户的快速开始、配置、交互使用和故障排查。
- `docs/developer/`：开发者过程文档（见上文「开发者文档目录」）。
- `scripts/project/`：任务、文档与发布一致性检查的工程脚本。

表现层逻辑只能放在 `cli`，Agent/业务逻辑只能放在 `agent`，跨进程契约只能放在 `protocol`。

## 方案设计参考

下列本地 Coding Agent 源码可用于对照交互体验、会话恢复、工具事件流、Skill 与 Agent 架构的成熟实现；它们不是本仓库的依赖，也不能覆盖本仓库的 Task、Spec、架构约束和用户决策。Research 文档应优先基于这些路径做竞品源码调研：

- Oh My Pi：`/Users/zhangjingxin/Code/OpenSource/oh-my-pi`
- Qwen Code：`/Users/zhangjingxin/Code/OpenSource/qwen-code`
- Codex：`/Users/zhangjingxin/Code/OpenSource/codex`
- DeepAgents：`/Users/zhangjingxin/Code/OpenSource/deepagents`
- DeepSeek Reasonix：`/Users/zhangjingxin/Code/OpenSource/DeepSeek-Reasonix`
- Claude Code：`/Users/zhangjingxin/Code/OpenSource/claude-code`
- MiMo Code：`/Users/zhangjingxin/Code/OpenSource/MiMo-Code`
- Grok Build：`/Users/zhangjingxin/Code/OpenSource/grok-build`

## 构建、测试与开发命令

- `bun run dev`：开发模式运行 CLI 工作区入口。
- `bun run build`：构建全部 Bun 工作区包。
- `bun run typecheck`：检查 OpenTUI/TypeScript 类型。
- `bun run test`：运行工作区测试脚本。
- `cd packages/cli && bun test`：运行 TypeScript IPC/TUI 测试。
- `cd packages/agent && .venv/bin/python -m pytest -q`：使用项目虚拟环境运行 Python 测试。
- `bun run project:check`：同时检查文档链接、任务状态、生成看板和版本/Changelog 一致性。
- `bun run task:claim -- <ID> --owner <名称> --branch <分支>`：认领 Task。
- `bun run task:complete -- <ID> --evidence "<命令与结果>"`：记录证据并完成任务（完成后归档至 `task/archive/`）。
- `bun run version:set <SemVer>`：唯一允许修改根 `VERSION`、各包版本与 `CHANGELOG.md` 的入口。

## 代码风格与命名

TypeScript 使用 ESM、2 空格缩进；变量/函数使用 `camelCase`，类/类型使用 `PascalCase`。协议名称必须保持稳定字符串，例如 `stream/text`、`stream/done`。

Python 使用 4 空格缩进；模块/函数使用 `snake_case`，类使用 `PascalCase`，公开 API 必须有类型标注和简洁 docstring。Python 服务端 stdout 只能输出换行分隔的 JSON-RPC；诊断信息写入 stderr 或结构化日志。

维护中的 TS/TSX/Python 生产源码必须具有中文文件说明；类和公开方法/函数必须具有中文 JSDoc 或 docstring。复杂私有函数须在关键决策、状态转换、并发、终端兼容或安全边界处添加中文注释，说明意图而非复述代码。自动生成文件、第三方资源和行为命名测试仅保留来源或用途说明。

当前未配置格式化或 lint 工具。请遵循邻近代码风格，避免无关格式调整。

## 测试规范

Python 测试命名为 `test_<行为>`，Bun 测试命名为 `*.test.ts`。修改 IPC 时必须同时覆盖 Python 端派发/流式行为和 TypeScript 帧处理。优先使用 mock 模型或 mock HTTP 服务，测试中禁止使用真实模型凭据。

修改服务端生命周期时，必须补充取消、中断/恢复、畸形帧和终态错误事件的回归测试。

Codex sandbox 内依赖 loopback 监听、进程枚举或其他被禁宿主能力的测试无需运行，也不得把这类权限失败当作代码缺陷继续排查；记录跳过原因即可。仅当用户明确要求时，才申请在 sandbox 外补跑。

**实现阶段必须走 TDD**（`agent-skills:test-driven-development`）：先写失败测试，再写最小实现，再重构；行为变更不得无「先实现后补测」作为默认路径。

## Task 状态、看板与归档

- 仓库 Markdown 是任务与文档的唯一事实来源。
- 活动 Task 只编辑 `docs/developer/task/HC-XXX-….md`；整体状态汇总在 `docs/developer/task/任务看板.md`。
- 不得手写绕过检查去改看板语义以规避 `tasks:check`；认领、完成后应运行项目提供的 task 同步/完成命令。
- 状态建议：`待认领`、`进行中`、`阻塞`、`待验收`、`已完成`、`已过时`。
- `owner` / `branch` 表示当前认领者与实施分支，通过 `bun run task:claim` 写入；未认领固定 `owner: 未认领`、`branch: -`。
- **已完成** Task：证据与引用写全后移入 `docs/developer/task/archive/`，并从活动看板消失。
- **已过时** Task：在正文说明替代 Task/Spec，保留文件作历史，不删除。

活动任务默认每 14 天复核一次，并在认领前、Spec 变更后、相邻架构合并后、准备发布或关闭前复核。过期未复核的活动任务应被 `tasks:check` 拒绝。

### Task front matter（最小字段）

```yaml
---
id: HC-XXX
title: 功能简介（中文，≤15 字）
feature_area: 稳定的功能板块名称
priority: P0
status: 待认领
owner: 未认领
branch: -
reviewed_at: YYYY-MM-DD
review_due: YYYY-MM-DD
scope: 要完成的范围
acceptance: 可验证的验收结果
user_docs: 不涉及或具体路径
developer_docs: 对应 spec/plan/architecture 路径
test_evidence: -
references: -
completed_at: -
---
```

## 模型分工

- **强模型**：grill-me 需求确认、Spec/Plan/Todo 产出、跨包/Protocol/生命周期决策、复杂排障、code review。
- **快速模型**：按已确认 Todo 做实现、focused tests、机械迁移、文档同步与证据记录。复杂功能一次只做一个可演示停点；做完即停，等用户看过再接力下一段。简单需求（快速通道）可一次完成。
- 快速模型不得改变范围与公开契约；阻塞时停在 Todo/Task 记录点，交回强模型修订 Spec/Plan。
- 多个执行 Thread 可接力同一编号的 Task/Spec/Plan/Todo；Thread 数不等于 Task 数。一次执行不等于做完整份 Todo。

### 推荐 Skill 总表

| 用途 | Skill |
|------|--------|
| 需求确认 / 生成 Task | `mattpocock:grill-me` |
| 生成 Spec | `agent-skills:spec-driven-development` |
| Spec 中的模块与 interface 设计 | `mattpocock:codebase-design` |
| 领域语言 / 难逆转概念 | `mattpocock:domain-modeling`（按需） |
| 生成 Plan 与 Todo | `agent-skills:planning-and-task-breakdown` |
| 竞品或外部事实调研 | `mattpocock:research`；结论写入 `docs/developer/research/` |
| 设计问题需可运行验证 | `mattpocock:prototype`（不替代 Spec） |
| 实现（TDD） | `agent-skills:test-driven-development` |
| 复杂缺陷 | `mattpocock:diagnosing-bugs` |
| Review | `agent-skills:code-review-and-quality` |
| 合并冲突 | `mattpocock:resolving-merge-conflicts` |

## 协作与功能完成定义

一个功能只有同时满足下列条件才能标记完成并归档：

1. Todo 项已勾选，代码已实现；
2. 自动化测试已通过，证据已写入 Task；
3. 已用 `agent-skills:code-review-and-quality` 完成 review，结论写回 Task；
4. 用户可感知变更已更新 `docs/user/`（若涉及）；
5. 架构/协议/配置变更已更新 `docs/developer/architecture/` 或对应过程文档；
6. 任务状态、关联提交/PR 与版本影响已记录；无版本变更也须在 Task 中说明；
7. Task 文件已移入 `docs/developer/task/archive/`，任务看板已同步。

根目录 `VERSION` 是唯一版本来源。禁止手工修改任何分散版本字段或 `CHANGELOG.md` 顶部版本节；必须使用 `bun run version:set <SemVer>`，随后运行 `bun run release:check`。提交前运行 `bun run project:check`、`bun run typecheck` 和 `bun run test`。

## 提交与 Pull Request

沿用现有 Conventional Commit 风格，例如 `feat: Node IPC 客户端，JSON-RPC over stdio`、`fix: handle cancelled agent run`。提交应按包保持聚焦。

PR 必须说明影响层、行为变化、已运行测试、对应 `HC-XXX` 编号，以及配置或协议影响。OpenTUI 变更应附终端截图；新增环境变量或文件系统权限必须明确说明。

合并前若存在冲突，使用 `mattpocock:resolving-merge-conflicts` 处理，避免盲目覆盖任一侧的行为语义。

## 安全与配置

禁止提交 API Key、网关凭据或其他秘密。模型密钥优先通过 TOML 配置引用的环境变量提供；用户级 `~/.harness/config.toml` 可在权限受限时保存 `api_key` 降级值，但不得把该值写入仓库配置、日志、文档或交接记录。工作区路径、MCP 配置、shell 命令和流式工具输出均应视为不可信输入。

## 交接记录

所有 handoff 必须使用中文，并写入项目根目录的 `tmp/`；默认覆盖更新 `tmp/handoff.md`。`tmp/` 为本地忽略目录，交接记录不得提交。

交接内容须说明：当前 `HC-XXX` 与所处阶段、未提交改动边界、已完成验证、未运行检查、下一步与建议 Skill。已有 Task/Spec/Plan/Todo/架构文档不重复抄录，只引用仓库路径与 Todo 项。不得写入密钥、凭据或个人信息。

## 存量迁移说明

- 目录与脚本已迁到本规范：`tasks/` → `task/`，`designs/` → `spec/`，任务 ID 前缀 `ZC-` → `HC-`，文件名带功能简介。
- 已完成任务位于 `docs/developer/task/archive/`；活动看板由 `bun run tasks:sync` 生成。
- `docs/developer/architecture/adr/` 仅作历史资料；新架构结论写入对应大功能架构 MD。
- 新功能一律按「功能开发流程」双轨推进（复杂功能 task → spec → plan → todo → implement → review；简单需求走快速通道），不再在 Task 正文内嵌 Plan/Todo。
