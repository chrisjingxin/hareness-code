# dcode `/goal` 目标闭环调研

## 调研范围

本文回答 Deep Agents Code（下称 dcode）如何实现 `/goal`，覆盖命令入口、状态模型、持久化与恢复、执行循环、预算与终止条件、TUI 反馈和测试覆盖；并结合 Harness Code 当前架构，判断可复用部分、不可照搬部分以及建议的整合边界。

- 一手源码：`/Users/zhangjingxin/Code/OpenSource/deepagents`
- 固定提交：`03436b369c0324498602fe6b7918cf36f3629d76`
- 引用路径均相对上述源码仓库根目录，格式为 `文件:起止行`
- 方法：静态追踪 dcode TUI、server graph、LangGraph checkpoint、goal middleware 与 SDK `RubricMiddleware`，并交叉核对对应一手测试

## 结论摘要

dcode 的 `/goal` 不是一个后台任务调度器，也没有 token、金额或 wall-clock 预算。它由三层机制组成：

1. TUI 管理用户可控的目标生命周期与验收标准提案；
2. LangGraph checkpoint 保存权威状态，并把当前目标投影成模型可见、用户历史不可见的内部消息；
3. 每次带 active goal 的 Agent turn 自然停止时，`RubricMiddleware` 调用独立 grader。只有 `needs_revision` 会在同一 turn 内注入反馈并回到主模型；其他 verdict 或迭代上限会结束当前 turn。

因此，其持续性是“后续用户 turn 仍携带同一目标”，而不是“无需用户输入、跨 turn 一直运行到完成”。新建目标和执行 `/goal resume` 会触发隐藏 continuation；单纯恢复 thread 只从 checkpoint 重建状态和 review UI，不会自动开始新 turn。单次 turn 达到 grader 迭代上限后，目标仍为 `active`，等待下一次用户输入继续。

```text
/goal <objective>
  -> server-side criteria agent 生成 proposal
  -> 用户 review，或 Auto/YOLO 自动接受
  -> checkpoint 原子写入 accepted goal + notice
  -> 隐藏 continuation 启动主 Agent turn
  -> 主 Agent 自然停止
  -> grader 评估 rubric
       needs_revision -> 注入反馈 -> 回到主模型（受 max_iterations 限制）
       satisfied      -> 当前相关 turn 正常完成后提交 complete
       failed/error/达到上限 -> 结束当前 turn，目标通常保持 active
```

## 1. 命令入口与解析

### 1.1 单一注册入口

`/goal` 登记在统一命令注册表，说明为“持久 objective + acceptance criteria”，并带补全关键词和参数 hint。其 `BypassTier.QUEUED` 表示 Agent 忙碌、连接中或目标状态变更中时进入队列，不抢占当前执行：`libs/code/deepagents_code/command_registry.py:155-165`。

Chat composer 在首字符为 `/` 时进入 command mode，补全层从统一注册表生成候选；提交时恢复 `/`、写入输入历史并发出 typed `Submitted` 事件：`libs/code/deepagents_code/tui/widgets/chat_input.py:2440-2458`、`libs/code/deepagents_code/tui/widgets/chat_input.py:2655-2679`、`libs/code/deepagents_code/tui/widgets/chat_input.py:3076-3133`。

App 的统一消息分流最终把 `/goal` 与 `/goal ...` 交给 `_handle_goal_command`。启动参数 `--goal` 也复用同一处理函数，没有另一套语义：`libs/code/deepagents_code/app.py:14396-14508`、`libs/code/deepagents_code/main.py:2644-2654`、`libs/code/deepagents_code/app.py:10803-10821`。

### 1.2 子命令语义

`_handle_goal_command` 的解析规则如下：

| 输入 | 行为 |
|---|---|
| `/goal` | 展示当前状态；无目标时附完整 usage |
| `/goal show`、`/goal status` | 展示目标、状态、note、criteria 与 grader 配置 |
| `/goal amend <feedback>` | 根据当前 objective/criteria 生成修订 proposal |
| `/goal pause` | 保留目标与 criteria，停止其驱动工作和 grading |
| `/goal resume` | 恢复为 active 并发送隐藏 continuation |
| `/goal clear` | 清空 active、pending 与关联 rubric 状态 |
| `/goal model ...` | `/rubric model` 的别名，共用 grader 配置 |
| `/goal max-iterations ...` | `/rubric max-iterations` 的别名 |
| 其他 remainder | 整体视为新 objective，走新建/替换 proposal |

其中 `amend` 按首个 token 保留；`show/status/pause/resume/clear` 只匹配完整 remainder，所以 `/goal show me the money` 会被视为新目标：`libs/code/deepagents_code/app.py:14396-14508`。

## 2. 创建与修订流程

### 2.1 Criteria 不是客户端拼 prompt

