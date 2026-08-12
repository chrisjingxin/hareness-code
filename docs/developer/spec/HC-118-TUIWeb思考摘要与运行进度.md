# HC-118 TUI/Web 思考反馈与运行进度设计

关联任务：[HC-118](../task/archive/HC-118-TUIWeb思考摘要与运行进度.md)

## 通俗问题说明

模型第一次返回正文或 Tool 之前可能等待较长时间。当前两个界面只在那之后才出现动态变化，用户无法区分“模型仍在运行”和“进程已经卡住”。Harness Code 当前只支持 OpenAI Chat Completions；锁定版 adapter 不会保留非标准 reasoning 返回字段，因此本任务把安全的“思考反馈”定义为事实运行进度，不猜测性展示内部 reasoning。

目标是让用户在等待期间看到真实的运行阶段、活动时长和取消入口。运行进度不是 assistant 正文，也不能伪造“正在读文件”等未观测到的内部步骤。

## 已确认现状

- LangChain 版本锁定为 `langchain-core==1.4.8`、`langchain-openai==1.3.3`、`deepagents==0.6.8`。
- `ChatOpenAI` 默认使用 Chat Completions；`reasoning_effort` 是 Completions 请求参数，传 `reasoning` 或 `output_version="responses/v1"` 会触发 Responses 路径，本任务禁止这两个字段，并显式设置 `use_responses_api=False`。
- `ChatOpenAI` 官方 wrapper 不提取或保留第三方 `reasoning_content`、`reasoning_details` 等非标准 Chat Completions 返回字段；LangChain 归一化后的 `reasoning` 也可能是原始思维内容，不能展示。
- 当前支持协议没有可确认的公开 reasoning summary 形状，因此 Host 对任何 reasoning 内容 fail closed，只产生事实进度。
- Python Host 当前只把 `type=text` 组装为 `content.delta`；Interactive Core 的 Timeline 只有 message/tool/interaction；TUI/Web 都从同一个 snapshot 渲染。
- Transcript 捕获继续只读取安全的 text block；progress 属于运行期视图状态。

## 目标流程与关键 invariant

```text
run.started
  → run.progress(preparing, elapsed_ms)
  → model chunk
      ├─ type=text       → content.delta
      ├─ reasoning-only  → run.progress(model, elapsed_ms)
      └─ Tool chunk      → tool.started/delta/completed
  → run.completed/cancelled/failed
      → 清除 progress，保留正常终态摘要
```

关键 invariant：

1. `reasoning_content`、`reasoning_details`、规范化后的 `reasoning`、加密字段、未知字段和供应商私有载荷全部不公开，也不进入正文或 Transcript。
2. `run.progress` 只表示 Host 已进入准备或模型流阶段；UI 不得把它翻译成未观测到的具体执行步骤。
3. 事件按同一 Run 的 `sequence` 去重；重复/乱序帧不改变进度，终态事件清空临时状态。
4. subgraph namespace 仍 fail closed；工具输出继续走 Tool 事件。
5. reasoning effort 属于 `ModelSettings` 和 `model_settings_fingerprint`；同一个 Profile 的运行身份必须包含该配置。

## 公开 interface 与错误模式

### Protocol

新增事件：

```text
run.progress
payload = { phase: "preparing" | "model", elapsed_ms: non-negative integer }
```

事件沿用 v3 event envelope 和 sequence。进度不是持久化事件。

### Python Host

`_message_text()` 只接受显式 `type=text` block；存在 `content_blocks` 时不回退读取不透明对象，避免私有 reasoning 字段被误当正文。reasoning-only chunk 只触发 `run.progress`，不读取或转发 reasoning 文本。

### Interactive Core

`InteractiveState` 增加 `runProgress` nullable ephemeral 字段，`InteractiveSnapshot` 原样暴露给两个 adapter。新 Run 初始化为 `preparing/0`；接收 progress 更新它；任意终态、清空 Thread、恢复 Thread 或本地失败都会清除它。

### 模型配置

`ModelSettings.reasoning` 为可选结构，仅包含 `effort=low|medium|high`。显式配置时向 `ChatOpenAI` 传 `reasoning_effort`，保持 Chat Completions 请求；任何非法值在 config load 阶段抛 `ConfigError`，不配置时不发送该参数。

## 按依赖排序的实施步骤

1. 修改 `packages/protocol/schema/v3.json`，运行 `bun run protocol:generate`，再运行 `bun run protocol:check`。
2. 先加入 Python/TypeScript 红灯测试，覆盖 schema、Host translator、Core reducer 和两个 presenter。
3. 实现 Host 的 fail-closed reasoning 边界、progress 事件与安全 Transcript 读取。
4. 实现 Core ephemeral progress state、sequence/终态清理和 adapter snapshot parity。
5. 实现 TUI/Web 的运行态 spinner、时长、取消可达性、ARIA live 与 reduced-motion。
6. 实现 ModelSettings 解析、Chat Completions 参数和 fingerprint，并更新配置文档。
7. 运行 focused 检查，再运行 `bun run typecheck`、`bun run test`、`bun run project:check`，把结果写入任务。

## 可观察验收

- reasoning-only chunk 不进入正文、日志或 Transcript，只产生安全运行进度。
- 运行中显示 spinner、已运行时长、事实阶段和取消提示。
- 终态后临时进度消失，正常 run summary 保留；重复/乱序事件无重复进度。
- reduced-motion 下无强制动画；ARIA status/live 文案仍可被屏幕阅读器感知。
- `reasoning.effort` 改变会改变 Profile fingerprint；未配置时 adapter 不携带 `reasoning_effort`。
- 任何配置或代码路径都不向 `/responses` 发请求。

## 非范围

- Responses API、Responses `summary_text`、任何 raw CoT、encrypted reasoning、`reasoning_content` 或供应商私有字段的 UI、日志、Transcript、Thread 持久化。
- Provider 私有字段的猜测性兼容和 `extra_body` 旁路。
- HC-106 的协议/Core 设计和视觉主题迁移。
