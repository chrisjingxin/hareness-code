"""Qwen stdio LSP 的 Adapter/runtime 共用闭合校验。"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from harness_agent.plugins.common import read_json_object, safe_package_path
from harness_agent.plugins.model import PluginError


_FIELDS = frozenset(
    {
        "transport",
        "type",
        "command",
        "args",
        "cwd",
        "env",
        "timeout",
        "startupTimeout",
        "shutdownTimeout",
        "workspaceFolder",
        "extensionToLanguage",
        "initializationOptions",
        "settings",
    }
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_RE = re.compile(r"\$\{(?:extensionPath|workspacePath|/|pathSeparator)\}")
_ANY_TOKEN_RE = re.compile(r"\$\{[^}]+\}")
_UNSUPPORTED_ANGLE_TOKEN_RE = re.compile(
    r"<(?:extensionPath|workspacePath|pathSeparator)>"
)
_CONTROL_RE = re.compile(r"[\x00\r\n\t\f\v]")
_MAX_TEXT_BYTES = 16 * 1024
_MAX_COMMAND_BYTES = 4 * 1024
_MAX_ARGS = 64
_MAX_ENV = 64
_MAX_ENV_KEY_BYTES = 256
_MAX_OPTIONS_BYTES = 1024 * 1024
_MAX_TIMEOUT_SECONDS = 120.0
_RESERVED_ENV = frozenset(
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


@dataclass(frozen=True, slots=True)
class ValidatedQwenLspServer:
    """一个已通过静态字段校验、但尚未绑定 workspace 的 server。"""

    name: str
    value: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class QwenLspDocumentValidation:
    """Qwen LSP 文档的逐条结果；坏项不影响其他 server。"""

    servers: tuple[ValidatedQwenLspServer, ...]
    invalid: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


def validate_qwen_lsp_document(
    document: Mapping[str, object],
    *,
    root: Path,
    workspace: Path | None = None,
) -> QwenLspDocumentValidation:
    """校验 inline 或包内 JSON 的 Qwen stdio LSP，并隔离单项错误。"""
    if "lspServers" not in document:
        return QwenLspDocumentValidation(servers=())
    raw = document["lspServers"]
    merged: dict[str, object] = {}
    if isinstance(raw, Mapping):
        _merge_servers(merged, raw)
    elif isinstance(raw, str) or (
        isinstance(raw, list) and all(isinstance(item, str) for item in raw)
    ):
        paths = (raw,) if isinstance(raw, str) else tuple(raw)
        for relative in paths:
            child = read_json_object(root, relative)
            values = child.get("lspServers", child)
            if not isinstance(values, Mapping):
                raise PluginError(
                    "PLUGIN_LSP_DOCUMENT_INVALID",
                    "LSP JSON 的 lspServers 必须是 object",
                )
            _merge_servers(merged, values)
    else:
        raise PluginError(
            "PLUGIN_LSP_DOCUMENT_INVALID",
            "lspServers 必须是 object、JSON 路径或路径数组",
        )

    valid: list[ValidatedQwenLspServer] = []
    invalid: list[str] = []
    unsupported: list[str] = []
    for name, value in sorted(merged.items()):
        label = str(name)
        try:
            validated = validate_qwen_lsp_server(
                name,
                value,
                root=root,
                workspace=workspace,
            )
        except PluginError as exc:
            message = f"{label}: {exc.code}"
            if exc.code == "PLUGIN_LSP_TRANSPORT_UNSUPPORTED":
                unsupported.append(message)
            else:
                invalid.append(message)
            continue
        valid.append(validated)

    # Extension ownership is part of the canonical selection result.  The
    # first sorted name wins deterministically; a conflicting entry is not an
    # effective server and can never be selected later by loading order.
    owners: dict[str, str] = {}
    conflict_free: list[ValidatedQwenLspServer] = []
    for server in valid:
        conflicts = [
            extension
            for extension, _language in _extensions(server.value)
            if extension in owners
        ]
        if conflicts:
            invalid.append(
                f"{server.name}: PLUGIN_LSP_EXTENSION_CONFLICT: {','.join(conflicts)}"
            )
            continue
        conflict_free.append(server)
        for extension, _language in _extensions(server.value):
            owners[extension] = server.name
    return QwenLspDocumentValidation(
        servers=tuple(conflict_free),
        invalid=tuple(invalid),
        unsupported=tuple(unsupported),
    )


def validate_qwen_lsp_server(
    name: object,
    value: object,
    *,
    root: Path,
    workspace: Path | None = None,
) -> ValidatedQwenLspServer:
    """校验单个 Qwen LSP 的 closed fields、transport 和路径变量。"""
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise PluginError("PLUGIN_LSP_INVALID", "LSP server name 无效")
    if not isinstance(value, Mapping):
        raise PluginError("PLUGIN_LSP_INVALID", f"LSP server {name} 必须是 object")
    unknown = set(value) - _FIELDS
    if unknown:
        raise PluginError("PLUGIN_LSP_FIELD_INVALID", f"未知字段 {','.join(sorted(map(str, unknown)))}")

    transport = value.get("transport", value.get("type", "stdio"))
    if "transport" in value and "type" in value and value["transport"] != value["type"]:
        raise PluginError("PLUGIN_LSP_FIELD_INVALID", "transport 与 type 冲突")
    if not isinstance(transport, str):
        raise PluginError("PLUGIN_LSP_FIELD_INVALID", "transport 类型无效")
    if transport != "stdio":
        raise PluginError("PLUGIN_LSP_TRANSPORT_UNSUPPORTED", "只支持 stdio transport")

    command = value.get("command")
    if not isinstance(command, str) or not command.strip() or command != command.strip():
        raise PluginError("PLUGIN_LSP_COMMAND_INVALID", "command 无效")
    _validate_text(command, "command", _MAX_COMMAND_BYTES)
    if not _has_token(command) and _looks_like_path(command):
        raise PluginError("PLUGIN_LSP_PATH_INVALID", "command 不能使用宿主绝对路径")
    if _has_any_token(command):
        _validate_tokens(
            command,
            root=root,
            workspace=workspace,
            field="command",
            require_package_file=True,
        )

    args = value.get("args", [])
    if not isinstance(args, list) or len(args) > _MAX_ARGS:
        raise PluginError("PLUGIN_LSP_ARGS_INVALID", "args 超出限制")
    for index, item in enumerate(args):
        if not isinstance(item, str):
            raise PluginError("PLUGIN_LSP_ARGS_INVALID", f"args[{index}] 类型无效")
        _validate_text(item, f"args[{index}]", _MAX_TEXT_BYTES)
        if _looks_like_path(item) and not _has_token(item):
            raise PluginError("PLUGIN_LSP_PATH_INVALID", f"args[{index}] 不能使用宿主绝对路径")
        if _has_any_token(item):
            _validate_tokens(
                item,
                root=root,
                workspace=workspace,
                field=f"args[{index}]",
                require_package_file="${extensionPath}" in item,
            )

    cwd = value.get("cwd")
    if cwd is not None:
        _validate_path_field(
            cwd,
            root=root,
            workspace=workspace,
            field="cwd",
            require_directory=True,
        )

    workspace_folder = value.get("workspaceFolder", "${workspacePath}")
    _validate_path_field(
        workspace_folder,
        root=root,
        workspace=workspace,
        field="workspaceFolder",
        require_directory=True,
    )

    env = value.get("env", {})
    if not isinstance(env, Mapping) or len(env) > _MAX_ENV:
        raise PluginError("PLUGIN_LSP_ENV_INVALID", "env 超出限制")
    for key, item in env.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) > _MAX_ENV_KEY_BYTES
            or key.upper() in _RESERVED_ENV
        ):
            raise PluginError("PLUGIN_LSP_ENV_INVALID", "env key 无效")
        if not isinstance(item, str):
            raise PluginError("PLUGIN_LSP_ENV_INVALID", "env value 类型无效")
        _validate_text(item, f"env.{key}", _MAX_TEXT_BYTES)
        if _looks_like_path(item) and not _has_token(item):
            raise PluginError("PLUGIN_LSP_PATH_INVALID", f"env.{key} 不能使用宿主绝对路径")
        if _has_any_token(item):
            _validate_tokens(item, root=root, workspace=workspace, field=f"env.{key}")

    extensions = value.get("extensionToLanguage")
    if not isinstance(extensions, Mapping) or not extensions:
        raise PluginError("PLUGIN_LSP_EXTENSIONS_INVALID", "extensionToLanguage 必须是非空 object")
    for extension, language in extensions.items():
        if (
            not isinstance(extension, str)
            or not re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,63}", extension)
            or "/" in extension
            or "\\" in extension
            or not isinstance(language, str)
            or not language.strip()
            or len(language.encode("utf-8")) > 256
        ):
            raise PluginError("PLUGIN_LSP_EXTENSIONS_INVALID", "extensionToLanguage 条目无效")

    for field in ("initializationOptions", "settings"):
        options = value.get(field, {})
        if not isinstance(options, Mapping):
            raise PluginError("PLUGIN_LSP_OPTIONS_INVALID", f"{field} 必须是 object")
        try:
            if len(json.dumps(dict(options), ensure_ascii=False).encode("utf-8")) > _MAX_OPTIONS_BYTES:
                raise PluginError("PLUGIN_LSP_OPTIONS_TOO_LARGE", f"{field} 超出大小限制")
        except (TypeError, ValueError) as exc:
            raise PluginError("PLUGIN_LSP_OPTIONS_INVALID", f"{field} 不是 JSON object") from exc

    for field in ("timeout", "startupTimeout", "shutdownTimeout"):
        if field in value:
            timeout = value[field]
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 0 < float(timeout) <= _MAX_TIMEOUT_SECONDS * 1000
            ):
                raise PluginError("PLUGIN_LSP_TIMEOUT_INVALID", f"{field} 无效")
    return ValidatedQwenLspServer(name=name, value=dict(value))


def resolve_qwen_lsp_value(
    value: str,
    *,
    root: Path,
    workspace: Path,
    field: str,
    require_package_file: bool = False,
    require_directory: bool = False,
) -> str:
    """在 runtime 绑定 store/workspace 后只展开四类允许 token。"""
    _validate_text(value, field, _MAX_TEXT_BYTES)
    _validate_tokens(
        value,
        root=root,
        workspace=workspace,
        field=field,
        require_package_file=require_package_file,
        require_directory=require_directory,
    )
    replacements = {
        "${extensionPath}": str(root.resolve()),
        "${workspacePath}": str(workspace.resolve()),
        "${/}": os.sep,
        "${pathSeparator}": os.sep,
    }
    return re.sub(
        r"\$\{extensionPath\}|\$\{workspacePath\}|\$\{/\}|\$\{pathSeparator\}",
        lambda match: replacements[match.group(0)],
        value,
    )


def _merge_servers(target: dict[str, object], values: Mapping[str, object]) -> None:
    """合并命名 server，重复名称稳定拒绝。"""
    for name, value in values.items():
        if not isinstance(name, str) or not name or name in target:
            raise PluginError("PLUGIN_LSP_DUPLICATE", "LSP server name 重复或无效")
        target[name] = value


def _extensions(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw = value.get("extensionToLanguage")
    if not isinstance(raw, Mapping):
        return ()
    return tuple(
        (extension, language)
        for extension, language in raw.items()
        if isinstance(extension, str) and isinstance(language, str)
    )


def _validate_path_field(
    value: object,
    *,
    root: Path,
    workspace: Path | None,
    field: str,
    require_directory: bool,
) -> None:
    """限制 cwd/workspaceFolder 只能指向 package/workspace 的安全目录。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} 无效")
    _validate_text(value, field, _MAX_TEXT_BYTES)
    if _has_any_token(value) and not _has_token(value):
        _validate_tokens(
            value,
            root=root,
            workspace=workspace,
            field=field,
            require_directory=require_directory,
        )
    if not _has_token(value):
        raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} 必须使用受支持路径 token")
    _validate_tokens(
        value,
        root=root,
        workspace=workspace,
        field=field,
        require_directory=require_directory,
    )