TUI 只提交 typed `goal_criteria_request`，graph input 为 `messages: []` 加请求对象；criteria 生成在服务端 graph 内完成：`libs/code/deepagents_code/app.py:14678-14714`。

`GoalCriteriaMiddleware.before_agent` 在普通 Agent loop 之前拦截该请求，调用隔离的 criteria agent；成功时把 proposal 写入 `_pending_goal_*`，清掉请求与公开 rubric，并 `jump_to: end`，不会误跑主 coding agent：`libs/code/deepagents_code/goal_rubric.py:1369-1473`、`libs/code/deepagents_code/goal_rubric.py:1475-1583`。

Criteria agent：

- 使用结构化 `GoalProposal { objective, criteria }`；新建必须保留 objective，修订只改变反馈涉及部分：`libs/code/deepagents_code/goal_rubric.py:116-170`、`libs/code/deepagents_code/goal_rubric.py:174-234`。
- 最多携带最近 8 条父对话消息，并对单条、总文本和序列化大小做限制；内部 control message、tool/media 内容不会混入：`libs/code/deepagents_code/goal_rubric.py:87-91`、`libs/code/deepagents_code/goal_rubric.py:1306-1366`。
- 可使用只读 repository、`fetch_url`、可选 web search 和 MCP context tools；外部工具沿用 Manual/Auto/YOLO 审批政策：`libs/code/deepagents_code/goal_rubric.py:1017-1095`、`libs/code/deepagents_code/goal_rubric.py:1623-1745`。
- Context agent 出错、递归耗尽或没有可用 proposal 时，降级到无工具、仅依赖 goal 文本的 fallback agent：`libs/code/deepagents_code/goal_rubric.py:1495-1526`、`libs/code/deepagents_code/goal_rubric.py:1548-1583`、`libs/code/deepagents_code/goal_rubric.py:1748-1801`。

### 2.2 Proposal 审核与竞态边界

Manual 模式展示 inline review；YOLO 自动接受；Auto 是否自动接受由一次性偏好与配置决定：`libs/code/deepagents_code/app.py:13594-13609`、`libs/code/deepagents_code/app.py:14832-14945`。

Review 支持四种 typed 结果：接受、编辑 criteria 后接受、带反馈驳回并重新生成、取消。编辑/驳回是多行输入，Esc 先退出编辑态，再次 Esc 才取消 proposal：`libs/code/deepagents_code/tui/widgets/goal_review.py:37-78`、`libs/code/deepagents_code/tui/widgets/goal_review.py:202-381`。

Proposal worker、review future 和 active goal 分离。request ID、防重复集合、review resolution lock 与 queued application 防止旧结果覆盖新 proposal，或在当前 Agent turn 中途改写 checkpoint：`libs/code/deepagents_code/app.py:13611-13655`、`libs/code/deepagents_code/app.py:14816-14963`、`libs/code/deepagents_code/app.py:15106-15165`。

接受时若 graph 正忙，application 排到 turn 后安全边界；空闲时立即写 checkpoint。新建成功后自动发送隐藏 continuation 开始工作，修订/恢复也可继续现有工作而不要求用户重复 prompt：`libs/code/deepagents_code/app.py:15167-15326`。

## 3. 状态模型与所有权

### 3.1 生命周期

状态词固定为：

```text
active --用户 pause--> paused --用户 resume--> active
active --Agent blocker--> blocked --下一次用户输入--> active
active --当前成功 grading--> complete
任意可清理状态 --用户 clear--> 无目标
```

`active` 与 `blocked` 被投影为 actionable；`paused` 保留目标但不驱动工作；`complete` 是终态：`libs/code/deepagents_code/goal_state_limits.py:8-24`、`libs/code/deepagents_code/goal_state_notice.py:480-522`。

用户拥有 create/amend/pause/resume/clear。模型只得到一个写侧工具：

```text
update_goal(status: "complete" | "blocked", note: str)
```

模型不能创建、替换、暂停、恢复或清除目标。`blocked` 带 note 立即提交；`complete` 只把 evidence 放入 `_pending_goal_completion_note`，等待当前 rubric verdict，避免模型自报完成直接越过验收：`libs/code/deepagents_code/goal_tools.py:125-272`、`libs/code/deepagents_code/goal_tools.py:300-346`。

下一次用户发送普通消息或 Skill 调用时，blocked 会先原子恢复为 active，清除 live blocker note，并把 prior blocker 放进新 notice 后再允许 turn 启动；写入失败则消息不会发送：`libs/code/deepagents_code/app.py:15460-15483`、`libs/code/deepagents_code/app.py:17649-17672`。

### 3.2 Checkpoint channel

权威 goal/rubric 数据不依赖聊天文本，而是 LangGraph state channel：

