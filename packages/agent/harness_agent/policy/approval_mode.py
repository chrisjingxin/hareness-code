"""Harness Code 审批模式的稳定值、兼容别名和安全归一规则。"""

from __future__ import annotations

import logging
from typing import Literal, TypeAlias

from harness_agent.policy.dangerous_rules import strip_dangerous_rules

logger = logging.getLogger(__name__)

_dangerous_rules_stash: list = []
"""AUTO 模式下被剥离的危险 allow 规则暂存区，退出 AUTO 时恢复。"""

ApprovalMode: TypeAlias = Literal["plan", "default", "auto-edit", "auto", "yolo"]
"""面向配置、Agent 和 TUI 的规范审批模式。"""

DEFAULT_APPROVAL_MODE: ApprovalMode = "default"
"""未配置或无法识别时使用的保守默认审批模式。"""

_CANONICAL_MODES = frozenset({"plan", "default", "auto-edit", "auto", "yolo"})
_LEGACY_MODE_ALIASES = {"ask": "default"}

# 模式切换循环顺序（Shift+Tab 循环切换）
MODE_CYCLE: list[ApprovalMode] = ["plan", "default", "auto-edit", "auto", "yolo"]

# 预留：bubble 模式用于子代理审批冒泡，当前未启用
_RESERVED_MODES = frozenset({"bubble"})


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
    # 预留模式：bubble 当前未启用，降级为 default 并输出警告
    if normalized in _RESERVED_MODES:
        logger.warning("审批模式 %s 已预留但尚未启用，降级为 default", normalized)
        return DEFAULT_APPROVAL_MODE, f"审批模式 {normalized} 尚未启用，已降级为默认确认模式。"
    return DEFAULT_APPROVAL_MODE, "审批模式无效，已安全降级为默认确认模式。"


def on_mode_entered(mode: str, current_rules: list) -> list:
    """进入新模式时对规则做预处理。

    - 进入 ``"auto"`` 模式：调用 ``strip_dangerous_rules`` 剥离危险 allow
      规则，并将其暂存到 ``_dangerous_rules_stash``。
    - 退出 AUTO（模式非 ``"auto"`` 且 stash 非空）：将暂存的规则恢复回规则列表。
    - 其他情况：原样返回 current_rules。
    """
    global _dangerous_rules_stash
    if mode == "auto":
        safe, stripped = strip_dangerous_rules(current_rules)
        _dangerous_rules_stash.clear()
        _dangerous_rules_stash.extend(stripped)
        return safe
    elif _dangerous_rules_stash:
        restored = list(current_rules) + list(_dangerous_rules_stash)
        _dangerous_rules_stash.clear()
        return restored
    return current_rules


def next_mode(current: ApprovalMode, project_dir: str | None = None) -> ApprovalMode:
    """返回模式切换循环中的下一个模式。

    如果提供了 project_dir，会检查受信目录门禁：未受信目录不允许切换到
    auto/yolo 模式，强制保持在 default 模式。

    Args:
        current: 当前审批模式。
        project_dir: 项目目录路径，用于受信目录门禁检查。

    Returns:
        下一个审批模式。如果目录不受信且目标是 auto/yolo，返回 default。
    """
    try:
        idx = MODE_CYCLE.index(current)
        target = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
    except ValueError:
        return DEFAULT_APPROVAL_MODE

    # 受信目录门禁：未受信目录不允许切换到 auto/yolo
    if project_dir and target in ("auto", "yolo"):
        from harness_agent.policy.trust_gate import is_restricted_mode_for_untrusted
        restricted, _reason = is_restricted_mode_for_untrusted(target, project_dir)
        if restricted:
            logger.warning("未受信目录，权限模式锁定为 default")
            return DEFAULT_APPROVAL_MODE

    return target
