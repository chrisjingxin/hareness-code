# dcode 性能评测调研

调研日期：2026-09-07。目标是先确认 dcode 已经测了什么，再选择 Harness Code 的对比方法；本轮未运行真实模型或付费基准，也未新增评测实现。

## 结论与证据范围

dcode 已有三类互补评测：**运行时微基准、真实模型行为评测、Harbor 沙箱任务评测**。它们回答的问题不同，不能把 SDK 的分数、CLI 的启动时间和完整任务成绩混成一个“性能分”。

本地源码：`/Users/zhangjingxin/Code/OpenSource/deepagents`，commit `03436b369c0324498602fe6b7918cf36f3629d76`（提交日期 2026-09-02）。已读取根目录、`libs/code`、`libs/evals` 的 AGENTS；尝试 CodeGraph 后确认该 checkout 无索引，回退到定向源码检索。下文源码路径均相对此目录；行号对应上述 commit，可用 `https://github.com/langchain-ai/deepagents/blob/03436b369c0324498602fe6b7918cf36f3629d76/<路径>#L<行号>` 复核。

## 1. dcode 本体如何测运行速度

| 测量项 | 实际方法与指标 | 源码 |
|---|---|---|
| `--help` / `--version` | 新 Python 子进程执行命令，以 `perf_counter` 记录 wall-clock；各要求小于 1 秒 | `libs/code/tests/integration_tests/benchmarks/test_startup_benchmarks.py:139–185` |
| 关键模块导入 | 新进程内测 main/ui/config/skills.commands/tool_display 导入，各小于 1 秒 | 同文件 `196–236` |
| CodSpeed 导入趋势 | pytest-benchmark 重复测 app/main/config/Textual adapter 等，每轮清除指定 `sys.modules` 前缀 | `test_codspeed_import_benchmarks.py:33–62,73–185`（同一 benchmarks 目录） |
| 本地项目上下文检测 | 120 个源码文件的 dirty 混合语言仓库，真实 LocalShellBackend 执行检测脚本；2 次预热、10 轮、每轮 1 次 | `test_local_context_benchmarks.py:106–162,182–199` |
| 大输入边界 | 50 万行填充 Makefile 的限量预览、含 1 万项的目录列表；同样 2 次预热、10 轮 | 同文件 `165–251` |
| Python 中间件开销 | 静态 backend 排除 shell 开销，批量 1 万次 before_agent；每批 1000 次 prompt 组装，输入含 3 个 MCP server × 12 tools | 同文件 `254–315` |

注意：1 秒是防回归门槛，**不是公开实测成绩**。导入微基准每轮清模块缓存也不等于操作系统冷缓存；部分缓存驱逐本身处于计时范围。`--help`、`--version` 不代表 TUI 可输入、Agent 可调用或首 token 已到达。启动测试的内部 CLI subprocess 未检查 returncode，跨产品评测应同时检查退出码与输出正确性（`test_startup_benchmarks.py:157–171`）。

本地命令（在 deepagents 仓库根目录；环境需已安装）：

```sh
make -C libs/code benchmark     # 普通 pytest-benchmark，查看 min/max/mean/stddev
make -C libs/code bench         # CodSpeed walltime
make -C libs/deepagents bench   # SDK 微基准，另列
```

命令事实源为 `libs/code/Makefile:44–51` 与 `libs/deepagents/Makefile:44–51`。两个包均有 `bench-memory` 入口，但当前 nightly 没启用 memory job，不能据此声称已有持续内存评测。

