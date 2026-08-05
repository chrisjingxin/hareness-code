"""AUTO 模式下危险 allow 规则的识别与剥离。

在 AUTO 模式下，宽泛的 Bash allow 规则（如 ``"python"``、``"bash"``、``"*"``）
会绕过审批流程，存在安全风险。本模块提供危险规则匹配和剥离能力，
供审批模式切换时使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness_agent.policy.permission_rules import PermissionRule

DANGEROUS_ALLOW_PATTERNS: list[str] = [
    "*",
    "python",
    "node",
    "bash",
    "sh",
    "sudo",
    "ssh",
    "curl",
    "wget",
    "npm",
    "npx",
    "yarn",
    "pip",
    "pip3",
    "eval",
    "chmod",
    "chown",
]
"""宽泛的 allow 规则模式列表，进入 AUTO 模式时应从规则集中剥离。"""


def is_dangerous_allow_rule(tool: str, resource: str) -> bool:
    """判断一条 allow 规则是否为危险宽泛规则。

    危险规则的定义：
    - tool 必须是 ``"execute"``（Bash 规则）
    - resource 为 ``"*"`` → True（全通配）
    - resource 以 DANGEROUS_ALLOW_PATTERNS 中任一模式为前缀 → True
    - 其他 → False

    前缀匹配设计为覆盖 ``"python *"``、``"bash -c ..."`` 等带参变体。
    """
    if tool != "execute":
        return False
    if resource == "*":
        return True
    for pattern in DANGEROUS_ALLOW_PATTERNS:
        if pattern == "*":
            continue  # 全通配已在上面单独判断
        if resource == pattern or resource.startswith(pattern + " "):
            return True
    return False


def strip_dangerous_rules(
    rules: list,
) -> tuple[list, list]:
    """从规则列表中分离危险 allow 规则。

    遍历所有规则，将 effect 为 ``"allow"`` 且经 ``is_dangerous_allow_rule``
    判定为危险的规则移入已剥离列表，其余保留在安全列表中。

    Returns:
        ``(safe_rules, stripped_rules)`` —— 安全规则列表和已剥离规则列表。
    """
    from harness_agent.policy.permission_rules import PermissionRule

    safe: list[PermissionRule] = []
    stripped: list[PermissionRule] = []

    for rule in rules:
        if not isinstance(rule, PermissionRule):
            safe.append(rule)
            continue
        if rule.effect == "allow" and is_dangerous_allow_rule(rule.tool, rule.resource):
            stripped.append(rule)
        else:
            safe.append(rule)

    return safe, stripped
