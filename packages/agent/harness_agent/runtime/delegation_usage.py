"""实验性委派用量账本：只供诊断日志排障，不进入协议或界面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


def _token(value: object) -> int | None:
    """把一次调用的用量字段收成非负整数；缺失为 None。"""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@dataclass
class _ExecutionUsage:
    """单个 execution 的累加用量。"""

    execution_id: str
    agent_id: str
    profile_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    complete: bool = True
    calls: int = 0


@dataclass
class DelegationUsageLedger:
    """按 execution 累加模型调用用量，供 Run 终态写诊断日志。"""

    _executions: dict[str, _ExecutionUsage] = field(default_factory=dict)

    def record(
        self,
        *,
        execution_id: str,
        agent_id: str,
        profile_id: str,
        usage: Mapping[str, object],
    ) -> None:
        """把一次可观察模型调用记入所属 execution；缺失字段标不完整。"""
        if not execution_id:
            return
        current = self._executions.get(execution_id)
        if current is None:
            current = _ExecutionUsage(
                execution_id=execution_id,
                agent_id=agent_id or "unknown",
                profile_id=profile_id or "unknown",
            )
            self._executions[execution_id] = current
        input_tokens = _token(usage.get("input_tokens"))
        output_tokens = _token(usage.get("output_tokens"))
        cached_tokens = _token(
            usage.get("cached_input_tokens", usage.get("cached_tokens"))
        )
        if input_tokens is None or output_tokens is None:
            current.complete = False
        if input_tokens is not None:
            current.input_tokens += input_tokens
        if output_tokens is not None:
            current.output_tokens += output_tokens
        if cached_tokens is not None:
            current.cached_tokens += cached_tokens
        current.calls += 1

    def summary(self, root_execution_id: str) -> dict[str, object]:
        """生成诊断字段：root 自身、全部 child、合计。"""
        root = self._executions.get(root_execution_id)
        children = [
            item
            for item in self._executions.values()
            if item.execution_id != root_execution_id
        ]
        root_input = root.input_tokens if root is not None else 0
        root_output = root.output_tokens if root is not None else 0
        root_cached = root.cached_tokens if root is not None else 0
        child_input = sum(item.input_tokens for item in children)
        child_output = sum(item.output_tokens for item in children)
        child_cached = sum(item.cached_tokens for item in children)
        complete = (root.complete if root is not None else True) and all(
            item.complete for item in children
        )
        return {
            "root_input_tokens": root_input,
            "root_output_tokens": root_output,
            "root_cached_input_tokens": root_cached,
            "child_input_tokens": child_input,
            "child_output_tokens": child_output,
            "child_cached_input_tokens": child_cached,
            "total_input_tokens": root_input + child_input,
            "total_output_tokens": root_output + child_output,
            "total_cached_input_tokens": root_cached + child_cached,
            "complete": complete,
            "child_count": len(children),
        }
