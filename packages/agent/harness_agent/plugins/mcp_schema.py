"""Agent Plugins 1.0 MCP schema、安全路径和字面配置校验。"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping
from urllib.parse import urlsplit

from harness_agent.extensions.mcp import DEFAULT_CONNECT_TIMEOUT_SECONDS
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
_QWEN_MCP_FIELDS = frozenset({"type", "command", "args", "cwd", "env", "timeout"})
_QWEN_MCP_PLACEHOLDER_RE = re.compile(
    r"\$\{(?:extensionPath|workspacePath|/|pathSeparator)\}"
)
_QWEN_MCP_ANY_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")
_QWEN_MCP_UNSUPPORTED_ANGLE_TOKEN_RE = re.compile(
    r"<(?:extensionPath|workspacePath|pathSeparator)>"
)
_QWEN_MCP_MAX_COMMAND_BYTES = 4 * 1024
_QWEN_MCP_MAX_ARG_COUNT = 64
_QWEN_MCP_MAX_ARG_BYTES = 16 * 1024
_QWEN_MCP_MAX_ENV_COUNT = 64
_QWEN_MCP_MAX_ENV_KEY_BYTES = 256
_QWEN_MCP_MAX_ENV_VALUE_BYTES = 16 * 1024
_QWEN_MCP_MAX_TIMEOUT_SECONDS = 120
_QWEN_MCP_RESERVED_ENV_KEYS = frozenset(
    {
        "PATH",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)

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


def validate_qwen_mcp_document(
    document: Mapping[str, object],
    *,
    root: Path,
    workspace: Path | None = None,
) -> McpDocumentValidation:
    """逐 server 校验 Qwen stdio MCP，并隔离不支持或损坏条目。"""
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", "mcpServers 必须是 object")
    valid: list[ValidatedMcpServer] = []
    invalid: list[str] = []
    unsupported: list[str] = []
    for name, raw in raw_servers.items():
        label = name if isinstance(name, str) else repr(name)
        try:
            valid.append(
                validate_qwen_mcp_server(
                    name,
                    raw,
                    root=root,
                    workspace=workspace,
                )
            )
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


def validate_qwen_mcp_server(
    name: object,
    raw: object,
    *,
    root: Path,
    workspace: Path | None = None,
) -> ValidatedMcpServer:
    """校验 Phase 2 支持的 Qwen stdio server 字段和路径边界。"""
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", name
    ):
        raise PluginError("PLUGIN_MCP_INVALID", "MCP server name 必须是安全字符串")
    if not isinstance(raw, Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 必须是 object")
    unknown = set(raw) - _QWEN_MCP_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name} 包含未知字段：{fields}")

    transport = raw.get("type", "stdio")
    if transport != "stdio":
        raise PluginError(
            "PLUGIN_MCP_TRANSPORT_UNSUPPORTED",
            f"Qwen MCP server {name} 只支持 stdio transport",
        )

    command = raw.get("command")
    if not isinstance(command, str) or not command or command != command.strip():
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.command 无效")
    _validate_qwen_text(command, f"{name}.command", _QWEN_MCP_MAX_COMMAND_BYTES)
    if not _has_qwen_placeholder(command) and (
        any(char.isspace() for char in command)
        or "/" in command
        or "\\" in command
        or command in {".", ".."}
    ):
        raise PluginError(
            "PLUGIN_MCP_PATH_INVALID",
            f"MCP server {name}.command 必须是 bare executable 或安全包内路径",
        )
    _validate_qwen_path_tokens(
        command,
        root=root,
        workspace=workspace,
        field=f"{name}.command",
        require_package_file=True,
        require_executable=True,
    )

    args = raw.get("args", [])
    if not isinstance(args, list) or len(args) > _QWEN_MCP_MAX_ARG_COUNT:
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.args 超出限制")
    for index, value in enumerate(args):
        if not isinstance(value, str):
            raise PluginError(
                "PLUGIN_MCP_FIELD_INVALID",
                f"MCP server {name}.args[{index}] 必须是字符串",
            )
        _validate_qwen_text(value, f"{name}.args[{index}]", _QWEN_MCP_MAX_ARG_BYTES)
        _validate_qwen_path_tokens(
            value,
            root=root,
            workspace=workspace,
            field=f"{name}.args[{index}]",
            require_package_file=True,
        )

    env = raw.get("env", {})
    if not isinstance(env, Mapping) or len(env) > _QWEN_MCP_MAX_ENV_COUNT:
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.env 超出限制")
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) > _QWEN_MCP_MAX_ENV_KEY_BYTES
            or key.upper() in _QWEN_MCP_RESERVED_ENV_KEYS
        ):
            raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.env key 无效")
        if not isinstance(value, str):
            raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.env value 无效")
        _validate_qwen_text(value, f"{name}.env.{key}", _QWEN_MCP_MAX_ENV_VALUE_BYTES)
        _validate_qwen_path_tokens(
            value,
            root=root,
            workspace=workspace,
            field=f"{name}.env.{key}",
        )

    cwd = raw.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd:
            raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP server {name}.cwd 无效")
        _validate_qwen_text(cwd, f"{name}.cwd", _QWEN_MCP_MAX_ARG_BYTES)
        _validate_qwen_path_tokens(
            cwd,
            root=root,
            workspace=workspace,
            field=f"{name}.cwd",
            require_directory=True,
        )
        if not _has_qwen_placeholder(cwd):
            try:
                cwd_path = safe_package_path(root, cwd, require_exists=True)
            except PluginError as exc:
                raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP server {name}.cwd 目录不存在") from exc
            if not cwd_path.is_dir() or cwd_path.is_symlink():
                raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP server {name}.cwd 必须是包内目录")

    timeout = raw.get("timeout", DEFAULT_CONNECT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= _QWEN_MCP_MAX_TIMEOUT_SECONDS
    ):
        raise PluginError("PLUGIN_MCP_TIMEOUT_INVALID", f"MCP server {name}.timeout 无效")
    return ValidatedMcpServer(name=name, transport="stdio")


def resolve_qwen_mcp_value(
    value: str,
    *,
    root: Path,
    workspace: Path,
    field: str,
    require_package_file: bool = False,
    require_directory: bool = False,
    require_executable: bool = False,
) -> str:
    """一次性展开 Qwen 四类路径 token，并复核运行时边界。"""
    _validate_qwen_path_tokens(
        value,
        root=root,
        workspace=workspace,
        field=field,
        require_package_file=require_package_file,
        require_directory=require_directory,
        require_executable=require_executable,
    )
    return _replace_qwen_tokens(value, root=root, workspace=workspace)


def _validate_qwen_text(value: str, field: str, max_bytes: int) -> None:
    """限制 Qwen MCP 文本的编码、控制字符和字节大小。"""
    if "\x00" in value or _COMMAND_CONTROL_RE.search(value):
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP {field} 含有控制字符")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PluginError("PLUGIN_MCP_FIELD_INVALID", f"MCP {field} 编码无效") from exc
    if size > max_bytes:
        raise PluginError("PLUGIN_MCP_FIELD_TOO_LARGE", f"MCP {field} 超过大小限制")


def _has_qwen_placeholder(value: str) -> bool:
    """判断值是否含有 Qwen 运行时保留 token。"""
    return bool(_QWEN_MCP_PLACEHOLDER_RE.search(value))


def _validate_qwen_path_tokens(
    value: str,
    *,
    root: Path,
    workspace: Path | None,
    field: str,
    require_package_file: bool = False,
    require_directory: bool = False,
    require_executable: bool = False,
) -> None:
    """验证 token 只能作为安全路径片段，且 extensionPath 目标存在。"""
    unknown = [
        match.group(0)
        for match in _QWEN_MCP_ANY_PLACEHOLDER_RE.finditer(value)
        if not _QWEN_MCP_PLACEHOLDER_RE.fullmatch(match.group(0))
    ]
    if unknown:
        raise PluginError(
            "PLUGIN_MCP_PLACEHOLDER_INVALID",
            f"MCP {field} 使用未知 placeholder：{unknown[0]}",
        )
    if _QWEN_MCP_UNSUPPORTED_ANGLE_TOKEN_RE.search(value):
        raise PluginError(
            "PLUGIN_MCP_PLACEHOLDER_INVALID",
            f"MCP {field} 使用未支持的 placeholder",
        )
    normalized = value.replace("\\", "/")
    if _qwen_looks_like_host_path(normalized):
        raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP {field} 不能使用宿主绝对路径")
    if any(part == ".." for part in PurePosixPath(normalized).parts):
        raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP {field} 不能包含 parent path")

    for match in _QWEN_MCP_PLACEHOLDER_RE.finditer(value):
        token = match.group(0)
        if token in {"${/}", "${pathSeparator}"}:
            continue
        prefix = value[: match.start()]
        if prefix and prefix[-1] not in {
            "=", ":", " ", "\t", "\n", "\r", '"', "'", "`", "(", "[", "{"
        }:
            raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP {field} token 不是独立路径片段")
        suffix_end = _qwen_placeholder_suffix_end(value, match.end())
        suffix = value[match.end() : suffix_end]
        suffix = suffix.replace("${/}", "/").replace("${pathSeparator}", "/")
        if not suffix:
            continue
        relative = suffix.lstrip("/")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP {field} token 目标越过根目录")
        if token == "${extensionPath}":
            try:
                target = safe_package_path(root, relative, require_exists=True)
            except PluginError as exc:
                if exc.code in {"PLUGIN_COMPONENT_MISSING", "PLUGIN_COMPONENT_PATH_INVALID"}:
                    raise PluginError(
                        "PLUGIN_MCP_TARGET_MISSING",
                        f"MCP {field} token 目标不存在",
                    ) from exc
                raise
            if require_directory:
                if not target.is_dir() or target.is_symlink():
                    raise PluginError("PLUGIN_MCP_PATH_INVALID", f"MCP {field} 必须指向包内目录")
            elif require_package_file and (not target.is_file() or target.is_symlink()):
                raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} 必须指向包内普通文件")
            elif require_executable and (not target.is_file() or not os.access(target, os.X_OK)):
                raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} 必须指向可执行文件")
            elif not target.is_file() and not target.is_dir():
                raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} token 目标无效")
        else:
            if workspace is None:
                continue
            target = _safe_workspace_path(workspace, relative, field)
            if require_directory:
                if not target.is_dir() or target.is_symlink():
                    raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} workspace 目录不存在")
            elif require_package_file and (not target.is_file() or target.is_symlink()):
                raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} workspace 文件不存在")


def _replace_qwen_tokens(value: str, *, root: Path, workspace: Path) -> str:
    """替换 Qwen token，不解释 shell、环境变量或其他 placeholder。"""
    replacements = {
        "${extensionPath}": str(root.resolve()),
        "${workspacePath}": str(workspace.resolve()),
        "${/}": os.sep,
        "${pathSeparator}": os.sep,
    }
    return re.sub(
        r"\$\{(?:extensionPath|workspacePath|/|pathSeparator)\}",
        lambda match: replacements[match.group(0)],
        value,
    )


def _qwen_placeholder_suffix_end(value: str, start: int) -> int:
    """读取 token 后的单一路径片段，避免把后续参数当成资源路径。"""
    index = start
    while index < len(value):
        if value.startswith("${/}", index) or value.startswith("${pathSeparator}", index):
            index += len("${/}") if value.startswith("${/}", index) else len("${pathSeparator}")
            continue
        if value.startswith("${", index) or value[index].isspace() or value[index] in {'"', "'", "`", ";", ")", ","}:
            break
        index += 1
    return index


def _qwen_looks_like_host_path(value: str) -> bool:
    """识别 `/tmp`、`--file=/tmp` 和 Windows drive 形式的宿主路径。"""
    stripped = value.strip().strip('"\'')
    return bool(
        stripped.startswith("/")
        or re.search(r"(?:^|[=\s])/[A-Za-z0-9._-]+", stripped)
        or re.match(r"^[A-Za-z]:[\\/]", stripped) is not None
    )


def _safe_workspace_path(workspace: Path, relative: str, field: str) -> Path:
    """将 workspacePath 后缀限制在当前 workspace，且不跟随越界链接。"""
    candidate = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace.resolve())
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise PluginError("PLUGIN_MCP_TARGET_MISSING", f"MCP {field} workspace 目标不存在") from exc
    return resolved


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