| Channel | 含义 | 写入方 |
|---|---|---|
| `_goal_objective` | 已接受 objective | TUI |
| `_goal_status` | `active/paused/blocked/complete` | TUI 或 `update_goal` |
| `_goal_rubric` | 与 goal 绑定的 criteria | TUI |
| `_goal_status_note` | blocker 或 completion evidence | TUI/Agent |
| `_pending_goal_completion_note` | 等待 rubric verdict 的完成证据 | Agent |
| `_sticky_rubric` | TUI 持有的持久 rubric | TUI |
| `_pending_goal_objective/_rubric/_kind/_request_id` | 待 review proposal | server graph/TUI |
| `goal_criteria_request` | 一次 criteria 请求，输出时省略 | TUI graph input/server middleware |

这些 goal 字段通过 `PrivateStateAttr` 从公开 graph I/O schema 隐藏；完整声明及写入职责见 `libs/code/deepagents_code/resume_state.py:20-54`、`libs/code/deepagents_code/resume_state.py:200-285`、`libs/code/deepagents_code/goal_rubric.py:351-363`。

## 4. 存储、notice 与恢复

### 4.1 存储

本地 dcode 使用 LangGraph `AsyncSqliteSaver`，数据库位于状态目录的 `sessions.db`；dev server 生成的 checkpointer 也指向同一数据库：`libs/code/deepagents_code/sessions.py:341-374`、`libs/code/deepagents_code/client/launch/server_manager.py:121-163`。

TUI 通过 `configurable.thread_id` 调 `aupdate_state`。本地 graph 直接更新；远端先确保 thread 存在，再以 `as_node="model"` 更新。goal state 与可选 notice 在同一个 update 中写入：`libs/code/deepagents_code/app.py:13371-13423`。

所有脱离 Agent run 的状态变更都经过 `_goal_state_mutation_boundary`：先取得 goal lock，再等执行与 reconcile 静止，避免 checkpoint 写竞争。若 pause/resume/complete/blocker-reset 的写入失败，内存状态会回滚或保持可重试：`libs/code/deepagents_code/app.py:13358-13380`、`libs/code/deepagents_code/app.py:13953-14004`、`libs/code/deepagents_code/app.py:15328-15432`、`libs/code/deepagents_code/app.py:15460-15483`。

### 4.2 模型上下文 notice

模型不再通过 `get_goal/get_rubric` 读取状态。`GoalToolsMiddleware` 将权威 channel 投影为内部 `HumanMessage`：

- `lc_source="goal_state"`，用户 transcript、标题与派生历史会过滤；
- schema version + event ID + state fingerprint 标识当前状态；
- notice append-only，当前消息明确 supersede 旧消息，不改写可缓存的历史前缀；
- objective、criteria、note 经过 HTML escape 并放入明确边界；
- 新建、修订、恢复另有一次性 `goal_control` continuation；
- 状态未变但 notice 已落在 summarization cutoff 外时，会持久补写或只在当前 model request 临时 re-pin；超大的旧 notice 用等索引 bounded placeholder 替换，避免破坏 summary cutoff 的绝对下标。

证据：`libs/code/deepagents_code/goal_state_notice.py:28-56`、`libs/code/deepagents_code/goal_state_notice.py:228-338`、`libs/code/deepagents_code/goal_state_notice.py:480-542`、`libs/code/deepagents_code/goal_state_notice.py:625-779`、`libs/code/deepagents_code/goal_tools.py:275-288`、`libs/code/deepagents_code/goal_tools.py:348-507`。

### 4.3 恢复

Resume 直接从最新 checkpoint `state.values` 恢复 active goal、rubric、status、note、pending completion 与待审 proposal，不重放历史来重建状态。完整 pending proposal 会重新挂载 review，或按当前 Auto/YOLO 政策接受：`libs/code/deepagents_code/app.py:13694-13815`、`libs/code/deepagents_code/app.py:13817-13892`、`libs/code/deepagents_code/app.py:14901-14945`。

兼容与损坏处理是 fail-closed：

- 旧 checkpoint 有 objective 但没有 `_goal_status` 时，按历史语义视为 `active`；
- `_goal_status` 存在但值非法时，notice 与 TUI 都降级为 `paused`，并尝试回写修复；
- restore 中保存的旧 `satisfied` grade 只作显示数据，不能触发 complete；只有当前 live turn 的相关 grading run 可以完成目标。

证据：`libs/code/deepagents_code/goal_state_notice.py:442-477`、`libs/code/deepagents_code/app.py:13830-13892`。

## 5. 执行循环与完成判定

### 5.1 每个 goal-backed turn

普通发送前，TUI 选择 rubric：paused/complete goal 不传 rubric；one-shot rubric 优先；否则 active goal 的 criteria 作为本 turn rubric，并标记 `goal_backed_grading=True`。发送之前还会确保当前 goal notice 已持久化，否则拒绝启动 turn：`libs/code/deepagents_code/app.py:18199-18275`。

