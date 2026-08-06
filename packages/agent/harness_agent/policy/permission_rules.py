"""权限规则持久化模块：管理工具调用的 allow/deny/ask 规则匹配与存储。

规则评估采用两级优先级策略：
- 第一级：动作类型优先级 deny > allow > ask
- 第二级：同动作内来源优先级 session > project > user > system
"""
from __future__ import annotations

import fnmatch
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

RuleScope = Literal["session", "project", "user", "system"]
"""规则作用域：session 由调用方内存管理，project/user/system 持久化到 JSON 文件。"""

# 同动作内来源优先级（索引越小优先级越高）
_SCOPE_PRIORITY: dict[RuleScope, int] = {
    "session": 0,
    "project": 1,
    "user": 2,
    "system": 3,
}


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """单条权限规则，描述对特定工具和资源的访问决策。"""

    tool: str
    """工具名，支持通配符 ``*`` 和 ``?``。"""

    resource: str
    """资源模式，支持通配符 ``*`` 和 ``?``。"""

    effect: Literal["allow", "deny", "ask"]
    """匹配时的决策效果。"""

    scope: RuleScope = "session"
    """规则来源作用域，用于同动作内的优先级排序。"""


def matches_pattern(pattern: str, value: str) -> bool:
    """判断 value 是否匹配给定的通配符模式。

    使用 fnmatch 语义：``*`` 匹配任意字符序列，``?`` 匹配单个字符。
    """
    return fnmatch.fnmatch(value, pattern)


def evaluate_rules(
    tool: str, resource: str, rules: list[PermissionRule]
) -> str | None:
    """按两级优先级策略评估规则列表，返回生效的 effect 或 None。

    第一级：动作类型优先级 deny > allow > ask。
    第二级：同动作内来源优先级 session > project > user > system。

    具体逻辑：
    1. 遍历所有匹配的规则，按 effect 分组收集
    2. 任何 deny 命中 → 立即返回 "deny"（绝对优先，不可覆盖）
    3. 无 deny 时，有 allow 命中 → 返回 "allow"（用户明确批准优先于 ask）
    4. 仅有 ask 命中 → 返回 "ask"
    5. 无命中 → 返回 None

    同一 effect 内的命中规则会先按来源优先级排序
    （session > project > user > system），便于在需要定位具体命中规则时
    取到优先级最高的来源。

    无任何规则匹配时返回 None，由调用方决定默认行为。
    """
    matched_allow: list[PermissionRule] = []
    matched_ask: list[PermissionRule] = []

    for rule in rules:
        if not (matches_pattern(rule.tool, tool) and matches_pattern(rule.resource, resource)):
            continue
        if rule.effect == "deny":
            # deny 绝对优先，立即短路返回
            return "deny"
        elif rule.effect == "allow":
            matched_allow.append(rule)
        elif rule.effect == "ask":
            matched_ask.append(rule)

    # 同动作内按来源优先级排序（session > project > user > system），
    # 保证需要取具体命中规则时，首个元素始终是优先级最高的来源。
    matched_allow.sort(key=lambda r: _SCOPE_PRIORITY.get(r.scope, 99))
    matched_ask.sort(key=lambda r: _SCOPE_PRIORITY.get(r.scope, 99))

    # 无 deny 命中时，allow 优先于 ask（用户明确批准的操作不再弹窗）
    if matched_allow:
        return "allow"
    if matched_ask:
        return "ask"
    return None


# ---------------------------------------------------------------------------
# 工具调用统一规则匹配入口
# ---------------------------------------------------------------------------

# 执行 Shell 命令的工具：规则 resource 按命令语义匹配（拆段、词边界）
_SHELL_TOOLS: frozenset[str] = frozenset({"execute", "monitor"})
# 文件类工具：规则 resource 按目标路径通配匹配
_FILE_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "delete_file", "apply_patch"}
)


def extract_tool_resource(tool_name: str, tool_args: dict[str, Any]) -> str:
    """从工具参数中提取用于规则匹配的资源标识。

    - execute/monitor：command 参数；
    - 文件类工具：file_path 参数（反斜杠归一化为 POSIX 风格，保证
      Windows 路径也能匹配 ``src/**`` 这类规则）；
    - web_fetch：url 参数；
    - 其他工具：``"*"`` 通配。
    """
    if tool_name in _SHELL_TOOLS:
        return str(tool_args.get("command") or "*")
    if tool_name in _FILE_TOOLS:
        return str(tool_args.get("file_path") or "*").replace("\\", "/")
    if tool_name == "web_fetch":
        return str(tool_args.get("url") or "*")
    return "*"


