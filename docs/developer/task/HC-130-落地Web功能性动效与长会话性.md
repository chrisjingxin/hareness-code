---
id: HC-130
title: 落地 Web 功能性动效与长会话性能证据
feature_area: Web UI 工作台体验升级
parent_task: HC-124
decomposed_by: Codex
priority: P2
status: 待认领
owner: 未认领
branch: -
reviewed_at: 2026-08-24
review_due: 2026-09-07
scope: 在稳定 Web UI 结构上统一 hover/menu/drawer/Tool/new-output/Run 状态动效，完整支持 reduced-motion，并用 500/1000 item 与 streaming 场景测量 render、Markdown/高亮和滚动成本，仅在证据表明需要时实施 windowing 或局部优化。
acceptance: 动效时长、easing 和允许属性符合 HC-124；历史恢复不批量播放、新输出不抢用户滚动、Tool 展开保持 scroll anchor；reduced-motion 取消位移/呼吸/smooth scroll但状态仍清楚；500/1000 item + streaming 有可复现 Profiler/Browser trace 证据和结论；如无瓶颈不引入 windowing，如有则最小优化并覆盖回归；focused tests、Browser 检查、build、typecheck 通过。
user_docs: docs/user/Web界面.md
developer_docs: docs/developer/spec/HC-124-统筹WebUI工作台体验升级与.md
test_evidence: -
references: docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md、docs/developer/task/HC-127-收敛WebTimeline身份.md、docs/developer/task/HC-128-强化WebRun、Compos.md、docs/developer/task/HC-129-完成Web工作台键盘与可访问性.md
completed_at: -
---

## 背景

当前页面有 hover transition、spinner、Tool chevron 和 streaming cursor，但没有统一的功能性 motion 语言。长 Timeline 也只有 memo 依据，没有真实性能测量。动画和性能必须在结构稳定后一起处理，避免用动画放大重排或用猜测引入复杂 windowing。

## 当前存在的问题

- menu/drawer/new message 没有一致时长与 easing。
- active Run 只有 spinner/cursor，状态变化缺少克制的连续感。
- Tool 展开和新输出可能改变 scroll anchor；用户向上阅读时不能被抢回底部。
- 不知道 500/1000 item、streaming Markdown 和 Shiki Worker 是否真正形成瓶颈。

## 目标设计

```text
稳定结构
  → CSS transform/opacity 功能性动效
  → reduced-motion 等价状态
  → Profiler/trace 测量
  → 有证据才优化或 window
```

精确 motion table 和长会话流程以 [HC-124 设计](../spec/HC-124-统筹WebUI工作台体验升级与.md) 为准。

## 实施步骤

1. 建立 motion duration/easing token，统一 hover、menu、overlay、Tool、new message 和 Run status。
2. 用 transform/opacity 实现 overlay/menu；禁止动画 width/left/top 和历史 stagger。
3. 区分历史恢复与本页新增 item，只有新增内容播放 140～180ms 微动效。
4. 加固 scroll follow/anchor：用户离底时保持位置并显示“有新输出”，Tool 展开不改变跟随判断。
5. 完成 reduced-motion 行为和测试。
6. 构造 500/1000 Timeline item、连续 streaming、Markdown code/table 和 Tool 展开场景，记录 Profiler/trace。
7. 按证据定位 render、Markdown/highlight 或 DOM 成本；只有 DOM 规模是主因时才引入 windowing。

## 范围

- Web motion CSS/React 状态、Timeline scroll 策略和性能测试夹具。
- 必要的 memo/key/selector 或 windowing 最小优化。
- 性能与 reduced-motion 文档证据。

## 非范围

- 不引入 GSAP、Framer Motion 或新动画依赖。
- 不增加背景光斑、磁性 cursor、bounce 或装饰性持续动画。
- 不在没有测量证据时重写 Timeline。
- 不改变消息、Tool 或 Run 业务数据。

## 验收清单

- [ ] motion token、时长、easing 和属性符合设计，交互反馈不超过 240ms。
- [ ] 历史无批量入场，新输出/Tool 展开不抢滚动或产生明显跳动。
- [ ] reduced-motion 下状态完整、无位移/呼吸/smooth scroll。
- [ ] 500/1000 item 与 streaming 有可复现测量、瓶颈归因和优化决策记录。
- [ ] 必要优化有测试；无必要时明确记录不引入 windowing 的证据。
- [ ] focused tests、Browser 检查、build、typecheck 通过。

## 定期复核记录

- 2026-08-09（Codex）：从 HC-124 拆解；等待 HC-127～HC-129 完成后执行，下一次复核 2026-08-23。

