"""Harness Code 审批模式的稳定值、兼容别名和安全归一规则。"""

from __future__ import annotations

from typing import Literal, TypeAlias

ApprovalMode: TypeAlias = Literal["plan", "default", "auto-edit", "yolo"]
"""面向配置、Agent 和 TUI 的规范审批模式。"""

DEFAULT_APPROVAL_MODE: ApprovalMode = "default"
"""未配置或无法识别时使用的保守默认审批模式。"""

_CANONICAL_MODES = frozenset({"plan", "default", "auto-edit", "yolo"})
_LEGACY_MODE_ALIASES = {"ask": "default"}


def parse_approval_mode(value: object | None) -> tuple[ApprovalMode, str | None]:
    """将配置值归一为规范模式，并为兼容或非法输入返回安全提示。

    这里不抛出配置错误：审批模式配置失误不能意外阻止 Agent 启动，也不能
    放宽权限。因此任何未知值都必须回落到 ``default``，由 TUI 显示提示。
    """
    if value is None:
        return DEFAULT_APPROVAL_MODE, None
    normalized = str(value).strip().lower()
    if not normalized:
        return DEFAULT_APPROVAL_MODE, None
    if normalized in _CANONICAL_MODES:
        return normalized, None  # type: ignore[return-value]
    if normalized in _LEGACY_MODE_ALIASES:
        return (
            _LEGACY_MODE_ALIASES[normalized],
            "审批模式 ask 已按默认确认模式执行。",
        )
    return DEFAULT_APPROVAL_MODE, "审批模式无效，已安全降级为默认确认模式。"