def _match_domain(domain_pattern: str, url: str) -> bool:
    """判断 URL 的主机名是否等于域名模式或为其子域。

    ``WebFetch(domain:github.com)`` 按规格对 ``github.com`` 及其子域
    （如 ``api.github.com``）生效；匹配方式为域名匹配而非 glob。
    """
    try:
        hostname = urlparse(url).hostname or ""
    except ValueError:
        return False
    domain = domain_pattern.strip().lower()
    hostname = hostname.lower()
    if not domain or not hostname:
        return False
    return hostname == domain or hostname.endswith("." + domain)


def _rule_tool_matches(rule: PermissionRule, tool_name: str) -> bool:
    """判断规则的工具模式是否适用于当前工具名。

    除通配匹配外：execute/monitor 都执行 Shell 命令，两者的规则互相适用；
    ``MCP(...)``（tool=mcp_tool）规则适用于所有不在内置风险表中的外部工具
    ——MCP 工具名由各服务器决定，无法预先枚举。
    """
    if fnmatch.fnmatch(tool_name, rule.tool):
        return True
    if tool_name in _SHELL_TOOLS and rule.tool in _SHELL_TOOLS:
        return True
    if rule.tool == "mcp_tool":
        from harness_agent.policy.tool_risk import TOOL_KIND_MAP

        return tool_name not in TOOL_KIND_MAP
    return False


def _match_resource(
    rule: PermissionRule, tool_name: str, tool_args: dict[str, Any]
) -> bool:
    """判断单条规则的 resource 是否匹配当前调用的资源。"""
    resource = rule.resource
    if resource in ("", "*"):
        return True
    if tool_name == "web_fetch":
        url = str(tool_args.get("url") or "")
        if resource.startswith("domain:"):
            return _match_domain(resource[len("domain:"):], url)
        return fnmatch.fnmatch(url, resource)
    if tool_name in _FILE_TOOLS:
        file_path = str(tool_args.get("file_path") or "").replace("\\", "/")
        if not file_path:
            return False
        return fnmatch.fnmatch(file_path, resource.replace("\\", "/"))
    # MCP 等外部工具的 MCP(...) 规则：resource 匹配工具名本身
    if rule.tool == "mcp_tool":
        return fnmatch.fnmatch(tool_name, resource)
    # 其他工具没有结构化资源，字面通配即可
    return False


def evaluate_tool_rules(
    tool_name: str, tool_args: dict[str, Any], rules: list[PermissionRule]
) -> str | None:
    """按工具语义匹配权限规则，返回生效的 effect 或 None。

    这是所有审批路径（HITL 预检、deny 守卫、AUTO 过滤器、审批流水线）
    共用的唯一规则匹配入口，替代早期"整串 fnmatch"的粗匹配：

    - execute/monitor：命令按链式/管道拆段后逐段匹配（词边界安全），
      所有段命中 allow 才整体 allow，任一段 deny 即整体 deny；
      没有任何规则命中时返回 None（区别于命中 ask 规则）；
    - web_fetch：支持 ``domain:`` 域名匹配（含子域）与 URL 通配；
    - 文件类工具：目标路径通配匹配（路径分隔符归一化）；
    - 其他工具（task、MCP 等）：``resource="*"`` 或 MCP 名称匹配。

    优先级与 :func:`evaluate_rules` 一致：deny > allow > ask；
    无任何规则匹配时返回 None，由调用方决定默认行为。
    """
    if not rules:
        return None
    tool_args = tool_args or {}

    # Shell 工具走拆段合取评估；延迟导入避免与 bash_matcher 的循环依赖
    if tool_name in _SHELL_TOOLS:
        from harness_agent.policy.bash_matcher import evaluate_bash

        command = str(tool_args.get("command") or "").strip()
        if not command:
            return None
        applicable = [r for r in rules if _rule_tool_matches(r, tool_name)]
        if not applicable:
            return None
        result = evaluate_bash(command, applicable, _SHELL_TOOLS | {"*"})
        # 所有段都无规则命中时返回 None，交回默认审批管线；
        # evaluate_bash 此时会汇总为 "ask"，不能与命中 ask 规则混淆。
        if all(seg["decision"] is None for seg in result["segments"]):
            return None
        return str(result["decision"])

    matched_allow = False
    matched_ask = False
    for rule in rules:
        if not _rule_tool_matches(rule, tool_name):
            continue
        if not _match_resource(rule, tool_name, tool_args):
            continue
        if rule.effect == "deny":
            return "deny"
        if rule.effect == "allow":
            matched_allow = True
        elif rule.effect == "ask":
            matched_ask = True
    if matched_allow:
        return "allow"
    if matched_ask:
        return "ask"
    return None


