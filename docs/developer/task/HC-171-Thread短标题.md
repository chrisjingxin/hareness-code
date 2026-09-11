---
id: HC-171
title: Thread短标题
feature_area: Thread 会话展示
parent_task: -
decomposed_by: Grok
priority: P1
status: 待认领
owner: 未认领
branch: -
scope: 为每个 thread 增加可持久化的短标题；第一条用户消息发出后并行调一次模型起名，失败则展示截断后的第一条原文；用户用 /title 改当前 thread 标题，改过后迟到的模型结果不得覆盖；标题出现在 /resume 弹窗、Web 侧栏 Thread 列表与 TUI 项目检查器当前会话名。不新增 TUI thread 列表，不做旧数据回填，不随对话自动改标题。
acceptance: 新 thread 发出第一条用户消息后，/resume、Web 侧栏与 TUI 项目检查器出现短标题或截断原文；模型起名失败仍能辨认该 thread；/title 短标题 只改当前 thread 且立刻反映到上述入口；空 /title 只提示用法；用户改过的标题不会被自动起名覆盖；旧 thread 无标题时仍显示截断后的第一条消息；内部 thread_id 仍不展示。
user_docs: docs/user/交互使用.md、docs/user/Web界面.md
developer_docs: docs/developer/spec/HC-171-Thread短标题.md、docs/developer/plan/HC-171-Thread短标题.md、docs/developer/todo/HC-171-Thread短标题.md、docs/developer/architecture/架构总览.md、docs/developer/architecture/斜杠命令体系.md
test_evidence: -
references: -
completed_at: -
---

# HC-171 Thread短标题

需求已用 `grilling` 与用户确认（2026-09-09）。用户结果与验收以本文为准；后续 Spec 只能细化实现，不能扩大为 TUI thread 列表、内联改名或自动持续改标题。

## 通俗说明

现在找历史会话时，`/resume` 弹窗和 Web 左侧 Thread 列表都拿**第一条用户原文**当标题。原文往往很长，扫起来费劲，也不能改成自己认得的短名字。

本任务给每个 thread 一个短标题：

```text
用户发出该 thread 的第一条消息
  → 立刻并行调一次模型，根据这条输入起一个十几到二十个字的短标题
  → 成功：列表和 /resume 显示这个标题
  → 失败：显示截断后的第一条原文
用户随时可以 /title 修索引
  → 只改当前 thread
  → 改过之后，迟到的自动起名结果不能盖掉
```

TUI 侧边栏仍然是项目检查器：显示当前会话短标题，不在这里做一份 ChatGPT 式的 thread 列表。

## 当前问题

1. Thread 摘要只有 `first_message` / `latest_message`，没有可编辑的短标题。
2. Web 侧栏标题直接用第一条原文；`/resume` 选择器同样靠消息摘要辨认会话，长 prompt 挤在一行里不好找。
3. 没有斜杠命令能改这个显示名；`/rename` 又容易和 `/resume` 搞混。

## 已确认产品决策

1. **展示面**：`/resume`（含 `harness --resume`）弹窗、Web 侧栏 Thread 列表，以及 TUI 项目检查器里的当前会话名。TUI 不新增 thread 列表。
2. **默认名**：第一条用户消息发出后立刻并行调一次模型起名；失败则回退为截断后的第一条用户原文。
3. **只起一次**：每个 thread 只在第一条用户消息时自动起名；之后不随对话更新。
4. **命令**：canonical 名称为 `/title`，不用 `/rename`，也不为它增加 `rename` 别名。
5. **改名用法**：`/title 短标题` 改当前 thread；空的 `/title` 只提示用法，不另开编辑器。
6. **用户优先**：用户改过的标题，迟到的自动起名结果不得覆盖。因此 `/title` 改的是元数据、不新开 Run，自动起名进行中也必须能用。
7. **长度**：标题短，大约十几到二十个字；列表展示按此截断，不把整段 prompt 铺开。
8. **旧 thread**：不做批量回填。没有标题时，展示截断后的 `first_message`（再没有则沿用现有「无标题」占位）。
9. **自动起名对用户不可见**：这次模型调用不写入对话时间线或 Transcript，失败时用户只看到截断原文，不弹错误打断当前任务。

## 用户最终得到什么

能在 `/resume`、Web 侧栏和 TUI 项目检查器用短标题认出会话；新对话会自动起名，也可以 `/title` 改成自己要的名字。

## 范围

1. Thread 摘要增加可持久化短标题；列表、打开、恢复都带上这个字段。
2. 第一条用户消息受理后并行起名一次；成功写入标题，失败保持无标题并由界面回退到截断原文。
3. 内置 `/title`：带参数改当前 thread，空参提示用法；执行中可用；用户写入优先于未完成的自动起名。
4. `/resume` 选择器主标签显示标题（或回退文案）；仍不展示内部 `thread_id`。
5. Web 侧栏 Thread 项主标题同样显示；本地搜索能按标题过滤。
6. TUI 项目检查器标题区显示当前会话短标题（或截断原文），仍不是 thread 列表。
7. 更新 `docs/user/交互使用.md`（Slash Command、恢复 Thread、项目检查器）和 `docs/user/Web界面.md`（侧栏 Thread 列表）。

## 非范围

- TUI 项目检查器改成或增加 thread 列表（当前会话短标题除外）。
- Web 侧栏点击进入编辑、右键改名或其他内联改名入口。
- 旧 thread 批量用模型回填标题。
- 随后续对话自动改标题。
- 空 `/title` 弹出编辑器或对话框。
- 给 `/title` 增加 `/rename` 别名。
- 在 TUI 顶栏/状态栏另做当前 thread 标题展示（本任务只保证现有列表入口）。

## 可观察验收

1. 新建 thread，发送第一条用户消息后，不等整轮对话结束，Web 侧栏和随后打开的 `/resume` 能看到短标题，或至少看到截断后的第一条原文。
2. 模拟/强制自动起名失败时，该 thread 仍出现在列表中，标题为截断原文，当前任务不被打断。
3. 在自动起名尚未返回时执行 `/title 修索引`，两侧入口显示「修索引」；之后自动起名成功也不能改回去。
4. 空的 `/title` 只出现用法提示，标题不变。
5. `/title` 不带参数以外的其它 thread，只改当前这条。
6. 没有标题的旧 thread 在 `/resume` 和 Web 侧栏仍按截断 `first_message` 显示；内部 `thread_id` 仍不出现。
7. Web 侧栏搜索能匹配到用户设置或自动生成的标题。
8. 用户文档已说明 `/title`、自动起名与 `/resume`/Web 侧栏展示关系。
