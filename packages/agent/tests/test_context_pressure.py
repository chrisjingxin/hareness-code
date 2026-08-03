"""ContextPressurePolicy 的确定性阈值、工具压力和 Run 阶段回归测试。"""

from __future__ import annotations

import pytest


def test_default_pressure_fixture_keeps_order_and_calibrated_values() -> None:
    """工具密集、文本密集和混合 fixture 校准默认压力行为。"""
    from harness_agent.context_pressure import (
        ContextPressurePolicy,
        ContextPressurePolicyConfig,
    )

    config = ContextPressurePolicyConfig()
    assert (
        config.report_ratio,
        config.micro_ratio,
        config.full_ratio,
        config.hard_ratio,
    ) == (0.50, 0.60, 0.80, 0.90)
    assert config.tool_pressure_tokens == 8_192
    assert config.tool_pressure_count == 2
    assert config.idle_threshold_ms == 900_000
    assert config.keep_recent == 1

    policy = ContextPressurePolicy(config)
    tool_dense = policy.decide(
        policy.measure(
            4_000,
            16_000,
            reclaimable_tool_tokens=16_400,
            reclaimable_tool_count=2,
        )
    )
    assert (tool_dense.action, tool_dense.reason) == ("micro", "tool_pressure")

    text_dense = policy.decide(policy.measure(13_000, 16_000))
    assert (text_dense.action, text_dense.reason) == ("full", "occupancy")

    mixed = policy.decide(
        policy.measure(
            8_800,
            16_000,
            reclaimable_tool_tokens=8_000,
            reclaimable_tool_count=1,
        )
    )
    assert (mixed.action, mixed.reason) == ("report", "occupancy")


def test_policy_rejects_unordered_thresholds() -> None:
    """调用方不能以配置覆盖 50/60/80/90 的顺序约束。"""
    from harness_agent.context_pressure import ContextPressurePolicyConfig

    with pytest.raises(ValueError, match="THRESHOLDS_UNORDERED"):
        ContextPressurePolicyConfig(report_ratio=0.7, micro_ratio=0.6)


def test_policy_idle_requires_top_level_initial_and_reclaimable_content() -> None:
    """idle 只在显式顶层首调且确有可回收内容时触发。"""
    from harness_agent.context_pressure import ContextPressurePolicy

    policy = ContextPressurePolicy()
    snapshot = policy.measure(
        4_000,
        16_000,
        reclaimable_tool_tokens=4_000,
        reclaimable_tool_count=1,
        idle_duration_ms=900_000,
    )
    assert policy.decide(snapshot, call_type="top_level_initial").reason == "idle"
    assert policy.decide(snapshot, call_type="tool_continuation").action == "none"
    assert policy.decide(snapshot, call_type="interaction_resume").action == "none"
    assert policy.decide(
        policy.measure(
            4_000,
            16_000,
            reclaimable_tool_tokens=4_000,
            reclaimable_tool_count=1,
            idle_duration_ms=None,
        ),
        call_type="top_level_initial",
    ).action == "none"


@pytest.mark.parametrize("invalid_idle", [-1, 900_000.0, "900000", True])
def test_policy_ignores_missing_or_abnormal_idle_timestamps(invalid_idle: object) -> None:
    """缺失、未来计算异常或非整数时间戳不得意外触发 idle。"""
    from harness_agent.context_pressure import ContextPressurePolicy

    policy = ContextPressurePolicy()
    snapshot = policy.measure(
        4_000,
        16_000,
        reclaimable_tool_tokens=4_000,
        reclaimable_tool_count=1,
        idle_duration_ms=invalid_idle,  # type: ignore[arg-type]
    )
    assert policy.decide(snapshot, call_type="top_level_initial").action == "none"


def test_policy_hard_and_real_overflow_are_distinct() -> None:
    """90% 仍是兼容的强制 full 水位，真实 overflow 才选择恢复动作。"""
    from harness_agent.context_pressure import ContextPressurePolicy

    policy = ContextPressurePolicy()
    hard = policy.decide(policy.measure(14_500, 16_000))
    assert hard.action == "full"
    overflow = policy.decide(policy.measure(1, 16_000), overflow=True)
    assert overflow.action == "overflow"


def test_model_call_lifecycle_consumes_idle_once_and_explicitly_schedules_resume() -> None:
    """生命周期阶段推进不读取时间戳，也不会把 Interaction 恢复当成 idle。"""
    from harness_agent.context_pressure import ModelCallLifecycle

    lifecycle = ModelCallLifecycle(
        next_call_type="top_level_initial", idle_duration_ms=900_000
    )
    assert lifecycle.begin() == ("top_level_initial", 900_000)
    assert lifecycle.begin() == ("tool_continuation", None)
    lifecycle.schedule("interaction_resume")
    assert lifecycle.begin() == ("interaction_resume", None)
