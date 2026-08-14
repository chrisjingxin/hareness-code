"""Agent Plugins 1.0 MCP schema、安全路径和字面配置校验。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from harness_agent.plugins.common import safe_package_path
from harness_agent.plugins.model import PluginError


MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
"""固定的 Agent Plugins 1.0.0 MCP schema 标识。"""

_TOP_LEVEL_FIELDS = frozenset({"$schema", "mcpServers"})
_STDIO_FIELDS = frozenset({"type", "command", "args", "env", "cwd"})
_HTTP_FIELDS = frozenset({"type", "url", "headers"})
_MCP_TRANSPORTS = frozenset({"stdio", "streamable-http", "sse"})
# WebSocket 不是 Agent Plugins 1.0 closed union 的成员，但它是一个明确的
# transport 名称；将它单独归类可以保留“客户端不支持”与配置形状错误的边界。
_UNSUPPORTED_TRANSPORTS = frozenset({"websocket"})
_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
_PLACEHOLDER_SYNTAX_RE = re.compile(r"\$\{[^}]*\}")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_COMMAND_CONTROL_RE = re.compile(r"[\x00\r\n\t\f\v]")

McpServerStatus = Literal["valid", "unsupported", "invalid"]


@dataclass(frozen=True, slots=True)
class ValidatedMcpServer:
    """通过 Agent Plugins MCP closed union 的 server 条目。"""

    name: str
    transport: str


@dataclass(frozen=True, slots=True)
class McpDocumentValidation:
    """顶层校验结果及逐条失败边界。"""

    servers: tuple[ValidatedMcpServer, ...]
    invalid: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


def validate_mcp_document(
    document: Mapping[str, object],
    *,
    root: Path | None = None,
) -> McpDocumentValidation:
    """校验 MCP 顶层 closed schema，并按 server 隔离可恢复错误。"""
    if set(document) - _TOP_LEVEL_FIELDS:
        unknown = ", ".join(sorted(str(field) for field in set(document) - _TOP_LEVEL_FIELDS))
        raise PluginError("PLUGIN_MCP_INVALID", f"mcp.json 包含未知顶层字段：{unknown}")
    if document.get("$schema") != MCP_SCHEMA_ID:
        raise PluginError(
            "PLUGIN_MCP_SCHEMA_UNSUPPORTED",
            "mcp.json schema 缺失、不受支持或与 Plugin 版本不匹配",
        )
    if "mcpServers" not in document or not isinstance(document["mcpServers"], Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", "mcpServers 必须是 object")

    valid: list[ValidatedMcpServer] = []
    invalid: list[str] = []
    unsupported: list[str] = []
    for name, raw in document["mcpServers"].items():
        label = name if isinstance(name, str) else repr(name)
        try:
            valid.append(validate_mcp_server(name, raw, root=root))
        except PluginError as exc:
            message = f"{label}: {exc.code}: {exc}"
            if exc.code == "PLUGIN_MCP_TRANSPORT_UNSUPPORTED":
                unsupported.append(message)
            else:
                invalid.append(message)
    return McpDocumentValidation(
        servers=tuple(valid),
        invalid=tuple(invalid),
        unsupported=tuple(unsupported),
    )


def validate_mcp_server(
    name: object,
    raw: object,
    *,
    root: Path | None = None,
) -> ValidatedMcpServer:
    """校验一个 MCP server 的 closed discriminated union。"""
    if not isinstance(name, str):
        raise PluginError("PLUGIN_MCP_INVALID", "MCP server name 必须是字符串")
    if not isinstance(raw, Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 必须是 object")

    transport = raw.get("type")
    if not isinstance(transport, str):
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} transport 无效")
    if transport in _UNSUPPORTED_TRANSPORTS:
        raise PluginError(
            "PLUGIN_MCP_TRANSPORT_UNSUPPORTED",
            f"MCP server {name} transport {transport!r} 当前不受 Harness 支持",
        )
    if transport not in _MCP_TRANSPORTS:
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} transport 无效")

    allowed = _STDIO_FIELDS if transport == "stdio" else _HTTP_FIELDS
    unknown = set(raw) - allowed
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise PluginError(
            "PLUGIN_MCP_INVALID",
            f"MCP server {name} 包含 variant 外字段：{fields}",
        )

    if transport == "stdio":
        _validate_stdio_server(name, raw, root=root)
    else:
        _validate_http_server(name, raw)
    return ValidatedMcpServer(name=name, transport=transport)


def validate_stdio_command(command: object, *, root: Path | None = None) -> str:
    """校验 stdio command 是 bare executable 或 `./` 包内路径。"""
    if not isinstance(command, str) or not command or command != command.strip():
        raise PluginError("PLUGIN_MCP_COMMAND_INVALID", "stdio command 必须是单一 executable token")
    if _COMMAND_CONTROL_RE.search(command) or any(char.isspace() for char in command):
        raise PluginError("PLUGIN_MCP_COMMAND_INVALID", "stdio command 不能包含 shell 空白或控制字符")
    if "${" in command:
        raise PluginError("PLUGIN_MCP_COMMAND_INVALID", "stdio command 不支持 placeholder")
    if command.startswith("./"):
        if root is not None:
            safe_package_path(root, command, require_exists=False)
        return command
    if "/" in command or "\\" in command or command in {".", ".."}:
        raise PluginError(
            "PLUGIN_MCP_COMMAND_INVALID",
            "stdio command 必须是 bare executable 或 ./ 包内路径",
        )
    return command


def validate_stdio_cwd(cwd: object) -> None:
    """校验 cwd 的 portable 语法；实际 root/data containment 在安装实例确定后完成。"""
    if cwd is None:
        return
    if not isinstance(cwd, str) or not cwd:
        raise PluginError("PLUGIN_MCP_CWD_INVALID", "stdio cwd 必须是非空字符串")
    if cwd.startswith("./"):
        _reject_parent_path(cwd[2:])
        return
    if cwd == "${PLUGIN_ROOT}" or cwd.startswith("${PLUGIN_ROOT}/"):
        _reject_parent_path(cwd[len("${PLUGIN_ROOT}") + 1 :])
        return
    if cwd == "${PLUGIN_DATA}" or cwd.startswith("${PLUGIN_DATA}/"):
        _reject_parent_path(cwd[len("${PLUGIN_DATA}") + 1 :])
        return
    raise PluginError(
        "PLUGIN_MCP_CWD_INVALID",
        "stdio cwd 必须是 ./、${PLUGIN_ROOT} 或 ${PLUGIN_DATA} 路径",
    )


def replace_plugin_placeholders(value: str, *, root: Path, data: Path) -> str:
    """对运行字段做一次、非递归的两个保留变量替换；其他文本保持字面值。"""
    replacements = {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data)}
    return _PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], value)


def resolve_portable_cwd(cwd: str | None, *, root: Path, data: Path) -> Path:
    """解析 cwd 并确保 root/data 两个边界不会被越过。"""
    if cwd is None:
        return root.resolve()
    validate_stdio_cwd(cwd)
    if cwd.startswith("./"):
        return safe_package_path(root, cwd, require_exists=False).resolve()
    if cwd == "${PLUGIN_ROOT}" or cwd.startswith("${PLUGIN_ROOT}/"):
        return _resolve_contained_path(
            replace_plugin_placeholders(cwd, root=root, data=data), root, "Plugin root"
        )
    return _resolve_contained_path(
        replace_plugin_placeholders(cwd, root=root, data=data), data, "Plugin data"
    )


def validate_http_url(url: object, *, field: str = "url") -> str:
    """校验 portable HTTP/SSE URL 的 scheme、origin 和敏感语法。"""
    if not isinstance(url, str) or not url or any(ord(char) <= 0x20 for char in url):
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} 必须是绝对 HTTP(S) URL")
    if _PLACEHOLDER_SYNTAX_RE.search(url):
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} 不允许 placeholder")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} URL 无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} 必须是绝对 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} 不允许 userinfo")
    if parsed.fragment:
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} 不允许 fragment")
    if port is not None and not 0 < port <= 65535:
        raise PluginError("PLUGIN_MCP_URL_INVALID", f"MCP {field} port 无效")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise PluginError("PLUGIN_MCP_URL_INVALID", "非 loopback MCP endpoint 必须使用 HTTPS")
    return url


def validate_http_headers(headers: object, *, field: str = "headers") -> dict[str, str]:
    """校验 header name/value，并拒绝大小写不敏感的重复名称。"""
    if not isinstance(headers, Mapping):
        raise PluginError("PLUGIN_MCP_HEADER_INVALID", f"MCP {field} 必须是字符串映射")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            raise PluginError("PLUGIN_MCP_HEADER_INVALID", f"MCP {field} header name 无效")
        lowered = name.casefold()
        if lowered in seen:
            raise PluginError(
                "PLUGIN_MCP_HEADER_INVALID",
                f"MCP {field} 存在大小写重复 header：{name}",
            )
        if (
            not isinstance(value, str)
            or _HEADER_CONTROL_RE.search(value)
            or _PLACEHOLDER_SYNTAX_RE.search(value)
        ):
            raise PluginError("PLUGIN_MCP_HEADER_INVALID", f"MCP {field} header value 无效")
        seen.add(lowered)
        result[name] = value
    return result


def _validate_stdio_server(
    name: str,
    raw: Mapping[str, object],
    *,
    root: Path | None = None,
) -> None:
    if "command" not in raw:
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 缺少 command")
    validate_stdio_command(raw["command"], root=root)
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name}.args 必须是字符串数组")
    env = raw.get("env", {})
    if not isinstance(env, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name}.env 必须是字符串映射")
    reserved = {"plugin_root", "plugin_data"}
    if any(key.casefold() in reserved for key in env):
        raise PluginError(
            "PLUGIN_MCP_INVALID",
            f"MCP server {name}.env 不得设置保留变量",
        )
    validate_stdio_cwd(raw.get("cwd"))


def _validate_http_server(name: str, raw: Mapping[str, object]) -> None:
    if "url" not in raw:
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 缺少 url")
    validate_http_url(raw["url"], field=f"{name}.url")
    validate_http_headers(raw.get("headers", {}), field=f"{name}.headers")


def _resolve_contained_path(value: str, boundary: Path, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise PluginError("PLUGIN_MCP_CWD_INVALID", f"MCP cwd 不在 {label} 内")
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(boundary.resolve())
    except (OSError, ValueError) as exc:
        raise PluginError("PLUGIN_MCP_CWD_INVALID", f"MCP cwd 不在 {label} 内") from exc
    return resolved


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _reject_parent_path(value: str) -> None:
    """拒绝 portable cwd 中任何可能穿越 root/data 边界的 parent segment。"""
    if Path(value.replace("\\", "/")).is_absolute() or ".." in Path(value.replace("\\", "/")).parts:
        raise PluginError("PLUGIN_MCP_CWD_INVALID", "stdio cwd 不能包含 parent path")
