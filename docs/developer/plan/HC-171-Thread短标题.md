# HC-171 Thread短标题实施计划

关联：[Task](../task/HC-171-Thread短标题.md) · [Spec](../spec/HC-171-Thread短标题.md) · [Todo](../todo/HC-171-Thread短标题.md)

不新增范围。实现严格按 Todo 执行，一次只推进到下一个可演示停点；每到停点必须更新 Todo 证据与 `tmp/handoff.md`，然后停下等用户验收。未经用户要求不得继续下一停点。

## 通俗怎么拆

先让用户能 `/title` 改名，并在现有 `/resume` 和 Web 侧栏看到这个名字（没有标题时仍显示截断原文）。再让第一条消息并行起名，失败保持原文，用户改过的不能被盖掉。

```text
停点 1  /title 改当前 thread，/resume 与 Web 侧栏立刻显示
  │
停点 2  第一条消息并行起名 + 用户覆盖 + 用户/架构文档
```

停点按用户能看见的行为切。同一停点里协议、存储、命令和两端列表必须一起通，否则会出现「RPC 有了但列表还在用 first_message」的中间态。

## 已确认的代码事实

- `ThreadSummary` 没有 title。Web 侧栏主标签是 `first_message || latest_message || 「（无标题）」`；TUI `ThreadPicker` 主标签是 `firstMessage`。两端搜索都不含独立标题字段。
- 存储：`PRAGMA user_version = 19`。`harness_threads` 列是 fingerprint / thread_id / 时间 / first_message / latest_message / message_count。列契约在 `_migration_table_sql_contract`、bootstrap DDL、`_migration_validate_final_schema_sync` 等多处重复，改列必须一起改。
- `list_threads` 的 SELECT 与 `_summary()` / `_thread_summary_payload()` 必须同步加 `title`。
- 协议唯一源是 `packages/protocol/schema/v3.json`（minor 8）。改完必须 `bun run protocol:generate`。生成器目前只有 operations / events / interactions，没有 notifications 桶。
- IPC `handleMessage`：非 `event` 的 notification 已经 `emit(method, params)`。`thread.summary` 可以挂这条缝，不必塞进 Timeline Event。
- 命令走 Registry → Dispatcher → Controller。`/title` 应是 `thread.title`，`safety.runtime: "allowed"`，`CommandRpcMethod` 增加 `threads.set_title`。
- Thread catalog 目前只在 `thread.open` 和 `finishRun` 刷新。新 thread 在 Run 结束前可能不出现在 Web 侧栏。停点 1 必须在 `run.start` 受理后刷新或合并 catalog，否则「列表立刻能认出」不成立。
- `/btw` 的 `threads.side_question` 是旁路单轮 `ainvoke`、0 工具、不写 Transcript 的现成样子。自动起名按它做，不要进 graph。
- 压缩中白名单仍只放 help/status/quit，不把 `/title` 加进去。

## 架构决策

- **显示名只有一处公式。** `presentation-shared` 提供 `threadDisplayTitle`；TUI picker 与 Web 侧栏都调用它。禁止两端各写一遍 fallback。
- **origin 不出协议。** `title_origin` 只在 SQLite 和 store CAS 里；CLI 只看见 `title: string | null`。
- **`/title` 用 RPC 返回值更新 catalog。** 停点 1 不依赖 `thread.summary`。自动起名成功才需要通知。
- **生成器加 notifications 桶，而不是把 `thread.summary` 伪装成 Event。** 与 Spec 7.3 一致。
- **失败关闭的机械更新。** 所有 `ThreadSummary` 字面量（测试夹具、noop gateway）必须带 `title`，不保留无该键的旧形状。

## 依赖顺序

```text
1. 协议 ThreadSummary.title + threads.set_title + 生成代码
   ↓
2. SQLite v20 + set_user_title + list/open 带 title
   ↓
3. Host threads.set_title
   ↓
4. /title 命令 + catalog 合并 + 显示名 + run.start 后刷新列表
   ↓
5. 停点 1 可演示
   ↓
6. apply_auto_title CAS + 旁路起名 + thread.summary 通知
   ↓
7. 文档
   ↓
8. 停点 2 可演示
```

1–4 必须落在同一停点：只改库用户看不见；只改 UI 没有可写标题。

## 停点 1 — `/title` 改名，现有列表立刻显示

**改什么：** 协议 minor 9，`threadSummary.title`，`threads.set_title`（controlled，`threads.read`）。SQLite v20 增加 `title` / `title_origin`；`set_user_title` 做规范化（20 码点、空白折叠）。Host 暴露 RPC。CLI 登记 `/title`，执行中 allowed，空参用法 notice。`CatalogFeature` 按 `thread_id` 合并摘要；`run.start` 受理后刷新 thread catalog。TUI `/resume` 与 Web 侧栏主标签和搜索改用显示名。

**为什么：** 这是用户能感知的最小闭环：改名、看见、搜索，旧 thread 行为不变。

**怎么验证：** 见 Todo 停点 1 的 focused tests。`bun run typecheck`。

**用户怎么看：** `bun run dev` 发一条消息（或用已有 thread）。`/title 修索引`。打开 `/resume`，主标签是「修索引」。Web 侧栏同一条也是「修索引」，搜「修」能找到。空 `/title` 只提示用法。没改过名的旧 thread 仍显示第一条原文截断。执行中也可以 `/title`。

## 停点 2 — 并行起名、用户覆盖、文档

**改什么：** `apply_auto_title` 仅在 `title_origin IS NULL` 时写入。`accept_run` 且 `created=True`、用户消息后 `message_count==1` 时启动旁路起名（当前 Run 的模型、0 工具、15s、不写 Transcript）。成功发 `thread.summary`；CLI 合并 catalog，时间线不出现这条通知。用户 `/title` 之后迟到的 auto 静默丢弃。更新用户文档和架构文档；执行中立刻可用列表加上 `/title`。

**为什么：** Task 的默认名和「用户优先」在停点 1 还看不到。

**用户怎么看：** `/new` 后发一条明确主题的消息。Web 侧栏先出现截断原文，随后（不等整轮结束）换成短标题。再开一个 thread，第一条发出后立刻 `/title 我起的名`，等主回复结束，标题仍是「我起的名」。把起名模型/网络掐掉或 mock 失败时，列表停在截断原文，任务继续。文档能查到 `/title`。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `harness_threads` 列契约漏改一处 | 迁移/open 直接失败 | Todo 要求 grep 所有列清单和 DDL，v19→v20 单测 |
| 夹具漏加 `title` | protocol check / TS 红一片 | 生成器夹具 + 一次编译扫 noop gateway 和测试字面量 |
| 起名与主 Run 抢同一 provider 客户端 | 主回复变慢或打挂 | 与 `/btw` 一样走 provider pool acquire/release；失败不影响 Run |
| `thread.summary` 误进时间线 | 对话里冒出元数据 | 通知不走 `event` 信封；timeline 测试断言没有该 type |
| catalog 只在 finishRun 刷新 | 停点 1/2 列表不及时 | 停点 1 就在 run 受理后刷新；停点 2 再用通知补标题 |
| 生成器没有 notifications | 手工 Method 会和 generate --check 打架 | 停点 2 先扩展 `x-harness.notifications` 和 generate.ts |

## 非范围（提醒执行 Agent）

不新增 TUI thread 列表、Web 内联改名、旧数据回填、`/rename` 别名、压缩中放行 `/title`、另配起名模型、把通知做成 Timeline Event。