dcode 无条件装配 `GoalToolsMiddleware`，并在启用 goal criteria 时装配 `GoalCriteriaMiddleware`。另用 `ReliableRubricMiddleware` 包装 SDK `RubricMiddleware`，提供运行时 grader 模型、只读验证工具、审批和重试：`libs/code/deepagents_code/agent.py:2857-2878`、`libs/code/deepagents_code/agent.py:3224-3264`、`libs/code/deepagents_code/agent.py:3282-3409`。

SDK `RubricMiddleware` 只在主 Agent 自然停止（模型没有更多 tool call）后执行：

1. 独立 grader 读取 rubric 与受限 transcript/验证工具；
2. `needs_revision` 生成内部 `HumanMessage`，包含未满足项与 gap，并 `jump_to: model`；
3. `satisfied`、`failed`、`grader_error` 或 `max_iterations_reached` 不跳转，当前 run 结束；
4. 同一 grading run 冻结首个非空 criterion 名单，后续 grader 少报时拒绝无证据的 satisfied；
5. grader 异常记录为 `grader_error`，不伪装成 rubric 本身的 `failed`。

证据：`libs/deepagents/deepagents/middleware/rubric.py:2-10`、`libs/deepagents/deepagents/middleware/rubric.py:66-97`、`libs/deepagents/deepagents/middleware/rubric.py:606-718`、`libs/deepagents/deepagents/middleware/rubric.py:737-806`、`libs/deepagents/deepagents/middleware/rubric.py:1162-1272`、`libs/deepagents/deepagents/middleware/rubric.py:1274-1325`。

### 5.2 完成相关性

goal 自动完成同时要求：

1. TUI 在当前 live stream 观察到 goal-backed grading event；
2. Agent turn 正常完成，取消/异常 turn 的 grade 不算；
3. event 的 grading run ID 与 checkpoint 中 `_current_grading_run_id` 相同；
4. goal 仍为 `active`；
5. 没有等待应用的新 goal proposal；
6. checkpoint verdict 为 `satisfied`。

满足时，使用 Agent 暂存的 completion note；未调用 `update_goal(complete)` 时使用默认完成说明。`max_iterations_reached`、`failed` 或未满足会清除本次 pending completion 并保持 active；`grader_error` 保持 active，若已有 completion 请求则保留到下一 turn 重评：`libs/code/deepagents_code/app.py:14012-14080`、`libs/code/deepagents_code/app.py:18134-18154`、`libs/code/deepagents_code/app.py:18449-18462`。

## 6. 预算与终止条件

### 6.1 持久目标文本预算

| 项目 | 限制 |
|---|---:|
| objective 原文 | 8,000 字符 |
| rubric/criteria 原文 | 12,000 字符 |
| objective + criteria 原文 | 12,000 字符 |
| status note / prior blocker 原文 | 4,000 字符 |
| notice 内所有 HTML 转义后文本 | 16,000 字符 |

接受 proposal 时会同时做字段、联合原文和转义后 notice 校验，并为后续 status note 预留空间：`libs/code/deepagents_code/goal_state_limits.py:26-63`、`libs/code/deepagents_code/goal_state_limits.py:128-188`、`libs/code/deepagents_code/goal_state_limits.py:277-342`。

### 6.2 Criteria/grader 上下文预算

| 项目 | 限制 |
|---|---:|
| repository 工具调用 | 每次 criteria/grading operation 25 次 |
| repository read | 120 行、256,000 bytes |
| repository 单次工具结果 | 12,000 字符 |
| web search | 每次 operation 3 次 |
| context 工具结果累计 | 32,000 字符 |
| 父对话上下文 | 最近 8 条；单条 1,600；文本累计 6,000；序列化 12,000 |
| criteria context agent recursion limit | `25 * 2 + 2 = 52` |
| criteria fallback recursion limit | 8 |

证据：`libs/code/deepagents_code/_repository_bounds.py:37-44`、`libs/code/deepagents_code/goal_rubric.py:83-96`、`libs/code/deepagents_code/goal_rubric.py:489-653`、`libs/code/deepagents_code/goal_rubric.py:656-882`、`libs/code/deepagents_code/goal_rubric.py:1724-1801`。

### 6.3 Grader 迭代终止

默认 `max_iterations=3`，`/goal max-iterations N` 允许任意正整数，没有上限；`clear/default` 回到 SDK 默认：`libs/deepagents/deepagents/middleware/rubric.py:556-590`、`libs/code/deepagents_code/app.py:244-268`。

只有 `needs_revision` 会继续循环。最后一次仍为 `needs_revision` 时改写为 `max_iterations_reached` 并保留主 Agent 最后一条响应；`satisfied`、rubric `failed`、grader `grader_error` 立即终止：`libs/deepagents/deepagents/middleware/rubric.py:75-97`、`libs/deepagents/deepagents/middleware/rubric.py:778-806`、`libs/deepagents/deepagents/middleware/rubric.py:1227-1272`。

不存在 `/goal` 专属的 token、费用、时间预算，也没有 checkpoint 中的剩余预算或跨 turn 自动 scheduler 状态。

