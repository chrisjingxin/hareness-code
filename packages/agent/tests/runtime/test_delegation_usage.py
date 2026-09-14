"""实验性委派用量账本：只用于诊断日志，不进入协议。"""

from __future__ import annotations

from harness_agent.runtime.delegation_usage import DelegationUsageLedger


def test_ledger_sums_root_and_child_calls_without_session_max() -> None:
    """两次 root 调用 10+20 与一次 child 7 合计 37，不取最大值。"""
    ledger = DelegationUsageLedger()
    ledger.record(
        execution_id="root-1",
        agent_id="main",
        profile_id="pro",
        usage={"input_tokens": 10, "output_tokens": 1},
    )
    ledger.record(
        execution_id="root-1",
        agent_id="main",
        profile_id="pro",
        usage={"input_tokens": 20, "output_tokens": 2},
    )
    ledger.record(
        execution_id="child-1",
        agent_id="explore",
        profile_id="fast",
        usage={"input_tokens": 7, "output_tokens": 3, "cached_tokens": 1},
    )
    summary = ledger.summary("root-1")
    assert summary["root_input_tokens"] == 30
    assert summary["root_output_tokens"] == 3
    assert summary["child_input_tokens"] == 7
    assert summary["child_output_tokens"] == 3
    assert summary["child_cached_input_tokens"] == 1
    assert summary["total_input_tokens"] == 37
    assert summary["total_output_tokens"] == 6
    assert summary["complete"] is True
    assert summary["child_count"] == 1


def test_ledger_marks_incomplete_when_usage_missing() -> None:
    """缺 input/output 的调用使合计不完整，已知值仍保留。"""
    ledger = DelegationUsageLedger()
    ledger.record(
        execution_id="root-1",
        agent_id="main",
        profile_id="pro",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    ledger.record(
        execution_id="child-1",
        agent_id="explore",
        profile_id="fast",
        usage={"input_tokens": None, "output_tokens": 4},
    )
    summary = ledger.summary("root-1")
    assert summary["root_input_tokens"] == 10
    assert summary["child_output_tokens"] == 4
    assert summary["total_output_tokens"] == 6
    assert summary["complete"] is False
