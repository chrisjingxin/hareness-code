# HC-171 Thread短标题规格

关联任务：[HC-171](../task/HC-171-Thread短标题.md)  
命令架构：[斜杠命令体系](../architecture/斜杠命令体系.md)  
全局架构：[架构总览](../architecture/架构总览.md)

用户结果与验收以 Task 为准。本文只规定行为、公开 interface、状态、错误语义和 invariant；不规定实施步骤。后续 Plan/Todo 必须与本文同名，且不能扩大为 TUI thread 列表、内联改名或自动持续改标题。

## 1. 通俗目标

每个 thread 有一个短标题，用来在 `/resume` 弹窗、Web 侧栏和 TUI 项目检查器里认出会话。新 thread 在用户发出第一条消息后马上并行起名；失败就继续显示截断后的第一条原文。用户用 `/title` 改当前 thread，改过的名字不能被迟到的自动结果盖掉。

```text
用户发出该 thread 的第一条消息
  → accept_run 写入 first_message（列表立刻能靠原文认出）
  → Host 并行调一次模型起短标题（不写 Transcript，不挡 Run）
        成功 → 写入标题，列表换成短标题
        失败 / 超时 / 无模型 → 保持无标题，界面继续显示截断原文
用户 /title 修索引
  → 只改当前 thread，立刻反映到 /resume 与 Web 侧栏
  → 之后自动起名成功也不得覆盖
```

TUI 项目检查器显示当前会话短标题，不做 thread 列表。内部 `thread_id` 仍不展示。

## 2. 已确认假设

来自已确认 Task，Spec 不得改写：

1. 展示面是 `/resume`（含 `harness --resume`）、Web 侧栏 Thread 列表，以及 TUI 项目检查器里的当前会话名。TUI 仍不新增 thread 列表。
2. 第一条用户消息发出后立刻并行调一次模型；失败回退为截断后的第一条原文。
3. 每个 thread 只在第一条用户消息时自动起名；不随后续对话更新。
4. 命令 canonical 名称为 `/title`，不提供 `/rename` 别名。
5. `/title 短标题` 改当前 thread；空参只提示用法，不另开编辑器。
6. 用户写过的标题优先于未完成的自动起名；`/title` 改元数据、不新开 Run，主 Run 进行中可用。
7. 标题大约十几到二十个字。
8. 旧 thread 不回填；无标题时显示截断 `first_message`。
9. 自动起名对用户不可见：不进时间线/Transcript，失败不弹错、不打断当前任务。

本 Spec 补齐的实现口径（与 Task 不冲突；若冲突以 Task 为准）：

10. 标题上限 **20 个 Unicode 码点**；空白折叠为单空格，去掉首尾空白和换行。
11. 无标题时界面用现有 `first_message`（存储层已是 160 字预览），再由选择器/侧栏按列宽截断；不把截断原文写进标题字段。
12. 自动起名使用**这一次 Run 已绑定/请求的模型 Profile**，0 工具，不走 Agent graph。
13. 改标题不更新 `updated_at_ms`（排序仍按会话活动，不按改名）。
14. 压缩中命令白名单不加入 `/title`（仍只放 `/help`、`/status`、`/quit`）。

## 3. Capability Map 与 module seam

Module ID 是职责标签，不要求同名目录，也不拆成多份 Spec。

| Module ID | 职责 | 外部 interface | 依赖 |
|---|---|---|---|
| `title-store` | SQLite 标题真源、规范化、用户/自动写入的 CAS | `set_user_title` / `apply_auto_title` / `ThreadSummary.title` | 无 |
| `title-autogen` | 第一条用户消息后并行起名一次；失败静默 | 不阻塞 `run.start`；成功才调用 store | `title-store` |
| `title-protocol` | `ThreadSummary.title`、`threads.set_title`、`thread.summary` 通知 | JSON-RPC | `title-store`、`title-autogen` |
| `title-command` | `/title` Registry + Dispatcher + Controller | semantic operation → RPC | `title-protocol` |
| `title-presentation` | 列表主标签与搜索用同一套显示名 | `threadDisplayTitle` | `title-protocol` |

依赖只能沿下列方向：

```text
title-store
    ↓
title-autogen
    ↓
title-protocol
    ├── title-command
    └── title-presentation
```

深度要求：