## 7. TUI 反馈

### 7.1 持久状态

Chat input 上方常驻 `GoalStatusPanel`；无目标隐藏，有目标显示 `Goal · status` 与 objective，blocked/complete 时附 note：`libs/code/deepagents_code/tui/widgets/goal_status.py:16-53`、`libs/code/deepagents_code/app.py:4714-4731`。

Status bar 同步显示 `Goal complete`、`Goal blocked`、`Goal paused` 或 rubric 状态。`/goal show` 还显示 pending review、criteria、状态 note、grader model 和 max iterations：`libs/code/deepagents_code/app.py:14619-14676`、`libs/code/deepagents_code/app.py:15434-15458`。

### 7.2 Grading 过程

Custom stream event 被格式化为：

- `Checking acceptance criteria...`
- `Acceptance criteria satisfied`
- `Acceptance criteria not yet satisfied`
- `... (iteration limit reached)`
- `Rubric is invalid or cannot be evaluated`
- `Acceptance criteria check failed`

非 satisfied 结果带可展开详情：grader explanation、已满足项、未满足项/gap 及下一步。goal 仍 active 且达到上限时明确提示需后续 prompt 继续：`libs/code/deepagents_code/tui/textual_adapter.py:637-704`、`libs/code/deepagents_code/tui/textual_adapter.py:707-779`、`libs/code/deepagents_code/tui/textual_adapter.py:2262-2311`。

Agent 通过 `update_goal` 首次进入 blocked/complete 时，还会在 transcript 单次播报，避免仅靠 tool row 或状态栏：`libs/code/deepagents_code/app.py:13925-13951`。

## 8. 测试覆盖

### 8.1 已覆盖

当前提交的直接测试密度较高。静态统计至少包括：

- dcode 七个 goal-focused 文件共 115 个具名测试函数；
- `test_app.py` 中名称含 goal 的 96 个测试函数；
- SDK `test_rubric_middleware.py` 中 109 个测试函数。

这些是函数数，不是参数化展开后的 test case 数，也不代表代码覆盖率。

覆盖主题与代表性证据：

| 主题 | 一手测试 |
|---|---|
| 文本限制、HTML 转义、联合预算 | `libs/code/tests/unit_tests/test_goal_state_limits.py:20-118` |
| notice 格式、fingerprint、旧 schema、summary re-pin、内部消息过滤 | `libs/code/tests/unit_tests/test_goal_state_notice.py:41-590` |
| `update_goal` 的 blocked/paused/complete/超长拒绝与 notice middleware | `libs/code/tests/unit_tests/test_goal_tools.py:28-790` |
| append-only notice 与 compaction reconciliation | `libs/code/tests/unit_tests/test_goal_state_persistence.py:25-284` |
| criteria prompt、server checkpoint proposal、HITL、fallback、路径安全 | `libs/code/tests/unit_tests/test_goal_rubric.py:127-1193` |
| 客户端 request 相关性、terminal cleanup、无客户端模型构造 | `libs/code/tests/unit_tests/test_goal_criteria_client.py:25-249` |
| inline review future、编辑、驳回、取消、键盘行为 | `libs/code/tests/unit_tests/tui/widgets/test_goal_review.py:46-263` |
| TUI 偏好、恢复、写失败回滚、completion 相关性、blocked 自动恢复 | `libs/code/tests/unit_tests/test_app.py:760-1058`、`libs/code/tests/unit_tests/test_app.py:6147-7012`、`libs/code/tests/unit_tests/test_app.py:7503-9115` |
| Rubric loop、结构化输出、grader error、迭代上限、真实 graph 跳转 | `libs/deepagents/tests/unit_tests/middleware/test_rubric_middleware.py:186-1320`、`libs/deepagents/tests/unit_tests/test_end_to_end.py:4541-4619` |

### 8.2 明确缺口

- 未找到“真实 `AsyncSqliteSaver` 写入 goal -> 关闭进程 -> 重新构图 -> resume”的 goal 专属端到端测试；目标持久化测试主要使用 mock updater 或 `InMemorySaver`。
- `test_goal_rubric.py` 对路径安全和工具 allowlist 有测试，但文件末尾为 repository/web/context 调用次数预算预留的测试类当前为空：`libs/code/tests/unit_tests/test_goal_rubric.py:1193-1198`。
- 没有 token、金额或 wall-clock budget 的测试，因为 `/goal` 没有这些状态与终止语义。
- 本次未运行完整 deepagents test suite；源码工作树没有现成虚拟环境，而直接测试需要额外依赖。上述结论来自固定提交上的实现与一手测试交叉阅读，不以测试运行结果冒充验证。

## 9. 对整合分析最重要的事实边界

后续评估其他产品是否能整合该能力时，不能把 `/goal` 简化为“保存一段 prompt”。dcode 的正确性依赖以下耦合关系：

