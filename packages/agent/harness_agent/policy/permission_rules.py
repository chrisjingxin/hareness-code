"""权限规则持久化模块：管理工具调用的 allow/deny/ask 规则匹配与存储。"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

RuleScope = Literal["session", "project", "user"]
"""规则作用域：session 由调用方内存管理，project/user 持久化到 JSON 文件。"""


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """单条权限规则，描述对特定工具和资源的访问决策。"""

    tool: str
    """工具名，支持通配符 ``*`` 和 ``?``。"""

    resource: str
    """资源模式，支持通配符 ``*`` 和 ``?``。"""

    effect: Literal["allow", "deny", "ask"]
    """匹配时的决策效果。"""


def matches_pattern(pattern: str, value: str) -> bool:
    """判断 value 是否匹配给定的通配符模式。

    使用 fnmatch 语义：``*`` 匹配任意字符序列，``?`` 匹配单个字符。
    """
    return fnmatch.fnmatch(value, pattern)


def evaluate_rules(
    tool: str, resource: str, rules: list[PermissionRule]
) -> str | None:
    """按"最后匹配优先"策略评估规则列表，返回生效的 effect 或 None。

    从后往前遍历规则列表，第一个同时匹配 tool 和 resource 的规则生效。
    无任何规则匹配时返回 None，由调用方决定默认行为。
    """
    for rule in reversed(rules):
        if matches_pattern(rule.tool, tool) and matches_pattern(rule.resource, resource):
            return rule.effect
    return None


def _settings_path(scope: RuleScope, project_dir: Path | None) -> Path | None:
    """根据作用域返回对应的 settings.json 路径；session 无文件返回 None。"""
    if scope == "session":
        return None
    if scope == "project":
        base = project_dir if project_dir is not None else Path.cwd()
        return base / ".harness" / "settings.json"
    # scope == "user"
    return Path.home() / ".harness" / "settings.json"


def _read_permissions(path: Path) -> list[PermissionRule]:
    """从 JSON 文件读取 permissions 数组并转换为 PermissionRule 列表。

    文件不存在、JSON 格式错误或字段缺失时静默返回空列表。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []

    if not isinstance(data, dict):
        return []

    raw_rules = data.get("permissions")
    if not isinstance(raw_rules, list):
        return []

    rules: list[PermissionRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        resource = item.get("resource")
        effect = item.get("effect")
        if (
            isinstance(tool, str)
            and isinstance(resource, str)
            and effect in ("allow", "deny", "ask")
        ):
            rules.append(PermissionRule(tool=tool, resource=resource, effect=effect))
    return rules


def load_rules(
    project_dir: Path | None = None,
) -> dict[RuleScope, list[PermissionRule]]:
    """加载所有作用域的权限规则。

    - project: 从 project_dir/.harness/settings.json 读取。
    - user: 从 ~/.harness/settings.json 读取。
    - session: 返回空列表，由调用方在内存中管理。

    文件不存在或格式错误时对应层级返回空列表，不抛异常。
    """
    result: dict[RuleScope, list[PermissionRule]] = {
        "session": [],
        "project": [],
        "user": [],
    }

    project_path = _settings_path("project", project_dir)
    if project_path is not None:
        result["project"] = _read_permissions(project_path)

    user_path = _settings_path("user", None)
    if user_path is not None:
        result["user"] = _read_permissions(user_path)

    return result


def save_rule(
    rule: PermissionRule, scope: RuleScope, project_dir: Path | None = None
) -> None:
    """将一条权限规则追加写入对应作用域的 settings.json。

    - scope="session" 时不写文件，由调用方管理内存。
    - scope="project" 写入 project_dir/.harness/settings.json。
    - scope="user" 写入 ~/.harness/settings.json。

    目录不存在时自动创建；已有文件内容保留，仅追加到 permissions 数组。
    """
    path = _settings_path(scope, project_dir)
    if path is None:
        return

    # 读取现有配置，格式错误时从空对象开始
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}

    permissions = data.get("permissions")
    if not isinstance(permissions, list):
        permissions = []

    permissions.append(asdict(rule))
    data["permissions"] = permissions

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
