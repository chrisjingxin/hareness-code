"""拒绝追踪机制回归测试。"""

from __future__ import annotations

from harness_agent.policy.denial_tracking import CONSECUTIVE_DENIAL_THRESHOLD, DenialTracker


def test_initial_state_no_warning():
    """初始状态不触发警告。"""
    tracker = DenialTracker()
    assert tracker.should_inject_warning() is False


def test_below_threshold_no_warning():
    """连续拒绝 2 次（低于阈值）不触发警告。"""
    tracker = DenialTracker()
    tracker.record_denial()
    tracker.record_denial()
    assert tracker.should_inject_warning() is False


def test_threshold_triggers_warning():
    """连续拒绝达到阈值时触发警告。"""
    tracker = DenialTracker()
    for _ in range(CONSECUTIVE_DENIAL_THRESHOLD):
        tracker.record_denial()
    assert tracker.should_inject_warning() is True


def test_approval_resets_counter():
    """审批通过后重置连续拒绝计数。"""
    tracker = DenialTracker()
    tracker.record_denial()
    tracker.record_denial()
    tracker.record_approval()
    assert tracker.consecutive_count == 0
    assert tracker.should_inject_warning() is False


def test_warning_message_contains_count():
    """警告消息包含当前连续拒绝次数。"""
    tracker = DenialTracker()
    for _ in range(CONSECUTIVE_DENIAL_THRESHOLD):
        tracker.record_denial()
    msg = tracker.warning_message()
    assert str(CONSECUTIVE_DENIAL_THRESHOLD) in msg
    assert "ask_user" in msg


def test_reset_clears_state():
    """reset 方法清除状态，恢复到初始条件。"""
    tracker = DenialTracker()
    for _ in range(CONSECUTIVE_DENIAL_THRESHOLD + 2):
        tracker.record_denial()
    tracker.reset()
    assert tracker.consecutive_count == 0
    assert tracker.should_inject_warning() is False
