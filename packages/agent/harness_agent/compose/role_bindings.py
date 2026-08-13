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
        # Work Item 流水线 stage 名与 engine_services._StageDriverBase._stage 对齐；
        # 分类器与草稿阶段只读，Implement 复用主 Agent 全能力（无 ask_user），
        # Reviewer 保持只读能力交集。
        "work-item-intent": ComposeRoleBinding(
            role_id="work-item-intent",
            compose_stage="understand",
            planning=True,
        ),
        "work-item-grill": ComposeRoleBinding(
            role_id="work-item-grill",
            compose_stage="understand",
            planning=True,
        ),
        "work-item-task": ComposeRoleBinding(
            role_id="work-item-task",
            compose_stage="understand",
            planning=True,
        ),
        "work-item-spec": ComposeRoleBinding(
            role_id="work-item-spec",
            compose_stage="plan",
            planning=True,
        ),
        "work-item-plan": ComposeRoleBinding(
            role_id="work-item-plan",
            compose_stage="plan",
            planning=True,
        ),
        "work-item-implement": ComposeRoleBinding(
            role_id="work-item-implement",
            compose_stage="build",
        ),
        "work-item-review": ComposeRoleBinding(
            role_id="work-item-review",
            compose_stage="review",
            readonly=True,
        ),
        "work-item-report": ComposeRoleBinding(
            role_id="work-item-report",
            compose_stage="review",
            planning=True,
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