1. checkpoint channel 是权威状态；notice 只是模型上下文投影；
2. pending proposal 与 accepted goal 分离，并用 request ID 处理过期结果；
3. goal 状态写入必须与 Agent/checkpoint 执行串行化，失败必须回滚；
4. completion 必须绑定当前正常完成的 grading run，不能信任恢复出的旧 satisfied；
5. rubric loop 只在单次 turn 内自迭代，跨 turn 持久目标不等于后台自主续跑；
6. UI 需要同时表达 proposal review、常驻状态和 grading 过程，才能让用户知道系统是在工作、等待审核、blocked，还是达到迭代上限。

## 10. Harness Code 整合结论

### 10.1 总体判断

**可以整合，产品与现有运行时的契合度高，但不能把 dcode 的实现直接搬进 Harness。** 推荐把它定义为 Build 模式下的“跨 Run 持久目标 + 单次 Run 内验收修订闭环”，由 Harness 自己持久化目标事实，只复用或升级 DeepAgents 的 rubric middleware。

| 维度 | 判断 | 原因 |
|---|---|---|
| 用户价值 | 高 | 让用户不必在每轮重复目标和完成标准，并把“模型说完成”升级为独立验收结果 |
| Harness 架构适配 | 高 | 已有统一 Slash Registry、Host 受理边界、Thread SQLite、冻结 Context snapshot、shared TUI/Web controller 与 Agent middleware 装配点 |
| 直接移植 dcode | 不可取 | dcode 以稳定 LangGraph thread checkpoint 为真源；Harness 根图 checkpoint 是 execution-scoped |
| 复用 DeepAgents rubric | 可行但有版本风险 | Harness 已固定 `deepagents==0.6.8`，有基础循环；dcode 当前源码使用的新版接口和可靠性包装更丰富 |
| 实施复杂度 | 中高 | 会同时触及 `protocol`、`agent`、`cli`、SQLite schema、Run 生命周期和恢复语义，必须走完整 Task → Spec → Plan → Todo 流程 |

首版建议：只支持 Build mode；一个 thread 同时最多一个 accepted goal 和一个 pending proposal；criteria 必须由用户确认；默认单次 Run 最多 3 次 grading，另设安全硬上限；目标在达到上限后保持 active，但不跨 Run 后台自启。

### 10.2 与 `/plan`、Compose 的边界

`/goal` 不应成为 `/plan` 的别名，也不应复用 ComposeWorkItem 作为存储实体：

- `/plan` 是当前 Run 的执行约束与审批模式；`/goal` 是跨 Run 的用户结果和完成标准。
- Compose 已有独立的长期 Work Item、Task/Spec/Plan/Todo、验证和用户停点，`ComposeWorkItem.goal` 只是这条研发流程的一部分：`packages/agent/harness_agent/compose/models.py:237-259`。
- 给 Compose 无条件套上 rubric 自循环，会绕过“到可演示停点必须停止等待用户”的仓库流程不变量。

因此首版 `/goal` 应通过 Command Registry 的 `workModes: ["build"]` 隐藏于 Compose。后续若要让 Compose 展示统一目标状态，也应做只读投影或明确的领域映射，而不是让两套生命周期共用一张状态表。

## 11. 不能照搬：Checkpoint 身份不同

dcode 的关键前提是多个用户 turn 复用同一 `thread_id` checkpoint，所以 `_goal_objective` 等 private channel 能自然跨 turn 存活。Harness 刻意使用 execution-scoped 根图 checkpoint：`ExecutionRef.checkpoint_thread_id()` 把 project、thread、run 和 execution 一起做摘要，见 `packages/agent/harness_agent/runtime/execution_binding.py:334-357`。下一个顶层 Run 会得到不同的 checkpoint identity。

如果照搬 dcode channel，会产生两个问题：

1. 新 Run 无法天然看到前一 Run 的 goal private state；
2. 若再把状态复制到 Harness SQLite，就会出现 checkpoint 与 ThreadPersistence 两个真源，恢复、撤销和失败回滚都可能分叉。

所以建议不变量是：

> Goal、proposal、status 与 evaluation record 的唯一真源是 Harness SQLite；LangGraph state 只保存当前 execution 的 rubric 工作状态，Run 完成后不得反向成为长期真源。

Harness 的 `ThreadPersistence.accept_run()` 已在事务中登记 binding、Context snapshot、thread index 与用户记录，适合作为 Run 与目标 revision 绑定的原子边界：`packages/agent/harness_agent/threads/thread_persistence.py:3195-3265`。目标 mutation 也应沿用同一数据库锁、`BEGIN IMMEDIATE`、稳定错误码和 CAS revision，而不是由 TUI 直接写 graph checkpoint。

## 12. 推荐的 Harness 架构

