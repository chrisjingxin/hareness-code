# 多工具审批机制核查对话

## 用户

帮我看下我当前的多工具审批机制是怎么一个流程，有哪些问题、可以改进的点？我之前在 `@design-multi-tool-approval`、`@refactor-permission-engine` 写了一些设计文档，不知道是不是都实现了。

## 助手

我会先读取设计文档，再追踪实际实现。

两份设计都比较完整。我会继续读取任务、检查清单和生产代码，重点确认策略管线及多工具串行审批是否真正接线。

初步发现：相关文件基本都存在，但任务和检查清单的状态已经滞后，需要以实际生产调用链为准。

## 用户

继续。

## 助手

目前已经确认两处“文档写了、代码也有，但生产链路没真正用上”的典型情况：

- 集中式 `evaluate_permission()` 没有生产调用者。
- 受信目录门和 AUTO 危险规则剥离只接在 Python 辅助函数上，而实际模式切换发生在 TypeScript。

相关测试都通过了：

- Python 审批与策略测试：1048 项通过。
- TUI 串行审批测试：11 项通过。

但测试通过仍存在覆盖盲区。例如将一条 `deny` 规则持久化后，文件里会变成不含 effect 的 `Bash(rm *)`，重新加载时竟然变成 `allow`；现有测试没有覆盖这个往返安全不变量。

## 助手最终结论

主体已实现，但没有完全按两份设计落地，而且存在两个严重安全问题。

完整流程和问题优先级已整理为 Canvas：

[多工具审批核查报告](C:\Users\64809\.cursor\projects\d-code-tst-hareness-code\canvases\multi-tool-approval-review.canvas.tsx)

### 当前真实流程

模型返回多个工具 → AUTO 分类器预判 → HITL 预检 → Host 逐个弹审批 → 收集完全部决定 → 一次性恢复 LangGraph → 工具执行并受读写锁约束。

所以当前“串行”的是审批决定，并不是“批准一个立即执行一个”。安全工具也要等整批审批结束后才能执行。

### 严重问题

#### 1. deny 持久化后会变成 allow

`serialize_rule()` 没有保存 effect。实际复现结果：

```text
PermissionRule(effect="deny")
→ "Bash(rm *)"
→ 重新加载为 PermissionRule(effect="allow")
```

#### 2. AUTO 可被宽泛 allow 绕过

`Bash(*)` 等危险 allow 规则的剥离只有孤立函数，没有生产接线；而预检、分类器和破坏性守卫都会对 allow 提前返回。因此 `Bash(*)` 可能绕过 AUTO 的 F3 破坏性检查。

### 其他主要缺口

- `evaluate_permission()` 只有测试调用；生产逻辑分散在 preflight 和多个 middleware，容易漂移。
- 受信目录门未接线：CLI 仍可直接切换 auto/yolo，Always Allow 始终显示。
- “拒绝并反馈”不可用：CLI 固定发送空反馈，实际退化成普通拒绝。
- `approve_thread` 实际保存在 Host 共享列表，会影响其他 Thread，与“本线程允许”不符。
- 文件 Always Allow 仍生成 `resource="*"`，没有实现目录级授权。
- 危险命令只在 AUTO 硬拦截，没有实现“所有模式包括 yolo 都不可覆盖”。
- `shell_access`、`DenialTracker`、破坏意图豁免、危险规则剥离都有代码和测试，但没有生产调用者。
- Extension Hook 位于没有生产调用者的管线中，不能视为真正预留成功。

### 测试结果

- Python policy + host：`1048 passed`
- TUI 串行审批：`11 passed`

测试全绿，但主要验证的是孤立函数和 decision 数组，没有覆盖规则持久化往返、真实执行时序、反馈输入、受信门和混合工具批次。

### 建议优先顺序

1. 修复 DSL effect 往返。
2. 将破坏性规则置于所有 allow 之前。
3. 合并成唯一决策内核。
4. 明确批量执行语义。
5. 修正 Thread 作用域和审批 UI。
