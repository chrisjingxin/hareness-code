# HC-171 Thread短标题执行清单

关联：[Task](../task/HC-171-Thread短标题.md) · [Spec](../spec/HC-171-Thread短标题.md) · [Plan](../plan/HC-171-Thread短标题.md)

每项必须先写最小失败测试，再写实现。一次只做到下一个可演示停点；完成本段后勾选、记录实际命令和结果、覆盖更新 `tmp/handoff.md`，然后停下等用户看过。未经用户要求不得继续下一停点。

---

## 停点 1：`/title` 改名，现有列表立刻显示

用户怎么看：`bun run dev` 在当前 thread 执行 `/title 修索引`。`/resume` 主标签变成「修索引」；Web 侧栏同一条也是「修索引」，搜「修」能找到。空 `/title` 只提示用法。没改过名的 thread 仍显示第一条原文截断。执行中也可以 `/title`。

### 协议

- [x] `v3.json`：minor 8→9；`threadSummary` 必有 `title`（`string | null`）；新增 `threads.set_title`；错误码 `TITLE_EMPTY`。`cd packages/protocol && bun run generate` 与 `bun run check` 通过。

### 存储

- [x] SQLite `user_version` 19→20，增加 `title` / `title_origin`。v19 升 20 后旧行为 null。
- [x] `set_user_title` 规范化、空标题 `TITLE_EMPTY`、缺失 `THREAD_NOT_FOUND`、不改 `updated_at_ms`；用户写入覆盖 auto。`tests/threads/test_thread_title.py` 5 passed。

### Host RPC

- [x] `_handle_threads_set_title`。`tests/host/test_thread_title_rpc.py` + `test_thread_rpc.py` 通过。

### 命令、catalog、显示

- [x] `/title` Registry + Dispatcher + catalog upsert。`commands.test.ts`：解析、空参用法、running available、`/rename` 不是命令。`thread-catalog-lifecycle.test.ts`：成功后 catalog 标题与 notice。
- [x] `threadDisplayTitle` / `threadMatchesQuery`；TUI picker 与 Web 侧栏共用。`run.start` 受理后刷新 catalog。

### 停点 1 验证

- [x] focused tests + `bun run typecheck`。证据：
  - `cd packages/protocol && bun run check`
  - `cd packages/agent && .venv/bin/python -m pytest -q tests/threads/test_thread_title.py tests/host/test_thread_title_rpc.py tests/threads/test_thread_persistence.py` → 176 passed（含 persistence 全量）
  - `cd packages/cli && bun test tests/presentation-shared/thread-title.test.ts tests/interactive/commands.test.ts tests/interactive/thread-catalog-lifecycle.test.ts tests/web/presentation/workspace-sidebar.test.tsx tests/tui/presentation/views.test.ts tests/ipc/protocol-contract.test.ts` → pass
  - `bun run typecheck` → `@za38/cli` 通过
- [x] 按「用户怎么看」在 TUI 和 Web 走一遍。用户确认后进入停点 2。

---

## 停点 2：并行起名、用户覆盖、文档

用户怎么看：`/new` 后发一条主题清楚的消息。Web 侧栏先出现截断原文，随后（不等整轮结束）换成短标题。另一个 thread 在第一条发出后立刻 `/title 我起的名`，主回复结束后标题仍是用户起的。起名失败时停在截断原文，任务不被打断。文档能查到 `/title`。

### 自动起名与 CAS

- [x] `apply_auto_title`：仅 `title_origin IS NULL` 时写入 origin=`auto`；已被 user/auto 占用返回 None。完成信号：`test_apply_auto_title_does_not_overwrite_existing_auto` + 既有 user CAS。
- [x] Host：`message_count==1` 且 title 为空时启动旁路起名（当前 Run 模型、0 工具、15s、不挡 `run.start`）。echo / 无模型 / 超时 / 无效输出：不写库、不通知用户、不写 Transcript。成功则 `apply_auto_title` 并发 `thread.summary`。`tests/host/test_thread_title_autogen.py` 4 passed。

### 通知

- [x] `v3.json` 增加 `x-harness.notifications.thread.summary`（params=`threadSummary`）。生成器产出 `NOTIFICATION_METHODS` / `Method.THREAD_SUMMARY`，不进 `EVENT_TYPES`。CLI 监听后 `CatalogFeature.upsertThread`；Timeline 不渲染。`protocol:generate --check` 通过；`thread.summary 通知合并 catalog 且不进入时间线` 通过。

### 用户覆盖

- [x] 集成：先启动会慢返回的 auto，再 `threads.set_title`；auto 完成后 title 仍是用户值。`test_late_auto_title_does_not_override_user_title`。

### 文档

- [x] `docs/user/交互使用.md`：Slash 增加 `/title`；恢复 Thread 一节写主标签是标题或截断原文；执行中「立刻可用」列入 `/title`。
- [x] `docs/user/Web界面.md`：侧栏 Thread 项显示短标题，可搜索。
- [x] `docs/developer/architecture/斜杠命令体系.md` 登记 `thread.title`，执行中 allowed。
- [x] `docs/developer/architecture/架构总览.md` 的 `harness_threads` 索引字段补上 title。

### 停点 2 验证

- [x] 本段 focused tests + `bun run typecheck`。见 `tmp/handoff.md`。
- [x] `cd packages/protocol && bun run check` 通过。`bun run project:check` 见 handoff（若被无关过期 Task 挡住不改无关 Task）。
- [ ] 按「用户怎么看」演示，写证据，更新 `tmp/handoff.md`。**停在这里等用户看。**