```text
TUI / Web `/goal ...`
  -> CommandRegistry + CommandFeature（只解释语义，不保存业务状态）
  -> typed Goal RPC / event
  -> AgentHost GoalService
  -> ThreadPersistence SQLite（唯一真源，revision/CAS）
       ├─ pending proposal + review decision
       ├─ accepted goal + lifecycle status
       └─ correlated evaluation records
  -> Run preparation 读取当前 revision
       ├─ 生成 goal.state Dynamic ContextBlock
       └─ 把 criteria 作为本 Run rubric graph input
  -> RubricMiddleware 单次 Run 内 grade/revise
  -> custom stream 映射为 typed evaluation event
  -> 正常 Run 终态时由 Host 校验相关性并提交 complete/active
  -> selector 投影到 TUI 与 Web 的 Goal panel
```

### 12.1 最小持久模型

不建议一开始抽象成通用 workflow engine。三组相邻事实足够：

- `thread_goal`：`goal_id`、`thread_id`、objective、criteria、`active|paused|blocked|complete`、note、revision、时间戳；
- `goal_proposal`：`proposal_id`、create/amend kind、base goal revision、objective、criteria、pending/accepted/rejected/cancelled；
- `goal_evaluation`：`goal_id + goal_revision + run_id + grading_run_id`、iteration、result、evidence、grader profile fingerprint。

`complete` 的 CAS 条件至少包括：目标仍为 active、goal revision 未改变、没有待应用 amendment、Run 正常完成、observed grading run 与持久 evaluation 相同且 verdict 为 `satisfied`。恢复出的历史 `satisfied` 只能显示，不能再次驱动状态转换。

### 12.2 模型上下文投影

Harness 不需要复制 dcode 的 synthetic `HumanMessage`、fingerprint 与 summary re-pin 机制。`ContextLifecycle.prepare()` 已支持仅在当前 Run 有效的 `dynamic_blocks`，并对 DYNAMIC/RUN authority 做校验：`packages/agent/harness_agent/threads/context_lifecycle.py:235-319`。`AgentHost._prepare_run()` 正在此处构建 frozen snapshot，是加载当前 goal revision 的自然接缝：`packages/agent/harness_agent/host/agent_host.py:1430-1517`。

建议新增一个有字符预算、明确标记为低权限数据的 `goal.state` ContextBlock，包含 objective、criteria、status、revision 和 prior blocker。`RunContextSnapshotMiddleware` 会在每次模型调用边界重新注入同一份冻结 prompt，天然覆盖单次 Run 内所有 revision iteration，也不会污染用户 transcript：`packages/agent/harness_agent/runtime/run_context.py:174-207`。

这个设计还保留了一个重要一致性：Run 一旦受理，就使用该 Run 绑定的 goal revision；运行中接受 amendment 只能排到下一个安全边界，不能悄悄改写正在执行的验收条件。

### 12.3 Criteria proposal

Criteria 生成应当是 Host 管理的 typed execution，而不是把隐藏 prompt 伪装成普通用户消息。它可以复用现有 Managed Agent 的模型、策略、取消和资源治理，但必须使用只读工具视图，并输出结构化 proposal。默认流程为：

```text
/goal <objective>
  -> goal.propose
  -> 只读 criteria agent
  -> proposal event
  -> 用户 accepted / edited / rejected / cancelled
  -> Host CAS 应用 proposal
  -> accepted 后显式启动一次 continuation Run
```

首版不建议照搬 dcode 在 Auto 模式下跳过 review。验收条件决定何时自动判定完成，企业场景中应先保持人工确认；未来若开放自动接受，应成为单独的、可审计的用户偏好。

### 12.4 Rubric 输入和事件

Harness 的 managed executor 已允许 mapping 作为 graph input 原样传入，因此 active goal 的 Run 可以提交 `messages + rubric`，无需把 rubric 塞进用户消息：`packages/agent/harness_agent/runtime/managed_agent_executor.py:803-808`。

当前执行流只订阅 `messages` 与 `updates`：`packages/agent/harness_agent/runtime/execution_stream.py:195-207`。dcode 的 rubric 过程依赖 custom stream event；若要可靠展示 `checking / needs_revision / satisfied / max_iterations` 并持久化每次 evaluation，Harness 需要把 `custom` 纳入 stream mode，并在 `translate_stream_event` 邻近位置转成自己的 typed domain signal，不能让 CLI 直接解析 DeepAgents 私有 payload。

### 12.5 Middleware 与模型身份

`create_harness_agent()` 已集中装配 middleware，并在末尾加入 `RunContextSnapshotMiddleware` 与 `ContextWindowMiddleware`：`packages/agent/harness_agent/runtime/agent.py:715-735`、`packages/agent/harness_agent/runtime/agent.py:1255-1279`。Rubric middleware 应在这里显式装配，并通过顺序测试证明：

- grader revision 消息能被 context window 正确计入；
- frozen goal context 每次模型调用仍存在；
- grader 工具绝不继承主 Agent 的写权限；
- summarization 不会吞掉验收所需的原始证据。