def _validate_tokens(
    value: str,
    *,
    root: Path,
    workspace: Path | None,
    field: str,
    require_package_file: bool = False,
    require_directory: bool = False,
) -> None:
    """验证 token 语法、相对后缀、普通文件/目录和 workspace 边界。"""
    unknown = [
        match.group(0)
        for match in _ANY_TOKEN_RE.finditer(value)
        if _TOKEN_RE.fullmatch(match.group(0)) is None
    ]
    if unknown or _UNSUPPORTED_ANGLE_TOKEN_RE.search(value):
        raise PluginError("PLUGIN_LSP_PLACEHOLDER_INVALID", f"{field} 使用未知 placeholder")
    normalized = value.replace("\\", "/")
    if _looks_like_path(normalized):
        raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} 不能使用宿主绝对路径")
    if any(part == ".." for part in PurePosixPath(normalized).parts):
        raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} 不能包含 parent path")
    for match in _TOKEN_RE.finditer(value):
        token = match.group(0)
        if token in {"${/}", "${pathSeparator}"}:
            continue
        prefix = value[: match.start()]
        if prefix and prefix[-1] not in {
            "=", ":", " ", "\t", '"', "'", "`", "(", "[", "{", ","
        }:
            raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} token 不是独立路径片段")
        suffix_end = _token_suffix_end(value, match.end())
        suffix = value[match.end() : suffix_end]
        suffix = suffix.replace("${/}", "/").replace("${pathSeparator}", "/")
        if not suffix:
            continue
        relative = suffix.lstrip("/")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginError("PLUGIN_LSP_PATH_INVALID", f"{field} token 目标越过根目录")
        if token == "${extensionPath}":
            try:
                target = safe_package_path(root, relative, require_exists=True)
            except PluginError as exc:
                if exc.code in {"PLUGIN_COMPONENT_MISSING", "PLUGIN_COMPONENT_PATH_INVALID"}:
                    raise PluginError("PLUGIN_LSP_TARGET_MISSING", f"{field} 包内目标不存在") from exc
                raise
            if target.is_symlink() or (require_package_file and not target.is_file()):
                raise PluginError("PLUGIN_LSP_TARGET_MISSING", f"{field} 必须指向包内普通文件")
            if require_directory and (target.is_symlink() or not target.is_dir()):
                raise PluginError("PLUGIN_LSP_TARGET_MISSING", f"{field} 必须指向包内目录")
        elif workspace is not None:
            target = _safe_workspace_path(workspace, relative, field)
            if require_directory and (target.is_symlink() or not target.is_dir()):
                raise PluginError("PLUGIN_LSP_TARGET_MISSING", f"{field} workspace 目录不存在")


