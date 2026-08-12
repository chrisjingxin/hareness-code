"""Compose Activity 的固定内置角色绑定。

该注册表是 Compose 选择 Managed stage Agent 的唯一来源。它不读取
Plugin catalog，也不接受运行时注册，避免模型、Workflow 或 Plugin 元数据
把外部 Agent 自动带入 Compose Activity。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


@dataclass(frozen=True, slots=True)
class ComposeRoleBinding:
    """一个固定 Compose 内置角色的能力收窄与投影配置。"""

    role_id: str
    compose_stage: str
    headless: bool = True
    readonly: bool = False
    planning: bool = False


_BUILTIN_ROLE_BINDINGS: Final[Mapping[str, ComposeRoleBinding]] = MappingProxyType(
    {
        "understand": ComposeRoleBinding(
            role_id="understand",
            compose_stage="understand",
            planning=True,
        ),
        "plan": ComposeRoleBinding(
            role_id="plan",
            compose_stage="plan",
            planning=True,
        ),
        "build": ComposeRoleBinding(
            role_id="build",
            compose_stage="build",
        ),
        "verify": ComposeRoleBinding(
            role_id="verify",
            compose_stage="verify",
        ),
        "requirement-reviewer": ComposeRoleBinding(
            role_id="requirement-reviewer",
            compose_stage="review",
            readonly=True,
        ),
        "code-reviewer": ComposeRoleBinding(
            role_id="code-reviewer",
            compose_stage="review",
            readonly=True,
        ),
    }
)


class RoleBindingRegistry:
    """只解析代码内固定的 Compose 内置角色，拒绝未知或 Plugin role。"""

    @property
    def role_ids(self) -> frozenset[str]:
        """返回固定角色集合，供调用方和测试校验而不暴露可写注册入口。"""
        return frozenset(_BUILTIN_ROLE_BINDINGS)

    def resolve(self, role_id: str) -> ComposeRoleBinding:
        """按精确内置 role ID 解析绑定；未知 ID 一律 fail closed。"""
        binding = _BUILTIN_ROLE_BINDINGS.get(role_id)
        if binding is None:
            raise ValueError("COMPOSE_ROLE_NOT_FOUND")
        return binding