- CLI 不知道 `title_origin`、SQLite 列名或起名 prompt。
- 自动起名不知道 TUI/Web。
- TUI picker 与 Web 侧栏不得各自拼接「有 title 用 title、否则 first_message」。
- 表现层不得自己调模型起名。

## 4. 术语

```text
标题 / title
└─ 用户或自动写入的短名。存储与协议里缺省为 null，表示「还没有标题」。

显示名
└─ 列表实际画出的字符串：有 title 用 title，否则用 first_message，再否则「（无标题）」。

title_origin
└─ 仅存储层：null / auto / user。不进协议，不进 UI。

自动起名
└─ Host 在第一条用户消息受理后发起的一次旁路模型调用。不是 Run，不是 /btw，不写 Transcript。

用户标题
└─ 经 /title → threads.set_title 写入、origin=user 的标题。CAS 上自动写入必须失败。
```

## 5. 数据与持久化

SQLite `PRAGMA user_version` 从 **19 升到 20**。`harness_threads` 增加：

| 列 | 类型 | 含义 |
|---|---|---|
| `title` | `TEXT NULL` | 规范化后的标题；无标题为 NULL |
| `title_origin` | `TEXT NULL` | `auto` / `user`；与 `title` 同有同无 |

Invariant：`(title IS NULL) = (title_origin IS NULL)`。旧行迁移后两列都是 NULL，不做回填。

`ThreadSummary` 增加 `title: string | null`（协议必有该键，值可 null）。`first_message` / `latest_message` / `message_count` 含义不变。`title_origin` 不出现在 `threads.list` / `threads.open` / 任何 Event payload。

`list_threads` / `open_thread` / 新建索引行都必须带上 `title`。新建 thread 的 INSERT 不写标题（保持 NULL）。刷新 `first_message` / `latest_message` 的 UPDATE **不得**改 `title`。

store 的外部 interface：

```text
set_user_title(thread_id, raw) → ThreadSummary
  规范化后写入 title、title_origin='user'
  空标题 → TITLE_EMPTY
  不存在 → THREAD_NOT_FOUND

apply_auto_title(thread_id, raw) → ThreadSummary | None
  仅当当前 title_origin IS NULL 时写入 title、title_origin='auto'
  已被 user（或先前 auto）占用 → 返回 None，不报错
```

规范化（用户与自动共用）：

1. Unicode NFC；
2. 把换行/制表等空白折叠成单个空格，去掉首尾空白；
3. 去掉包裹整段的成对引号 `"` `'` `“”`；
4. 空串 → 视为无效；
5. 超过 20 个 Unicode 码点则截到 20（自动起名静默截；`/title` 截完后 notice 告知实存内容）。

改标题**不**修改 `updated_at_ms`。

## 6. 自动起名

### 6.1 何时启动

`accept_run` 成功且 **`created=True`**，并且这一次写入了用户消息、受理后 `message_count == 1`，且 `title_origin IS NULL`。

不启动的情况：幂等重试（`created=False`）、未记录用户消息、已有标题、echo 模式、模型配置不可用。这些情况保持无标题，界面走显示名回退。

每个 thread 只会被这条规则打中一次（只有第一条用户消息让 `message_count` 变成 1）。失败不重试。

### 6.2 与 Run 的关系

自动起名**不得**拖延 `run.start` 的返回。它是 Host 上的旁路任务，独立于 graph、Tool、审批和 Transcript。

- 0 工具，单轮 `ainvoke`。
- 模型 Profile = 这次 Run 的请求/绑定 Profile（与主回复相同来源；不要另配一个「起名模型」）。
- 输入只有这一条用户消息正文，外加固定的短系统说明：「用对方语言起一个不超过 20 字的会话标题，只输出标题本身」。
- 超时 **15s**（含 provider retry 预算）视为失败。
- 主 Run 取消、失败或结束，不取消已启动的起名；CAS 会消化与 `/title` 的竞争。
- 不得把起名 prompt/回复写入 Transcript、Timeline、LangGraph checkpoint。
- 诊断日志只记成功/失败/超时与标题码点数，不记用户原文和模型全文。

### 6.3 结果

```text
模型返回
  → 规范化
        无效 / 超时 / 抛错 / 无模型 → 不写库，不通知用户
        有效 → apply_auto_title
                    写入成功 → 发 thread.summary
                    CAS 未写入 → 静默（用户已经 /title）
```

## 7. Protocol

