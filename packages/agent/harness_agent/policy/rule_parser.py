"""权限规则 DSL 解析/序列化器。

将 ``ToolName(RuleContent)`` 字符串 DSL 与内部 ``PermissionRule`` 对象互转，
同时兼容过渡期 JSON 格式。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from harness_agent.policy.permission_rules import PermissionRule

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 工具名映射表
# ---------------------------------------------------------------------------

# DSL 名 → 内部工具名
_TOOL_NAME_MAP: dict[str, str] = {
    "Bash": "execute",
    "Read": "read_file",
    "Edit": "edit_file",
    "Write": "write_file",
    "Delete": "delete_file",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Grep": "grep",
    "Glob": "glob",
    "LS": "ls",
    "Agent": "task",
    "Task": "task",
    "MCP": "mcp_tool",
    "NotebookRead": "read_file",
    "NotebookEdit": "edit_file",
    "ToolSearch": "tool_search",
}

# 内部工具名 → DSL 名（取规范 DSL 名；Agent/Task 均归一到 Agent）
_TOOL_NAME_REVERSE: dict[str, str] = {
    "execute": "Bash",
    "read_file": "Read",
    "edit_file": "Edit",
    "write_file": "Write",
    "delete_file": "Delete",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "grep": "Grep",
    "glob": "Glob",
    "ls": "LS",
    "task": "Agent",
    "mcp_tool": "MCP",
    "tool_search": "ToolSearch",
}

# 解析正则：ToolName 或 ToolName(content)
# 工具名允许字母、数字、下划线和连字符（MCP 工具名可能包含连字符）；
# 内容中允许出现转义括号 \\( 和 \\)
_PARSE_PATTERN = re.compile(r"^([A-Za-z][\w-]*)(?:\((.+)\))?$")


def _unescape_parens(raw: str) -> str:
    """将 DSL 内容中的转义括号还原为字面括号。

    ``\\(`` → ``(``、``\\)`` → ``)``。
    """
    return raw.replace(r"\(", "(").replace(r"\)", ")")


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def parse_rule(
    rule_str: str, effect: str = "allow", scope: str = "session"
) -> PermissionRule:
    """解析 ``ToolName(RuleContent)`` 格式字符串为 ``PermissionRule``。

    Args:
        rule_str: DSL 规则字符串，如 ``"Bash(git clone *)"`` 或 ``"Read"``。
        effect: 决策效果，``"allow"`` / ``"deny"`` / ``"ask"``。
        scope: 规则作用域，``"session"`` / ``"project"`` / ``"user"`` / ``"system"``。

    Returns:
        解析后的不可变权限规则对象。

    Raises:
        ValueError: 无法解析规则字符串时抛出。

    Examples:
        >>> parse_rule("Bash(git clone *)")
        PermissionRule(tool='execute', resource='git clone *', ...)

        >>> parse_rule("WebFetch(domain:github.com)")
        PermissionRule(tool='web_fetch', resource='domain:github.com', ...)

        >>> parse_rule("Read")
        PermissionRule(tool='read_file', resource='*', ...)
    """
    if not isinstance(rule_str, str) or not rule_str.strip():
        raise ValueError(f"无效的规则字符串: {rule_str!r}")

    rule_str = rule_str.strip()
    m = _PARSE_PATTERN.match(rule_str)
    if m is None:
        raise ValueError(f"无法解析规则字符串: {rule_str!r}")

    dsl_tool = m.group(1)
    raw_resource = m.group(2)

    # 已知 DSL 名映射为规范工具名；未知名称（如 MCP 工具名）原样保留，
    # 保证 approve_project 生成的规则与运行时工具名可往返。
    tool = _TOOL_NAME_MAP.get(dsl_tool, dsl_tool)

    if raw_resource is None:
        resource = "*"
    else:
        resource = _unescape_parens(raw_resource)

    if effect not in ("allow", "deny", "ask"):
        raise ValueError(f"无效的 effect: {effect!r}，需为 allow/deny/ask")

    return PermissionRule(tool=tool, resource=resource, effect=effect, scope=scope)  # type: ignore[arg-type]


def serialize_rule(rule: PermissionRule) -> str:
    """将 ``PermissionRule`` 反向序列化为 DSL 字符串。

    Args:
        rule: 权限规则对象。

    Returns:
        DSL 格式字符串，如 ``"Bash(git clone *)"`` 或 ``"Write"``。

    Examples:
        >>> serialize_rule(PermissionRule(tool='execute', resource='git clone *', effect='allow'))
        'Bash(git clone *)'

        >>> serialize_rule(PermissionRule(tool='write_file', resource='*', effect='deny'))
        'Write'
    """
    dsl_name = _TOOL_NAME_REVERSE.get(rule.tool, rule.tool)

    if rule.resource == "*":
        return dsl_name

    return f"{dsl_name}({rule.resource})"


def parse_rule_list(
    raw_rules: list[Any], effect: str = "allow", scope: str = "session"
) -> list[PermissionRule]:
    """兼容解析 JSON 和 DSL 两种格式的规则列表。

    - 字符串 ``"Bash(git clone *)"`` → 调用 ``parse_rule``。
    - 字典 ``{"tool": "execute", "resource": "git clone *", "effect": "allow"}``
      → 从 JSON 构造 ``PermissionRule``（向后兼容过渡期）。
    - 无效条目跳过并记录 warning。

    Args:
        raw_rules: 混合格式的规则列表。
        effect: 字符串条目解析时的默认 effect。
        scope: 字符串条目解析时的默认 scope。

    Returns:
        成功解析的 ``PermissionRule`` 列表。
    """
    if not isinstance(raw_rules, list):
        return []

    result: list[PermissionRule] = []
    for item in raw_rules:
        try:
            if isinstance(item, str):
                result.append(parse_rule(item, effect=effect, scope=scope))
            elif isinstance(item, dict):
                tool = item.get("tool")
                resource = item.get("resource")
                item_effect = item.get("effect", effect)
                item_scope = item.get("scope", scope)
                if (
                    isinstance(tool, str)
                    and isinstance(resource, str)
                    and item_effect in ("allow", "deny", "ask")
                ):
                    result.append(
                        PermissionRule(
                            tool=tool,
                            resource=resource,
                            effect=item_effect,  # type: ignore[arg-type]
                            scope=item_scope,  # type: ignore[arg-type]
                        )
                    )
                else:
                    logger.warning("跳过无效 JSON 规则条目（字段缺失或类型错误）: %r", item)
            else:
                logger.warning("跳过无效规则条目（类型 %s 不支持）: %r", type(item).__name__, item)
        except (ValueError, TypeError) as exc:
            logger.warning("跳过无效规则条目: %r，原因: %s", item, exc)
    return result


def load_rules_from_dsl(
    permissions: list[str], scope: str = "user"
) -> list[PermissionRule]:
    """从 DSL 字符串列表加载规则。

    每条字符串格式为 ``[allow:|deny:|ask:]ToolName(RuleContent)``，
    前缀省略时默认使用 ``allow``。

    Args:
        permissions: DSL 规则字符串列表。
        scope: 所有规则的默认作用域。

    Returns:
        成功解析的 ``PermissionRule`` 列表。

    Examples:
        >>> load_rules_from_dsl(["allow:Bash(git *)", "deny:Bash(rm *)", "WebFetch(domain:github.com)"])
    """
    if not isinstance(permissions, list):
        return []

    result: list[PermissionRule] = []
    for entry in permissions:
        if not isinstance(entry, str) or not entry.strip():
            continue
        try:
            result.append(_parse_permission_entry(entry, scope=scope))
        except (ValueError, TypeError) as exc:
            logger.warning("跳过无效 DSL 权限条目: %r，原因: %s", entry, exc)
    return result


def _parse_permission_entry(entry: str, scope: str = "user") -> PermissionRule:
    """解析单条 DSL 权限条目，提取 effect 前缀和规则体。

    格式：``[allow:|deny:|ask:]ToolName(RuleContent)``。
    """
    entry = entry.strip()
    effect: str = "allow"
    rule_body: str = entry

    for prefix in ("allow:", "deny:", "ask:"):
        if entry.startswith(prefix):
            effect = prefix.rstrip(":")
            rule_body = entry[len(prefix) :].strip()
            break

    if not rule_body:
        raise ValueError(f"DSL 权限条目缺少规则体: {entry!r}")

    return parse_rule(rule_body, effect=effect, scope=scope)
