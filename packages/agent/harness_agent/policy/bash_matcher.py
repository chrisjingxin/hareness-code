"""Bash 命令规则匹配器：命令前缀匹配和安全门评估引擎。

本模块是通用 permission_rules.py 的 Shell 专项补充，在 bash_parser 的
命令解析基础上实现命令级规则匹配。核心能力包括：

- 词边界安全的命令前缀匹配（token 级别）
- 基于 glob 的自由模式匹配
- 单段命令的权限规则评估
- 整条链式命令的完整安全门评估（含 CWE-178 防护）
"""
from __future__ import annotations

import re
from typing import Any, Callable

from harness_agent.policy.bash_parser import extract_segments, strip_wrappers
from harness_agent.policy.permission_rules import PermissionRule


def matches_command_prefix(pattern: str, command: str) -> bool:
    """词边界安全的命令前缀匹配。

    将 pattern 和 command 按空格拆分为 token 序列后，检查 command 的
    token 序列是否以 pattern 的 token 序列开头（包含完全匹配的情况）。

    Args:
        pattern: 前缀匹配模式，如 ``"git"`` 或 ``"git commit"``。
        command: 待匹配的命令字符串，如 ``"git status"``。

    Returns:
        匹配时返回 ``True``，否则返回 ``False``。

    Examples:
        - ``"git"`` 匹配 ``"git status"``（后跟空格，词边界）
        - ``"git"`` 不匹配 ``"gitleaks detect"``（不是词边界）
        - ``"git commit"`` 匹配 ``"git commit -m x"``（带子命令）
    """
    pattern = pattern.strip()
    if not pattern:
        return False

    pattern_tokens = pattern.split()
    command_tokens = command.strip().split()

    if len(pattern_tokens) > len(command_tokens):
        return False

    return command_tokens[: len(pattern_tokens)] == pattern_tokens


def matches_command_glob(pattern: str, command: str) -> bool:
    """Freeform glob 模式匹配（``*`` 可跨单词边界）。

    将 glob 模式转换为正则表达式后进行全匹配：``*`` 映射为 ``.*?``，
    其余字符使用 ``re.escape`` 转义。

    Args:
        pattern: glob 模式，如 ``"git *"`` 或 ``"git clone *"``。
        command: 待匹配的命令字符串。

    Returns:
        匹配时返回 ``True``，否则返回 ``False``。

    Examples:
        - ``"git *"`` 匹配 ``"git status"``、``"git commit -m x"``
        - ``"git clone *"`` 匹配 ``"git clone https://..."``
    """
    pattern = pattern.strip()
    if not pattern:
        return False

    # 将 glob 转换为正则：* → .*?，其余转义
    regex_parts: list[str] = []
    for char in pattern:
        if char == "*":
            regex_parts.append(".*?")
        else:
            regex_parts.append(re.escape(char))
    regex = "".join(regex_parts)

    try:
        return re.fullmatch(regex, command.strip()) is not None
    except re.error:
        return False


def evaluate_bash_segment(
    segment: str,
    rules: list[PermissionRule],
    tool_names: frozenset[str] = frozenset({"execute", "*"}),
) -> str | None:
    """对单个 Bash 段的权限规则评估。

    只考虑 ``tool`` 属于 ``tool_names`` 的规则（默认 ``execute`` 与通配；
    ``monitor`` 等同样执行 Shell 的工具可传入自己的工具名），对每条规则
    调用合适的前缀匹配函数，按 deny > allow > ask 优先级返回决策。

    Args:
        segment: 单个 Bash 命令段字符串。
        rules: 权限规则列表。
        tool_names: 参与评估的规则工具名集合。

    Returns:
        ``"allow"`` | ``"deny"`` | ``"ask"`` | ``None``：
        匹配到规则时返回对应效果，无匹配时返回 ``None``。
    """
    segment = segment.strip()
    if not segment:
        return None

    matched_allow = False
    matched_ask = False

    for rule in rules:
        # 只考虑指定工具名或通配工具规则
        if rule.tool not in tool_names:
            continue

        # 根据 resource 是否含 * 选择匹配策略
        if "*" in rule.resource:
            matched = matches_command_glob(rule.resource, segment)
        else:
            matched = matches_command_prefix(rule.resource, segment)

        if not matched:
            continue

        if rule.effect == "deny":
            return "deny"
        elif rule.effect == "allow":
            matched_allow = True
        elif rule.effect == "ask":
            matched_ask = True

    if matched_allow:
        return "allow"
    if matched_ask:
        return "ask"
    return None