协议 minor **8 → 9**。`packages/protocol/schema/v3.json` 为唯一契约源，生成 TS 类型与校验器。

### 7.1 `threadSummary`

必有字段增加 `title`，类型 `string | null`。`additionalProperties: false` 保持。所有 fixtures / 测试夹具同步。不保留无 `title` 键的旧形状。

### 7.2 `threads.set_title`

| | |
|---|---|
| params | `{ thread_id: string, title: string }` |
| result | `{ thread: ThreadSummary }` |
| capability | `threads.read` |
| controlled | true（仅 owner Connection） |
| min_minor | 9 |

语义：把该 thread 的标题设为用户标题（`set_user_title`）。只允许当前 project 的 thread。返回写入后的摘要，供客户端立刻补 catalog，不必再 `threads.list`。

稳定错误（JSON-RPC message 为下列码；`data.code` 相同）：

| 码 | 何时 |
|---|---|
| `TITLE_EMPTY` | 规范化后为空 |
| `THREAD_NOT_FOUND` | 当前 project 没有该 thread |
| `CAPABILITY_REQUIRED` | 未协商 `threads.read` |
| `CONTROL_NOT_HOLDER` | 非 owner |

不引入 `threads.write` capability。

### 7.3 `thread.summary` 通知

JSON-RPC **notification**，method 为 `thread.summary`，params 为 `ThreadSummary`。

这是 Host 元数据，**不是** `event` 信封里的 Timeline Event：

- 无 `run_id` / `sequence`，不插入时间线；
- 发给该 Host 上已协商 `threads.read` 的 Connection（至少 owner；若该 thread 正被 `threads.watch`，观察者也收）；
- 仅在标题**实际写入**后发送（用户 `set_title` 成功、或 `apply_auto_title` 成功）。CAS 未写入不发。

CLI `CatalogFeature` 按 `thread_id` 合并进 `catalogs.threads.items`；没有该项则插入后再按现有 recency 排序。TUI/Web 只读 catalog，不自己打 RPC 补标题。

`/title` 的 RPC 返回值与这条通知重复时，以最新 `title` 为准，允许幂等覆盖。

## 8. `/title` 命令

```typescript
{
  id: "thread.title",
  name: "title",
  description: "为当前 thread 设置短标题",
  source: { type: "builtin" },
  presentation: "action",
  argumentHint: "<短标题>",
  suggested: true,
  requirements: {
    capabilities: [Capability.THREADS_READ],
    requiresThread: true,
  },
  safety: { runtime: "allowed" },
}
```

禁止 aliases，尤其禁止 `rename`。

Dispatcher：

```text
无 currentThreadId → Registry 已 disabled：「当前没有可用 thread」
空参 / 只有空白 → notice「用法：/title <短标题>」
否则 → threads.set_title({ thread_id: 当前, title: 参数原文 })
        成功 → 用 result.thread 更新 catalog；notice「已将标题设为「{显示名}」」
        TITLE_EMPTY → notice 标题无效
        其它错误 → notice 失败原因，不改 catalog
```

执行中：`safety.runtime === "allowed"`，菜单可点、手输可跑，草稿在成功后可清（与其它 allowed 命令一致）。压缩中仍禁用。

不打开 picker/dialog。不接受内部 `thread_id` 当参数（参数就是标题文本）。

## 9. 展示与搜索

共享函数放在 `packages/cli/src/presentation-shared/`（TUI picker、Web 侧栏与 TUI 检查器共用）：

```typescript
export function threadDisplayTitle(thread: {
  title: string | null
  first_message: string
  latest_message: string
}): string {
  const named = thread.title?.trim()
  if (named) return named
  const fallback = thread.first_message.trim() || thread.latest_message.trim()
  return fallback || "（无标题）"
}
```

| 入口 | 主标签 | 搜索字段 |
|---|---|---|
| TUI `ThreadPicker` | `threadDisplayTitle` | title、first_message、latest_message |
| Web `ThreadSection` | 同上 | 同上 |
| `harness --resume` | 与 `/resume` 同一 picker | 同上 |
| TUI 项目检查器 | 当前会话 `currentThreadDisplayTitle` | 不搜索 |

次行元数据（更新时间、消息数、Web 的进行中文案）不变。`thread_id` 仍不渲染。

