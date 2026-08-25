---
id: HC-129
title: 完成 Web 工作台键盘与可访问性收敛
feature_area: Web UI 工作台体验升级
parent_task: HC-124
decomposed_by: Codex
priority: P1
status: 待认领
owner: 未认领
branch: -
reviewed_at: 2026-08-24
review_due: 2026-09-07
scope: 对完成结构整改后的 Web 工作台执行跨组件可访问性收敛，补齐 skip link、Tab/焦点顺序、overlay focus trap/restore、键盘可调 separator、窄屏 44px target、双主题对比度、ARIA 状态和 live region 策略。
acceptance: 键盘可从页面入口跳到 Timeline/Composer并完成 Thread/文件/Dock/命令/Interaction 主要流程；Sidebar/Dock/Thread 分隔条支持 Arrow、Shift+Arrow、Home/End 和复位，暴露 aria value；overlay 正确 trap/restore，column 不声明 modal；窄屏主要目标≥44px；双主题必要文字/图标对比度达标；axe 或等价自动检查无高优先级问题，手工键盘矩阵有证据。
user_docs: docs/user/Web界面.md、docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-124-统筹WebUI工作台体验升级与.md
test_evidence: -
references: docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md、docs/developer/task/HC-125-统一Web工作台视觉token.md、docs/developer/task/HC-126-建立Web工作台三档响应式与外.md、docs/developer/task/HC-127-收敛WebTimeline身份.md、docs/developer/task/HC-128-强化WebRun、Compos.md
completed_at: -
---

## 背景

现有 Web 已有部分 ARIA、focus-visible 和菜单键盘行为，但三个 resize separator 仍是 Pointer-only，页面也没有绕过 Sidebar 大量控件的 skip link。本任务在结构稳定后做一次整体闭环，避免各子任务各自修一小块后仍留下断链。

## 当前存在的问题

- separator 只有 role/方向和 Pointer drag，没有 keyboard/value/reset。
- Sidebar 项目多时，键盘用户到 Timeline/Composer 需要经过大量 Tab。
- Overlay/drawer 与 desktop column 的 modal/focus 语义需要按 presentation 区分。
- 浅色 muted 必要小字对比度不足，窄屏 44px target 依赖尚未落地的响应式规则。
- streaming、Run status、notice 和 Interaction 的 live region 可能重复播报。

## 目标设计

```text
语义 DOM 顺序
  → skip link 进入 Timeline/Composer
  → composite widgets 用 Arrow/Home/End
  → overlay 才 trap focus
  → close/resolve 后恢复到稳定 trigger/Composer
```

## 实施步骤

1. 加入“跳到对话”“跳到输入框”链接和稳定 main/composer target。
2. 复核 Topbar、Sidebar、Timeline、Composer、Dock DOM 与 Tab 顺序。
3. 为三个 separator 增加 focus、aria min/max/now 和键盘增量/复位，复用现有 width/ratio intent。
4. 为 compact/narrow overlay 建立 focus trap/restore；desktop column 保持非 modal。
5. 审核 role=log、polite/assertive live region，避免 streaming token 重复朗读。
6. 测量 light/dark 文本、图标、focus、disabled 和 state contrast；补窄屏 target 测试。
7. 增加自动 a11y/DOM 断言与完整手工键盘矩阵证据。

## 范围

- Web presentation 的 DOM/ARIA/focus/keyboard/CSS。
- Adapter 中必要的 focus request/restore 表现状态。
- 可访问性 focused tests 和用户键盘说明。

## 非范围

- 不改变业务快捷键语义、审批策略或 capability。
- 不用正 tabindex 重排自然 DOM 顺序。
- 不为桌面鼠标控件无条件放大到 44px；只保证窄屏和 Pointer hit area。
- 不借可访问性任务重构视觉组件结构。

## 验收清单

- [ ] skip link、Tab、Arrow/Home/End、Escape 和 focus restore 矩阵通过。
- [ ] separator 可用键盘调整并暴露实时 aria value，可恢复默认。
- [ ] overlay modal/column non-modal 语义正确，无焦点陷阱或焦点丢失。
- [ ] light/dark 必要内容对比度和窄屏 target 达标。
- [ ] live region 不逐 token 重复播报，Interaction/错误仍及时可知。
- [ ] 自动检查、focused tests、build、typecheck 通过。

## 定期复核记录

- 2026-08-09（Codex）：从 HC-124 拆解；必须等待 HC-125～HC-128 的 DOM 稳定后执行，下一次复核 2026-08-23。