CI 每日 09:00 UTC 跑 SDK 与 code 包，另支持手动触发；CodSpeed action 使用 `mode: walltime`。无每 PR 基准任务，回归阈值放在 CodSpeed dashboard 管理，文档提到的“当时全局 10%”不是当前可保证值。来源：`.github/workflows/_benchmark_nightly.yml:7–38`、`_benchmark.yml:59–72`、`libs/DEVELOPMENT.md:198–221`。[CodSpeed 结果入口](https://codspeed.io/langchain-ai/deepagents)。

SDK 自身还测 `create_deep_agent` 图构建（假模型，不执行模型/工具）、工具数 1/5/10/20 和子代理数 1/3/5/10 的扩展成本；摘要中间件测**未触发摘要**的每轮 token 计数常见路径，不是摘要生成速度。来源：`libs/deepagents/tests/benchmarks/test_benchmark_create_deep_agent.py:1–12,185–215`、`test_benchmark_summarization_middleware.py:1–20`。

## 2. 真实模型行为 evals

`libs/evals/README.md:3` 明确称其为 **SDK** 的端到端行为评测。当前 catalog 记载 136 个 eval 函数、8 个分类：文件操作、检索、工具使用、记忆、对话、摘要、unit_test、LangChain middleware；函数数不等于展开参数后的任务数（`EVAL_CATALOG.md:7–15`）。

常见流程是：创建 Agent → 注入初始文件与问题 → 真实模型执行 → 收集回答/文件变化/工具调用轨迹 → 断言结果并记录效率。例如读文件题要求回答第二行第三个词，并标注理想 2 步、1 次工具调用；它直接调用 `create_deep_agent`，没有启动 dcode。来源：`libs/evals/tests/evals/test_file_operations.py:15,33–49`。

| 指标 | 当前代码定义 |
|---|---|
| correctness | pytest call 阶段 passed / total，报告保留两位小数 |
| step_ratio | 所有有期望值的轨迹：实际步骤总和 / 期望步骤总和 |
| tool_call_ratio | 实际工具调用总和 / 期望工具调用总和 |
| solve_rate | 每个合格用例：通过则 expected_steps / duration_s，失败则 0，再求均值；**不是任务成功率** |
| median_duration_s | pytest call 阶段耗时中位数，包含该测试体内的工作；不是 TTFT |

来源：`libs/evals/tests/evals/pytest_reporter.py:52–104,228–239,359–380`。当前报告没有统一顶层 token/cost/latency_ratio 字段；不能把官方旧文章出现的指标当成当前全部落地字段。官方 [How we build evals for Deep Agents](https://www.langchain.com/blog/how-we-build-evals-for-deep-agents)（2026-03-26）解释了 correctness、step/tool call/latency ratio 与 solve rate 的设计思路。

已安装 eval 环境、设置匹配模型凭据及 `LANGSMITH_TRACING=true` / `LANGSMITH_API_KEY` 后，在 `libs/evals` 可运行：

```sh
deepagents-evals list evals --category file_operations
deepagents-evals run --model "$MODEL" --eval-category file_operations --eval-tier baseline --report evals_report.json
deepagents-evals trials --model "$MODEL" --trials 3
```

入口还支持 aggregate/radar/retry-failed；多次试验聚合 mean/median/stdev/min/max。重要陷阱：pytest reporter 会把“有用例执行但用例失败”的退出码 1 改成 0，正式 CLI 根据报告失败数量恢复失败语义，不应只解析底层 pytest returncode。来源：`libs/evals/AGENTS.md:1` 起的命令、环境、退出码与 schema 说明；`pytest_reporter.py:348–354`。

第三方内容也不能直接当完整榜单复跑：FRAMES/Nexus/BFCL v3 在 `external_benchmarks.py:54–75` 各选 5 个 ID；另有 MemoryAgentBench 数据、Letta Context-Bench adapter、100 题 DRBench adapter，以及需部署到外部 checkout 的 continual-learning-bench adapter。后两者更偏研究/持续学习，未必适合作为第一批 Coding Agent 对比题。来源：`libs/evals/EVAL_CATALOG.md:114–119`、`harbor_adapters/contextbench/vendor/README.md:1–11`、`datasets/drbench-evals/README.md:1–14`、`deepagents_clbench/README.md:3–17,30–48`。

## 3. 最接近两产品能力对比的是 Harbor

当前 Harbor workflow 默认 `agent_impl=dcode`，另外可选 bare/tau3；可选 Terminal Bench 2、2.1、tau3、Harbor Index，亦支持 dataset override。来源：`.github/workflows/harbor.yml:31–43,114–122`。

**dcode 路径已接生产 Agent 构造器**：`make_graph` 调用 `create_cli_agent`，使用真实 headless system prompt，设置 `interactive=False`、自动审批、禁用 ask_user/记忆/skills、启用 shell，并指定 sandbox 工作目录。因此它测的是 dcode 的 Agent harness，在 Harbor 的 LangGraph runner 下执行，**不经过 dcode TUI 或完整 CLI 进程启动**。来源：`libs/evals/deepagents_harbor/langgraph_project/langgraph_agent.py:277–335`。bare 路径则调用 `create_deep_agent`（同文件 `443–447`），必须在结果中注明选了哪条路径。

可复用的 Docker 命令（仅展示，未执行；`MODEL` 与凭据需另行配置；示例为 Anthropic）：

```sh
# 在 libs/evals
make stage-harbor-local-deps
uv run harbor run \
  --agent langgraph \
  --agent-kwarg project_path=deepagents_harbor/langgraph_project \
  --agent-kwarg config=langgraph.json \
  --agent-kwarg graph=dcode \
  --agent-env 'ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}' \
  --dataset terminal-bench/terminal-bench-2 \
  --model "$MODEL" -n 1 -l 5 \
  --jobs-dir harbor-jobs/comparison-smoke --env docker
```

来源：`libs/evals/CONTRIBUTING.md:409–424,477`；`-n` 是并发数，`-l` 才是任务数上限。源码 staging 会把当前 checkout 复制到 sandbox 安装目录，便于锁定评测版本（`libs/evals/Makefile:41–60`）。Harbor verifier reward 与 Agent trace 可同步到 LangSmith；缺失 reward 的历史导入工具会回填 0 并附原因，比较时应区分任务失败与环境/验证器失败（`deepagents_harbor/langsmith.py:513–560`）。

## 已有成绩能说明什么

- 官方 [Improving Deep Agents with harness engineering](https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering)（2026-02-17）报告固定 GPT-5.2-Codex，在 Terminal Bench 2.0 的 89 个任务上从 52.8% 提升至 66.5%，使用 Harbor、Daytona、LangSmith。这是特定历史配置的结果，不能作为当前 dcode commit 的基线。
- 官方 [Deep Agents v0.7](https://www.langchain.com/blog/deep-agents-v0-7)（2026-07-29）提到基础 prompt/schema 开销约 6k → 2k tokens，并与 v0.6.12 做三类任务对比。这是 SDK 上下文开销/特定试验，不能解释为每个完整任务总 token 降低三分之二。
- 当前源码提供 [Evals LangSmith](https://smith.langchain.com/public/d4245855-4e15-48dc-a39d-8631780a9aeb/d)、[Harbor LangSmith](https://smith.langchain.com/public/e5f44462-4615-49ba-a0a1-194892dd5837/d) 与 CI 入口（`libs/evals/README.md:9–14`）。本轮未从这些动态 dashboard 提取当前跑次原始成绩。
- 对 tracked code/SDK/evals 文件的定向检索未发现可直接作为当前基线的已提交 benchmark report、trials_summary 或 harbor-jobs 结果；AGENTS 中 JSON 数字是 schema 示例，`make radar` 默认也是 toy data（`libs/evals/Makefile:90–98`）。因此目前能确认“评测机制和历史公开结果”，不能给出“两者快多少”的数值。

## Harness Code 对比建议（待确定评测范围）

优先做同模型的完整任务对比，再单列运行时开销。Harness 已有 `-n` 无头入口及 JSON 结果路径，可用整 CLI adapter；未必需要先新增协议。来源：Harness `README.md:31`、`packages/cli/src/index.ts:356–375,427–430`。JSON 结果只含 text/threadId/runId/usage，不含首 token、事件流或 duration；整 CLI 耗时可由外层计时，细分则需采集 IPC AgentClient 事件。args 支持 `--cwd --json --config`（`packages/cli/src/args.ts:56–66`）。协议 `runCompleted` 有 duration_ms，usage 有 input/output/可选 cached，但需先验证是否包含子代理、摘要、重试的所有调用，才能用于成本比较（`packages/protocol/schema/v3.json:1604–1614`）。

Harness 当前 HEAD 为 `9591fd6a031e1853f4ad5a23a684f7b898004b7e`，工作区已有用户改动，未来运行需保留对应补丁和状态。其锁定 SDK 为 deepagents 0.6.8（`packages/agent/uv.lock:338–339`）；本地对照 dcode 包版本 0.1.65、SDK 源码包版本 0.7.12（各包 pyproject.toml）。产品整体比较应保留各自版本并完整记录。如果目标是归因自定义 harness 的贡献，再单独设计 SDK 版本受控实验，不通过强行升降依赖来替代产品比较。

建议首轮 20 道固定任务 × 每题 3 次独立重复 × 2 个产品 = 120 runs。题型包括文件精确修改、跨文件修改、定位并修复缺陷、补测试、长上下文检索；先以 2–3 题验证 adapter 和 verifier。逐题准备独立初始快照与外部隐藏验收测试，防止依靠 Agent 自报成功。

控制相同模型精确版本、网关、推理预算、超时、初始仓库、可见需求和机器资源；保留各自生产 system prompt/工具设计，作为产品能力的一部分。禁用个人记忆/Skills/MCP 污染，统一审批策略。随机交错产品运行顺序，避免一方总在网关低负载时运行。

报告应至少包含：任务正确率；同题配对结果；总耗时与成功任务耗时；全部调用 token 和按同一价目计算的费用；超时、模型错误、工具错误、环境失败的分布。总体正确率和预算先固定，避免“更快”来自少做事或提前失败。3 次重复是初步稳定性检查，20 题不是可宣称泛化排名的大样本。

另用无真实模型的固定响应流测 CLI 冷启动、Agent ready、首个可见响应、取消响应及整个进程树内存；单列统计 P50/P95 与原始样本，不能用 mock 结果宣称代码任务能力。仓库已有 `docs/developer/research/弱模型文件编辑评测.md` 和 HC-133 的 fixture 可借鉴，但其中 mock 证据不等于真实模型成绩。

如果接 Harbor，比较层级需对齐：两边都测生产 Agent runtime，或都走完整无头 CLI。不要用 dcode 的纯图构造路径对比 Harness 含 Bun 启动、IPC、Python sidecar 的总耗时，却将差异全部归为 Agent 算法。