def _safe_workspace_path(workspace: Path, relative: str, field: str) -> Path:
    """解析 workspace token，拒绝越界和 symlink。"""
    candidate = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace.resolve())
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise PluginError("PLUGIN_LSP_TARGET_MISSING", f"{field} workspace 目标不存在") from exc
    return resolved


def _validate_text(value: str, field: str, limit: int) -> None:
    """限制字符串编码、控制字符与字节大小。"""
    if _CONTROL_RE.search(value) or "\x00" in value:
        raise PluginError("PLUGIN_LSP_FIELD_INVALID", f"{field} 含有控制字符")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PluginError("PLUGIN_LSP_FIELD_INVALID", f"{field} 编码无效") from exc
    if size > limit:
        raise PluginError("PLUGIN_LSP_FIELD_TOO_LARGE", f"{field} 超出大小限制")


def _has_token(value: str) -> bool:
    """判断字符串是否含受支持路径 token。"""
    return bool(_TOKEN_RE.search(value))


def _has_any_token(value: str) -> bool:
    """判断字符串是否含任意 ``${...}`` token。"""
    return bool(_ANY_TOKEN_RE.search(value))


def _looks_like_path(value: str) -> bool:
    """识别 Unix/Windows 宿主绝对路径或嵌入参数中的绝对路径。"""
    stripped = value.strip().strip('"\'')
    return bool(
        stripped.startswith("/")
        or re.search(r"(?:^|[=\s])/[A-Za-z0-9._-]+", stripped)
        or re.match(r"^[A-Za-z]:[\\/]", stripped) is not None
    )


def _token_suffix_end(value: str, start: int) -> int:
    """读取 token 后的一个路径片段，不把后续参数误认成目标。"""
    index = start
    while index < len(value):
        if value.startswith("${/}", index):
            index += len("${/}")
            continue
        if value.startswith("${pathSeparator}", index):
            index += len("${pathSeparator}")
            continue
        if value.startswith("${", index) or value[index].isspace() or value[index] in {
            '"', "'", "`", ";", ")", ","
        }:
            break
        index += 1
    return index