def evaluate_bash(
    command: str,
    rules: list[PermissionRule],
    tool_names: frozenset[str] = frozenset({"execute", "*"}),
) -> dict[str, Any]:
    """对整条 Bash 命令的完整权限评估。

    评估流程：
    1. 对 command 做 trim 处理（CWE-178 防护）
    2. 调用 ``extract_segments`` 拆分为独立段
    3. 每段调用 ``strip_wrappers`` 剥离包装器
    4. 每段调用 ``evaluate_bash_segment`` 评估
    5. 汇总：任何段 deny → 整体 deny；所有段 allow → 整体 allow（合取式）；
       存在 ask 或 None → 整体 ask（fallback）

    Args:
        command: 待评估的完整 Bash 命令字符串。
        rules: 权限规则列表。
        tool_names: 参与评估的规则工具名集合，透传给逐段评估。

    Returns:
        字典 ``{"decision": str, "segments": list}``：
        - ``decision``：``"allow"`` | ``"deny"`` | ``"ask"``
        - ``segments``：每个段的逐段决策列表，见 :func:`_make_segment_result`
    """
    # CWE-178 防护：去除首尾空白
    command = command.strip()

    if not command:
        return {"decision": "deny", "segments": []}

    # 拆分链式命令段
    raw_segments = extract_segments(command)

    segment_results: list[dict[str, Any]] = []
    has_deny = False
    has_ask_or_none = False
    all_allow = True

    for raw_segment in raw_segments:
        # 剥离包装器后评估
        processed = strip_wrappers(raw_segment, max_depth=3)
        decision = evaluate_bash_segment(processed, rules, tool_names)
        segment_results.append(
            _make_segment_result(raw_segment, processed, decision)
        )

        if decision == "deny":
            has_deny = True
            all_allow = False
        elif decision in ("allow",):
            pass  # allow 不影响 all_allow 状态
        else:
            # ask 或 None
            has_ask_or_none = True
            all_allow = False

    # 汇总决策
    if has_deny:
        overall = "deny"
    elif all_allow:
        overall = "allow"
    else:
        overall = "ask"

    return {"decision": overall, "segments": segment_results}


def remainder_after_command_prefix(command: str, prefix: str) -> str | None:
    """剥掉命令前缀后返回剩余部分；前缀不匹配时返回 ``None``。

    使用与 :func:`matches_command_prefix` 相同的词边界拆分，保证判定一致。
    """
    prefix = prefix.strip()
    if not prefix:
        return None
    prefix_tokens = prefix.split()
    command_tokens = command.strip().split()
    if len(prefix_tokens) > len(command_tokens):
        return None
    if command_tokens[: len(prefix_tokens)] != prefix_tokens:
        return None
    return " ".join(command_tokens[len(prefix_tokens) :])


def find_matching_allow_resource(
    segment: str,
    rules: list[PermissionRule],
    tool_names: frozenset[str] = frozenset({"execute", "monitor", "*"}),
) -> str | None:
    """返回命中该命令段的第一条 allow 规则的 resource；无匹配返回 ``None``。"""
    segment = segment.strip()
    if not segment:
        return None
    for rule in rules:
        if rule.tool not in tool_names or rule.effect != "allow":
            continue
        if "*" in rule.resource:
            matched = matches_command_glob(rule.resource, segment)
        else:
            matched = matches_command_prefix(rule.resource, segment)
        if matched:
            return rule.resource
    return None


def allow_remainder_triggers_floor(
    command: str,
    rules: list[PermissionRule],
    *,
    floor_evaluator: Callable[[str], dict[str, Any]] | None = None,
) -> bool:
    """allow 规则命中后，按剩余部分检查安全底线是否触发（ZC-117 约束 B）。

    - 纯前缀规则：剥掉前缀后对 remainder 跑底线
    - 含 ``*`` 的手写宽规则：对整段命令跑底线
    - 任一段触发底线 → ``True``（应强制弹窗）
    """
    from harness_agent.policy.bash_floors import evaluate_safety_floors

    evaluator = floor_evaluator or evaluate_safety_floors
    for raw_segment in extract_segments(command):
        processed = strip_wrappers(raw_segment, max_depth=3)
        resource = find_matching_allow_resource(processed, rules)
        if resource is None:
            continue
        if "*" in resource:
            subject = processed
        else:
            remainder = remainder_after_command_prefix(processed, resource)
            if remainder is None:
                subject = processed
            elif not remainder.strip():
                continue
            else:
                subject = remainder
        floors = evaluator(subject)
        if floors.get("any_floor_triggered"):
            return True
    return False


def _make_segment_result(
    raw: str, processed: str, decision: str | None
) -> dict[str, Any]:
    """构造单段的评估结果字典。

    Args:
        raw: 原始命令段。
        processed: 剥离包装器后的命令段。
        decision: 评估决策，可能为 None。

    Returns:
        包含 ``raw``、``processed`` 和 ``decision`` 键的字典。
    """
    return {
        "raw": raw,
        "processed": processed,
        "decision": decision,
    }
