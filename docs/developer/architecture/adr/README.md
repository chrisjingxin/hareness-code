# ADR（架构决策记录）

ADR 记录影响多个模块、长期维护或难以逆转的技术决策。文件命名为 `NNNN-简短标题.md`，例如 `0001-stdio-json-rpc.md`；编号递增且不复用。

```md
# ADR NNNN：决策标题

日期：YYYY-MM-DD  
状态：提议 / 已接受 / 已废弃

## 背景

## 决策

## 后果

## 备选方案
```

新 ADR 要从 [架构总览](../架构总览.md) 或相关开发文档链接，并在对应任务文件中记录关联。

当前 ADR：

- [ADR 0001：Agent 领域对象、生命周期与兼容映射](0001-agent-domain-model.md)
- [ADR 0002：Project-scoped Agent Host、多个 Connection 与单 Run owner](0002-project-host-multi-connection.md)
- [ADR 0003：Transcript 事实层与上下文投影生命周期](0003-transcript-context-projection-lifecycle.md)
- [ADR 0004：Legacy migration 的 durable supervision 协议](0004-legacy-migration-supervision.md)