Web 侧栏在自动起名或 `/title` 写入后，不需要手动点「重试」；走 catalog 合并即可。Run 进行中当前项仍可更新标题；切到其它 thread 的既有 busy 规则不变。

## 10. 错误与空态

| 场景 | 用户看到 |
|---|---|
| 新 thread、自动起名尚未返回 | 显示名 = 截断 `first_message` |
| 自动起名失败 | 同上，无错误条 |
| 空 `/title` | 用法 notice，标题不变 |
| 无当前 thread | 菜单 disabled；手输 notice「当前没有可用 thread」 |
| 旧 thread 无 title | 显示名 = 截断 `first_message`（或「（无标题）」） |
| 标题超过 20 码点 | 存前 20；notice 带实存标题 |

自动起名失败**禁止**用 Timeline notice / toast 报错。

## 11. Invariants

1. 自动起名与 `/title` 都不把内容写入 Transcript 或模型对话历史。
2. `title_origin='user'` 之后，任何 `apply_auto_title` 都不得改 `title`。
3. 同一 thread 自动起名任务最多一次（由「第一条用户消息」触发条件保证）。
4. 列表主标签只来自 `threadDisplayTitle`，两端公式相同。
5. 协议与存储都不把原始 project 路径放进标题摘要。
6. 无标题不是错误状态；`threads.list` 必须仍返回该 thread。

## 12. 测试策略

TDD。禁止真实模型凭据。自动起名用 echo / mock `ainvoke`。

| 层 | 焦点 | 命令 |
|---|---|---|
| store | 迁移 v19→20、list 带 title、user CAS 压过 auto、失败不写 title | `cd packages/agent && .venv/bin/python -m pytest -q tests/threads/test_thread_persistence.py` 及本任务新增用例 |
| host | `threads.set_title`、空标题、非 owner、自动起名成功/失败/超时、echo 跳过、不写 Transcript | `tests/host/test_thread_rpc.py` 及新增 autogen 测试 |
| protocol | `ThreadSummary.title`、`threads.set_title`、`thread.summary` | `packages/protocol` generate `--check` 与契约夹具 |
| command | 解析 `/title`、空参、running allowed、无 rename 别名 | `packages/cli && bun test tests/interactive/commands.test.ts` 等 |
| presentation | `threadDisplayTitle`、picker/Web 主标签与搜索 | `tests/presentation-shared/`、`tests/tui/presentation/`、`tests/web/presentation/` |

验证命令：

```text
cd packages/protocol && bun run check
cd packages/cli && bun test <focused>
cd packages/agent && .venv/bin/python -m pytest -q <focused>
bun run typecheck
```

## 13. 边界

- Always：改协议先改 `v3.json` 再生成代码；行为测试先红后绿；用户可感知文案更新 `docs/user/交互使用.md` 与 `docs/user/Web界面.md`。
- Ask first：给 `/title` 加别名、把压缩中白名单放进 `/title`、改 20 码点上限、另配起名模型。
- Never：TUI 侧栏做 thread 列表；Web 列表内联编辑；旧数据批量回填；把起名写入 Transcript；用 `/rename`；展示 `thread_id`。TUI 检查器只显示当前会话名。

## 14. 非范围

与 Task 非范围一致，并明确：

- TUI 顶栏/状态栏另做当前 thread 标题。
- 空 `/title` 弹编辑器。
- 随对话定期重起名。
- 新的 `threads.write` capability。
- 把 `thread.summary` 做成 Timeline Event。

## 15. 可观察验收

与 Task 验收一一对应，不新增产品范围：

1. 新建 thread，发第一条用户消息后，不等整轮结束，Web 侧栏和随后打开的 `/resume` 能看到短标题，或至少看到截断原文。
2. 自动起名失败时 thread 仍在列表中，显示截断原文，当前任务不被打断。
3. 自动起名尚未返回时 `/title 修索引`，两侧显示「修索引」；之后自动成功也不能改回去。
4. 空 `/title` 只有用法提示，标题不变。
5. `/title` 只改当前 thread。
6. 无标题的旧 thread 仍按截断 `first_message` 显示；`thread_id` 不出现。
7. Web 侧栏搜索能匹配标题。
8. 用户文档说明 `/title`、自动起名与 `/resume`/Web 侧栏的关系。

实现完成后更新 `斜杠命令体系.md`（登记 `thread.title`，并把它列入执行中 allowed）和 `架构总览.md` 中 `harness_threads` 索引字段。
