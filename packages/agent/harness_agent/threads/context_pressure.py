"""上下文压力的纯测量与分级决策。

本模块只处理整数预算、可回收工具压力和明确的 Run 调用类型。它不读取
SQLite、不修改 LangChain 消息，也不调用模型；执行层可以在微压缩后用新的
整数预算重新调用同一个策略。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


PressureAction = Literal["none", "report", "micro", "full", "overflow"]
PressureReason = Literal[
    "none", "occupancy", "tool_pressure", "idle", "manual", "overflow"
]
ModelCallType = Literal[
    "top_level_initial",
    "tool_continuation",
    "model_retry",
    "interaction_resume",
    "subagent",
    "unclassified",
]


@dataclass(frozen=True, slots=True)
class ContextPressurePolicyConfig:
    """一次模型预算使用的全部压力阈值和保留参数。

    总体水位延续现有 50/60/80/90 基线。工具压力和空闲默认值来自
    ``test_context_pressure`` 的工具密集/文本密集 fixture：两个约 8K token
    的旧工具结果即可提前触发，空闲使用 15 分钟而不是参考实现的固定值，
    并始终保留最近一个可恢复工具结果。
    """

    report_ratio: float = 0.50
    micro_ratio: float = 0.60
    full_ratio: float = 0.80
    hard_ratio: float = 0.90
    tool_pressure_tokens: int = 8_192
    tool_pressure_count: int = 2
    idle_threshold_ms: int = 15 * 60 * 1000
    keep_recent: int = 1
    idle_enabled: bool = True

    def __post_init__(self) -> None:
        """拒绝无序水位和无法产生确定行为的负配置。"""
        ratios = (
            self.report_ratio,
            self.micro_ratio,
            self.full_ratio,
            self.hard_ratio,
        )
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in ratios):
            raise ValueError("CONTEXT_PRESSURE_RATIO_INVALID")
        if not (
            self.report_ratio
            < self.micro_ratio
            < self.full_ratio
            < self.hard_ratio
        ):
            raise ValueError("CONTEXT_PRESSURE_THRESHOLDS_UNORDERED")
        if self.tool_pressure_tokens < 1 or self.tool_pressure_count < 1:
            raise ValueError("CONTEXT_PRESSURE_TOOL_THRESHOLD_INVALID")
        if self.idle_threshold_ms < 1 or self.keep_recent < 1:
            raise ValueError("CONTEXT_PRESSURE_RETENTION_INVALID")


@dataclass(frozen=True, slots=True)
class ContextPressureSnapshot:
    """一次模型调用前的有限压力测量。"""

    projected_input_tokens: int
    input_cap_tokens: int
    occupancy_ratio: float
    reclaimable_tool_tokens: int = 0
    reclaimable_tool_count: int = 0
    idle_duration_ms: int | None = None

    def __post_init__(self) -> None:
        """保证快照可以安全写入检查点诊断 JSON。"""
        if self.projected_input_tokens < 0 or self.input_cap_tokens < 1:
            raise ValueError("CONTEXT_PRESSURE_BUDGET_INVALID")
        if not math.isfinite(self.occupancy_ratio) or self.occupancy_ratio < 0:
            raise ValueError("CONTEXT_PRESSURE_OCCUPANCY_INVALID")
        if self.reclaimable_tool_tokens < 0 or self.reclaimable_tool_count < 0:
            raise ValueError("CONTEXT_PRESSURE_TOOL_MEASURE_INVALID")

    def record(self) -> dict[str, object]:
        """返回不含消息正文的稳定诊断字段。"""
        return {
            "projected_input_tokens": self.projected_input_tokens,
            "input_cap_tokens": self.input_cap_tokens,
            "occupancy_ratio": self.occupancy_ratio,
            "reclaimable_tool_tokens": self.reclaimable_tool_tokens,
            "reclaimable_tool_count": self.reclaimable_tool_count,
            "idle_duration_ms": self.idle_duration_ms,
        }


@dataclass(frozen=True, slots=True)
class ContextPressureDecision:
    """策略建议；执行层决定是否实际创建 Artifact 或检查点。"""

    action: PressureAction
    reason: PressureReason
    keep_recent: int
    snapshot: ContextPressureSnapshot | None = None


class ContextPressurePolicy:
    """无副作用地测量上下文压力并选择下一步动作。"""

    def __init__(
        self, config: ContextPressurePolicyConfig | None = None
    ) -> None:
        """绑定一次不可变配置，避免阈值散落在中间件分支中。"""
        self.config = config or ContextPressurePolicyConfig()

    def measure(
        self,
        projected_input_tokens: int,
        input_cap_tokens: int,
        *,
        reclaimable_tool_tokens: int = 0,
        reclaimable_tool_count: int = 0,
        idle_duration_ms: int | None = None,
    ) -> ContextPressureSnapshot:
        """从当前投影的预算和可回收量生成确定快照。"""
        if projected_input_tokens < 0 or input_cap_tokens < 1:
            raise ValueError("CONTEXT_PRESSURE_BUDGET_INVALID")
        return ContextPressureSnapshot(
            projected_input_tokens=projected_input_tokens,
            input_cap_tokens=input_cap_tokens,
            occupancy_ratio=projected_input_tokens / input_cap_tokens,
            reclaimable_tool_tokens=reclaimable_tool_tokens,
            reclaimable_tool_count=reclaimable_tool_count,
            idle_duration_ms=idle_duration_ms,
        )

    def decide(
        self,
        snapshot: ContextPressureSnapshot,
        *,
        call_type: ModelCallType = "unclassified",
        manual: bool = False,
        overflow: bool = False,
    ) -> ContextPressureDecision:
        """根据单次快照选择动作，不读取时间或其他外部状态。"""
        config = self.config
        if overflow:
            return ContextPressureDecision(
                "overflow", "overflow", config.keep_recent, snapshot
            )
        if manual:
            return ContextPressureDecision(
                "full", "manual", config.keep_recent, snapshot
            )

        if snapshot.occupancy_ratio >= config.full_ratio:
            return ContextPressureDecision(
                "full",
                "occupancy",
                config.keep_recent,
                snapshot,
            )

        # Keep the historic 50% report band intact.  Once the normal micro band
        # is reached, occupancy wins; idle remains an explicit auxiliary trigger
        # in the lower bands.
        if snapshot.occupancy_ratio >= config.micro_ratio:
            return ContextPressureDecision(
                "micro", "occupancy", config.keep_recent, snapshot
            )

        if (
            config.idle_enabled
            and call_type == "top_level_initial"
            and isinstance(snapshot.idle_duration_ms, int)
            and not isinstance(snapshot.idle_duration_ms, bool)
            and snapshot.idle_duration_ms >= config.idle_threshold_ms
            and snapshot.reclaimable_tool_tokens > 0
            and snapshot.reclaimable_tool_count > 0
        ):
            return ContextPressureDecision(
                "micro", "idle", config.keep_recent, snapshot
            )

        if snapshot.occupancy_ratio >= config.report_ratio:
            return ContextPressureDecision(
                "report", "occupancy", config.keep_recent, snapshot
            )

        has_tool_pressure = (
            snapshot.reclaimable_tool_tokens >= config.tool_pressure_tokens
            or snapshot.reclaimable_tool_count >= config.tool_pressure_count
        )
        if has_tool_pressure:
            return ContextPressureDecision(
                "micro", "tool_pressure", config.keep_recent, snapshot
            )
        return ContextPressureDecision("none", "none", config.keep_recent, snapshot)


@dataclass(slots=True)
class ModelCallLifecycle:
    """Run 显式传入并推进的模型调用阶段，不从消息时间戳猜测。"""

    next_call_type: ModelCallType = "top_level_initial"
    idle_duration_ms: int | None = None
    model_round: int = 0
    _first_model_call: bool = True

    def begin(self) -> tuple[ModelCallType, int | None]:
        """消费一个调用阶段；只有首个顶层调用可以携带 idle 值。"""
        self.model_round += 1
        call_type = self.next_call_type
        idle_duration = (
            self.idle_duration_ms
            if self._first_model_call and call_type == "top_level_initial"
            else None
        )
        self._first_model_call = False
        self.next_call_type = "tool_continuation"
        return call_type, idle_duration

    def schedule(self, call_type: ModelCallType) -> None:
        """由 RunCoordinator 在 Interaction 恢复等明确边界设置下一阶段。"""
        self.next_call_type = call_type