Harness 当前固定 `deepagents==0.6.8`：`packages/agent/pyproject.toml:7-16`。该版本已经包含基础 `RubricMiddleware`、grader error、iteration event、transcript budget 和 20 次硬上限，因此可以做最小原型；但 dcode 当前仓库使用的新版实现提供了 grader middleware/context/state schema、消息和 grader state 构造钩子、runtime context 透传以及更严格的 criterion coverage。

生产实现前必须先做兼容性 spike：要么升级 DeepAgents 并跑完整 Agent/Compose/IPC 回归，要么在 0.6.8 上做 Harness-owned 的窄适配。不要把 dcode 的 `ReliableRubricMiddleware` 和整段 SDK 源码复制进仓库。若引入独立 grader 模型，必须把 `grader` role 的模型配置指纹纳入 `AgentEngineProfile.model_roles`，否则共享 engine 与 Run 审计无法证明实际使用了哪个 grader：`packages/agent/harness_agent/runtime/agent_engine_profile.py:57-123`、`packages/agent/harness_agent/runtime/agent_spec.py:280-310`。

## 13. Protocol 与 UI 建议

Harness 已有统一的 `CommandRegistry`、availability 规则和稳定 ID 分派，见 `packages/cli/src/interactive/commands.ts:1-160` 与 `packages/cli/src/interactive/command-dispatcher.ts:1-111`。因此 `/goal` 不应落成 TUI-only switch，也不应把命令原文透传给模型。

建议的行为面，而非预先冻结的最终 RPC 名称：

| 命令 | Host 行为 | 是否启动 Agent Run |
|---|---|---|
| `/goal`、`show/status` | 读取 accepted goal + pending proposal + latest evaluation | 否 |
| `/goal <objective>` | 创建 criteria proposal | 独立只读 proposal execution |
| `/goal amend <feedback>` | 基于当前 revision 创建 amendment | 独立只读 proposal execution |
| review accept/edit/reject/cancel | CAS 更新 proposal/goal | accept 后可启动 continuation |
| `/goal pause`、`clear` | 原子 mutation | 否 |
| `/goal resume` | 原子改为 active | 是，显式 continuation |

Protocol 需要同时覆盖请求、snapshot projection、proposal review interaction 与 evaluation stream。TUI 和 Web 只消费同一 selector：输入框上方显示 objective/status/note，status view 显示 criteria、revision、grader 与迭代上限，Timeline 显示每次 evaluation 的简洁状态与可展开 gap。

## 14. 安全、资源与失败语义

必须保留以下不变量：

1. Goal 和 criteria 是低可信数据，不能扩大 EffectivePolicy、Sandbox、workspace roots、工具清单或审批模式；
2. criteria agent 与 grader 默认只读，工具次数、返回字符数、上下文消息数和总字符数都必须有 Host 侧硬预算；
3. objective、criteria、note 与最终 ContextBlock 均有独立和联合字符上限；
4. `max_iterations` 除默认值外必须有安全硬上限，不照搬 dcode 当前“任意正整数”的入口；
5. timeout、provider error 和 malformed structured output 归为 `grader_error`，不得误报为任务失败或完成；
6. cancel/failed Run 的 grade 不得提交 complete；持久化失败必须保持旧 revision，并向所有客户端返回稳定错误；
7. `/goal clear` 删除的是当前目标事实和待审 proposal，不应删除 transcript、文件变更或历史 evaluation audit。

## 15. 推荐交付切片与验收

这是跨包、协议和 SQLite schema 变更，不符合快速通道。建议后续先新建一个完整 HC Task，再按三个可演示停点实施：

1. **持久目标与恢复**：完成 GoalStore、typed RPC、`goal.state` ContextBlock、Build-only `/goal show/pause/resume/clear` 和共享 panel。验证：关闭并重启 Host 后仍恢复相同 revision；新 Run 能看到目标，transcript 中没有内部 notice。
2. **Proposal 与人工审核**：完成只读 criteria agent、typed review、request/revision 竞态和 accepted continuation。验证：旧 proposal 不能覆盖新 revision；拒绝可再生成；写失败不改变 active goal。
3. **Rubric 闭环**：完成 middleware、custom event 翻译、evaluation audit、blocked/complete 相关性和迭代硬上限。验证：`needs_revision` 只在同一 Run 内续跑；达到上限后 Run 结束且 goal 仍 active；只有当前正常 Run 的 satisfied grade 能完成目标。

在进入 Task 之前还应做一个短期技术验证：用 Harness 当前 0.6.8 跑最小真实 graph，确认 `rubric` mapping input、custom event、context window 与 checkpoint cleanup 的组合行为；再以同一测试矩阵评估升级到 dcode 当前 DeepAgents 版本的回归成本。这个 spike 的结果将决定“升级 SDK”还是“保留 0.6.8 并做窄适配”，不应在 Spec 中预设答案。
