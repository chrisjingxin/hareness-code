"""Compose 固定内置角色绑定测试。"""

from __future__ import annotations

import pytest


def test_role_binding_registry_only_resolves_fixed_builtin_roles() -> None:
    """Compose 只能使用预定义内置角色，不能从 Plugin 元数据自动扩展。"""
    from harness_agent.compose.role_bindings import RoleBindingRegistry

    registry = RoleBindingRegistry()

    assert registry.role_ids == frozenset(
        {
            "understand",
            "plan",
            "build",
            "verify",
            "requirement-reviewer",
            "code-reviewer",
        }
    )
    assert registry.resolve("understand").planning is True
    assert registry.resolve("requirement-reviewer").readonly is True
    with pytest.raises(ValueError, match="COMPOSE_ROLE_NOT_FOUND"):
        registry.resolve("plugin-reviewer")