def _system_settings_path() -> Path:
    """返回企业管控层配置文件路径（按平台选择）。"""
    if sys.platform == "win32":
        return Path("C:/ProgramData/harness/settings.json")
    elif sys.platform == "darwin":
        return Path("/Library/Application Support/Harness/settings.json")
    else:
        return Path("/etc/harness/settings.json")


def _settings_path(scope: RuleScope, project_dir: Path | None) -> Path | None:
    """根据作用域返回对应的 settings.json 路径；session 无文件返回 None。"""
    if scope == "session":
        return None
    if scope == "project":
        base = Path(project_dir) if project_dir is not None else Path.cwd()
        return base / ".harness" / "settings.json"
    if scope == "system":
        return _system_settings_path()
    # scope == "user"
    return Path.home() / ".harness" / "settings.json"


def _read_permissions(path: Path, scope: RuleScope) -> list[PermissionRule]:
    """从 JSON 文件读取 permissions 数组并转换为 PermissionRule 列表。

    兼容读取 DSL 字符串格式（如 ``"Bash(git clone *)"``）和旧 JSON 对象格式
    （如 ``{"tool": "execute", "resource": "git clone *", "effect": "allow"}``）。
    文件不存在、格式错误或字段缺失时静默返回空列表。
    """
    # 延迟导入，避免与 rule_parser 之间的循环依赖
    from harness_agent.policy.rule_parser import parse_rule_list

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []

    if not isinstance(data, dict):
        return []

    raw_rules = data.get("permissions")
    if not isinstance(raw_rules, list):
        return []

    rules = parse_rule_list(raw_rules, scope=scope)
    # 确保所有规则的 scope 统一为当前文件的作用域
    rules = [
        PermissionRule(tool=r.tool, resource=r.resource, effect=r.effect, scope=scope)  # type: ignore[arg-type]
        for r in rules
    ]
    return rules


def load_rules(
    project_dir: Path | None = None,
) -> dict[RuleScope, list[PermissionRule]]:
    """加载所有作用域的权限规则。

    - system: 从企业管控路径读取（文件不存在则静默返回空列表）。
    - project: 从 project_dir/.harness/settings.json 读取。
    - user: 从 ~/.harness/settings.json 读取。
    - session: 返回空列表，由调用方在内存中管理。

    文件不存在或格式错误时对应层级返回空列表，不抛异常。
    """
    result: dict[RuleScope, list[PermissionRule]] = {
        "session": [],
        "project": [],
        "user": [],
        "system": [],
    }

    system_path = _settings_path("system", None)
    if system_path is not None:
        result["system"] = _read_permissions(system_path, "system")

    project_path = _settings_path("project", project_dir)
    if project_path is not None:
        result["project"] = _read_permissions(project_path, "project")

    user_path = _settings_path("user", None)
    if user_path is not None:
        result["user"] = _read_permissions(user_path, "user")

    return result


def merge_rules(scoped_rules: dict[RuleScope, list[PermissionRule]]) -> list[PermissionRule]:
    """将四层作用域规则合并为 flat list，用于传入 evaluate_rules。

    合并顺序不影响评估结果（evaluate_rules 按动作优先级裁决），
    但保持 session → project → user → system 的顺序便于调试。
    """
    merged: list[PermissionRule] = []
    for scope in ("session", "project", "user", "system"):
        merged.extend(scoped_rules.get(scope, []))
    return merged


def save_rule(
    rule: PermissionRule, scope: RuleScope, project_dir: Path | None = None
) -> None:
    """将一条权限规则以 DSL 字符串格式追加写入对应作用域的 settings.json。

    - scope="session" 时不写文件，由调用方管理内存。
    - scope="project" 写入 project_dir/.harness/settings.json。
    - scope="user" 写入 ~/.harness/settings.json。
    - scope="system" 为只读层，不允许通过此函数写入。

    目录不存在时自动创建；已有文件内容保留，仅追加到 permissions 数组。
    规则以 DSL 格式写入，如 ``"Bash(git clone *)"``。
    """
    # 延迟导入，避免与 rule_parser 之间的循环依赖
    from harness_agent.policy.rule_parser import serialize_rule

    if scope == "system":
        # system 层为企业管控只读层，不允许运行时写入
        return
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

    permissions.append(serialize_rule(rule))
    data["permissions"] = permissions

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
