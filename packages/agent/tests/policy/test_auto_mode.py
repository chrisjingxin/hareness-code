"""AUTO 模式四层过滤器的单元测试。"""

from __future__ import annotations

import pytest

from harness_agent.policy.auto_mode import evaluate_auto_mode


class TestDestructiveDeleteGuard:
    """F3 DELETE 类守卫：绝对路径层级过浅时硬拦截。"""

    @pytest.mark.parametrize("file_path", ["/", "/home", "/usr/local", "C:/Users"])
    def test_shallow_absolute_delete_denied(self, file_path: str) -> None:
        """浅层绝对路径删除目标视为疑似高层目录，直接 deny。"""
        decision, reason = evaluate_auto_mode(
            "delete_file", {"file_path": file_path}, None
        )
        assert decision == "deny"
        assert "路径层级过浅" in reason

    def test_relative_delete_falls_back_to_ask(self) -> None:
        """相对路径不触发 DELETE 守卫，回退 F4 人工审批。"""
        decision, _reason = evaluate_auto_mode(
            "delete_file", {"file_path": "tmp/cache.db"}, None
        )
        assert decision == "ask"

    def test_deep_absolute_delete_not_denied(self) -> None:
        """深层绝对路径不属于浅层高危目录，不由 F3 硬拦截。"""
        decision, _reason = evaluate_auto_mode(
            "delete_file", {"file_path": "/home/user/file.txt"}, None
        )
        assert decision != "deny"
