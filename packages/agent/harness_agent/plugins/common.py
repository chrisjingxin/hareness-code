"""Plugin Adapter 共用的受限 JSON、路径和组件发现工具。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from harness_agent.plugins.model import PluginError


MAX_MANIFEST_BYTES = 256 * 1024
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PORTABLE_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<header>.*?)\r?\n---(?:\r?\n|\Z)(?P<body>.*)\Z",
    re.DOTALL,
)

# Qwen Markdown Command/Skill 使用同一份有界文本解析；Adapter 与
# SkillRegistry 都经过这里，避免安装报告和 Run snapshot 各自解释 YAML。
QWEN_MARKDOWN_MAX_BYTES = 64 * 1024
QWEN_COMMAND_ARGUMENT_MAX_BYTES = 16 * 1024
QWEN_COMMAND_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-:][a-z0-9]+)*$")
QWEN_UNSUPPORTED_EXPANSION_RE = re.compile(r"(?:!\{|@\{)")

# Hook 的结构限制属于 Adapter 与 canonical runtime 的共同安全契约；放在
# 共用纯校验 seam，避免安装报告与运行时各自维护一套容易漂移的边界。
HOOK_MAX_MATCHER_LENGTH = 512
HOOK_MAX_COMMAND_LENGTH = 32_768
HOOK_MAX_TIMEOUT_SECONDS = 600
HOOK_DEFAULT_TIMEOUT_SECONDS = 60
QWEN_HOOK_DEFAULT_TIMEOUT_MILLISECONDS = 60_000
QWEN_HOOK_MAX_TIMEOUT_MILLISECONDS = HOOK_MAX_TIMEOUT_SECONDS * 1000
HOOK_SUPPORTED_SHELLS = frozenset({"bash", "powershell"})


def read_json_object(root: Path, relative: str) -> dict[str, Any]:
    """读取包内受限 JSON object，拒绝链接、目录和超限文件。"""
    path = safe_package_path(root, relative, require_exists=True)
    if path.is_symlink() or not path.is_file():
        raise PluginError("PLUGIN_COMPONENT_PATH_INVALID", f"{relative} 必须是普通文件")
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise PluginError("PLUGIN_MANIFEST_TOO_LARGE", f"{relative} 超过大小上限")
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PluginError("PLUGIN_JSON_ENCODING_INVALID", f"{relative} 必须使用 UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise PluginError("PLUGIN_JSON_INVALID", f"{relative} 不是有效 JSON") from exc
    except OSError as exc:
        raise PluginError("PLUGIN_READ_FAILED", f"无法读取 {relative}") from exc
    if not isinstance(value, dict):
        raise PluginError("PLUGIN_JSON_ROOT_INVALID", f"{relative} 根节点必须是 object")
    return value


def safe_package_path(root: Path, relative: str, *, require_exists: bool = False) -> Path:
    """把 manifest 相对路径限制在 Plugin 根目录内。"""
    if not isinstance(relative, str) or not relative.strip():
        raise PluginError("PLUGIN_COMPONENT_PATH_INVALID", "组件路径必须是非空字符串")
    normalized = relative.strip().replace("\\", "/")
    candidate_path = Path(normalized)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise PluginError("PLUGIN_COMPONENT_PATH_INVALID", "组件路径不能离开 Plugin 根目录")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized == ".":
        return root
    candidate = root.joinpath(*Path(normalized).parts)
    current = root
    for part in Path(normalized).parts:
        current = current / part
        if current.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 组件路径不能经过符号链接")
    try:
        candidate.resolve(strict=require_exists).relative_to(root.resolve())
    except (FileNotFoundError, ValueError) as exc:
        raise PluginError("PLUGIN_COMPONENT_PATH_INVALID", "组件路径不能离开 Plugin 根目录") from exc
    if require_exists and not candidate.exists():
        raise PluginError("PLUGIN_COMPONENT_MISSING", f"组件路径不存在：{normalized}")
    return candidate


def normalize_component_paths(value: object, field: str) -> tuple[str, ...]:
    """规范化 Claude manifest 的 string 或 string[] 路径字段。"""
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是路径或路径数组", field=field)
    if not values:
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 不能为空", field=field)
    return values


def list_regular_files(path: Path, *, suffixes: tuple[str, ...] = ()) -> tuple[Path, ...]:
    """递归列出普通文件；staging 已拒绝链接，这里再次保持 fail-closed。"""
    if path.is_symlink() or not path.exists():
        return ()
    if path.is_file():
        if suffixes and path.suffix.lower() not in suffixes:
            return ()
        return (path,)
    if not path.is_dir():
        return ()
    result: list[Path] = []
    for entry in sorted(path.rglob("*")):
        if entry.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 组件不能包含符号链接")
        if entry.is_file() and (not suffixes or entry.suffix.lower() in suffixes):
            result.append(entry)
    return tuple(result)


def relative_sources(root: Path, paths: Iterable[Path]) -> tuple[str, ...]:
    """把组件来源转换成 POSIX 相对路径。"""
    return tuple(sorted(path.relative_to(root).as_posix() for path in paths))


def validate_skill_manifests(
    root: Path,
    skills_root: Path,
    *,
    require_name: bool,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """检查 Skill 目录结构、front matter 和正文，不解释工具权限字段。"""
    if skills_root.is_symlink() or not skills_root.is_dir():
        return (), ("Skill 组件必须是目录",)
    manifests: list[Path] = []
    diagnostics: list[str] = []
    for directory in sorted(entry for entry in skills_root.iterdir() if entry.is_dir()):
        manifest = directory / "SKILL.md"
        if not manifest.is_file() or manifest.is_symlink():
            if manifest.exists() or manifest.is_symlink():
                diagnostics.append(
                    f"{manifest.relative_to(root).as_posix()}: SKILL.md 必须是普通文件"
                )
            continue
        error = validate_skill_manifest_file(
            root,
            manifest,
            require_name=require_name,
            expected_directory_name=directory.name,
        )
        if error is None:
            manifests.append(manifest)
        else:
            label = manifest.relative_to(root).as_posix()
            diagnostics.append(f"{label}: {error}")
    return tuple(manifests), tuple(diagnostics)


def validate_skill_manifest_file(
    root: Path,
    manifest: Path,
    *,
    require_name: bool,
    expected_directory_name: str | None = None,
) -> str | None:
    """校验单个 Skill 文件，供目录扫描和显式文件路径共用。"""
    try:
        content = manifest.read_text(encoding="utf-8")
        if len(content.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("SKILL.md 超过大小上限")
        match = _FRONTMATTER_RE.match(content)
        if match is None:
            raise ValueError("缺少 YAML front matter")
        header = yaml.safe_load(match.group("header"))
        if not isinstance(header, Mapping):
            raise ValueError("front matter 必须是 object")
        name = header.get("name")
        if require_name and (not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name)):
            raise ValueError("name 必须是 kebab-case")
        if name is not None and (
            not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name)
        ):
            raise ValueError("name 必须是 kebab-case")
        if expected_directory_name is not None and isinstance(name, str):
            if name != expected_directory_name:
                raise ValueError("name 必须与 Skill 目录名一致")
        description = header.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description 必须是非空字符串")
        if not match.group("body").strip():
            raise ValueError("正文不能为空")
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        return str(exc)
    return None


def parse_qwen_markdown(
    path: Path,
    *,
    name_hint: str | None = None,
    kind: str,
) -> dict[str, object]:
    """解析 Qwen Markdown 的安全 front matter 与正文，不执行任何展开。"""
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("QWEN_MARKDOWN_PATH_INVALID")
        raw = path.read_bytes()
        if len(raw) > QWEN_MARKDOWN_MAX_BYTES:
            raise ValueError("QWEN_MARKDOWN_TOO_LARGE")
        content = raw.decode("utf-8")
        match = _FRONTMATTER_RE.match(content)
        if match is None:
            raise ValueError("QWEN_MARKDOWN_FRONTMATTER_INVALID")
        header = yaml.safe_load(match.group("header"))
        if not isinstance(header, Mapping):
            raise ValueError("QWEN_MARKDOWN_FRONTMATTER_INVALID")
        allowed = {
            "name",
            "description",
            "version",
            "argument_hint",
            "argument-hint",
            "user_invocable",
            "user-invocable",
            "disable-model-invocation",
            "allowed-tools",
        }
        unknown = set(header) - allowed
        if unknown:
            raise ValueError(
                "QWEN_MARKDOWN_FRONTMATTER_FIELD_UNSUPPORTED:"
                + ",".join(sorted(str(item) for item in unknown))
            )
        declared_name = header.get("name")
        name = declared_name if declared_name is not None else name_hint
        if not isinstance(name, str):
            raise ValueError("QWEN_MARKDOWN_NAME_INVALID")
        name_pattern = QWEN_COMMAND_NAME_RE if kind == "command" else PLUGIN_NAME_RE
        if not name_pattern.fullmatch(name):
            raise ValueError("QWEN_MARKDOWN_NAME_INVALID")
        if name_hint is not None and name != name_hint:
            # Qwen 的 front matter 通常只写文件名，而 Registry 对嵌套目录
            # 使用完整 ``parent:child`` stable name；两者相同时才绑定到
            # Adapter 传入的不可变来源。
            if not (
                kind == "command"
                and declared_name == name_hint.rsplit(":", 1)[-1]
            ):
                raise ValueError("QWEN_MARKDOWN_NAME_MISMATCH")
            name = name_hint
        description = header.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("QWEN_MARKDOWN_DESCRIPTION_INVALID")
        version = header.get("version")
        if version is not None and (not isinstance(version, str) or not version.strip()):
            raise ValueError("QWEN_MARKDOWN_VERSION_INVALID")
        if "argument_hint" in header and "argument-hint" in header:
            if header["argument_hint"] != header["argument-hint"]:
                raise ValueError("QWEN_MARKDOWN_ARGUMENT_HINT_CONFLICT")
        argument_hint = header.get("argument_hint", header.get("argument-hint"))
        if argument_hint is not None and not isinstance(argument_hint, str):
            raise ValueError("QWEN_MARKDOWN_ARGUMENT_HINT_INVALID")
        if argument_hint is not None and len(argument_hint.encode("utf-8")) > 512:
            raise ValueError("QWEN_MARKDOWN_ARGUMENT_HINT_TOO_LARGE")
        body = match.group("body").strip()
        if not body:
            raise ValueError("QWEN_MARKDOWN_BODY_EMPTY")
        if QWEN_UNSUPPORTED_EXPANSION_RE.search(body):
            raise ValueError("QWEN_MARKDOWN_EXPANSION_UNSUPPORTED")
        disabled_model = header.get("disable-model-invocation", False)
        if not isinstance(disabled_model, bool):
            raise ValueError("QWEN_MARKDOWN_MODEL_INVOCATION_INVALID")
        user_invocable = header.get("user_invocable", header.get("user-invocable", True))
        if not isinstance(user_invocable, bool):
            raise ValueError("QWEN_MARKDOWN_USER_INVOCATION_INVALID")
        if kind == "command":
            if user_invocable is not True:
                raise ValueError("QWEN_COMMAND_USER_INVOCATION_REQUIRED")
        allowed_tools = header.get("allowed-tools", ())
        if isinstance(allowed_tools, str):
            requested_tools = tuple(
                item for item in re.split(r"[\s,]+", allowed_tools.strip()) if item
            )
        elif isinstance(allowed_tools, list) and all(
            isinstance(item, str) and item.strip() for item in allowed_tools
        ):
            requested_tools = tuple(item.strip() for item in allowed_tools)
        elif allowed_tools in (None, ()):
            requested_tools = ()
        else:
            raise ValueError("QWEN_MARKDOWN_ALLOWED_TOOLS_INVALID")
        if kind == "skill":
            read_only_tools = frozenset({"read_file", "list_directory"})
            unsupported_tools = sorted(set(requested_tools) - read_only_tools)
            if unsupported_tools:
                raise ValueError(
                    "QWEN_SKILL_TOOLS_UNSUPPORTED:" + ",".join(unsupported_tools)
                )
        elif requested_tools:
            raise ValueError("QWEN_COMMAND_TOOLS_UNSUPPORTED")
        return {
            "name": name,
            "description": " ".join(description.split()),
            "version": version.strip() if isinstance(version, str) else None,
            "argument_hint": argument_hint,
            "requested_tools": requested_tools,
            "user_invocable": user_invocable,
            "model_invocable": not disabled_model,
            "body": body,
            "header": dict(header),
        }
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("QWEN_"):
            raise
        raise ValueError("QWEN_MARKDOWN_INVALID") from exc


def require_plugin_name(value: object, *, field: str = "name") -> str:
    """校验统一的 kebab-case Plugin 名称。"""
    if not isinstance(value, str) or not PLUGIN_NAME_RE.fullmatch(value):
        raise PluginError("PLUGIN_NAME_INVALID", "Plugin name 必须是 kebab-case", field=field)
    return value


def require_portable_plugin_name(value: object, *, field: str = "name") -> str:
    """校验 Agent Plugins 规范的 portable Plugin 名称。"""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or "--" in value
        or ".." in value
        or not PORTABLE_PLUGIN_NAME_RE.fullmatch(value)
    ):
        raise PluginError(
            "PLUGIN_NAME_INVALID",
            "Plugin name 必须是 1-64 个字符、以字母数字开头结尾且不含连续连字符或句点",
            field=field,
        )
    return value


def optional_string(value: object, field: str) -> str | None:
    """校验可选非空字符串字段。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是非空字符串", field=field)
    return value.strip()


def validate_hook_matcher(value: object) -> str | None:
    """校验 Hook matcher，使静态报告与 runtime 使用同一正则边界。"""
    if not isinstance(value, str) or len(value) > HOOK_MAX_MATCHER_LENGTH:
        return "PLUGIN_HOOK_MATCHER_INVALID"
    if value == "*":
        return None
    try:
        re.compile(value)
    except re.error:
        return "PLUGIN_HOOK_MATCHER_INVALID"
    return None


def normalize_hook_timeout_seconds(value: object, *, qwen: bool) -> float | None:
    """把格式原生 timeout 转成 canonical 秒；Qwen 输入单位是毫秒。"""
    maximum = QWEN_HOOK_MAX_TIMEOUT_MILLISECONDS if qwen else HOOK_MAX_TIMEOUT_SECONDS
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        return None
    return float(value) / 1000 if qwen else float(value)


def validate_command_hook_handler(
    value: object,
    *,
    event: str,
    qwen: bool,
) -> str | None:
    """校验 command Hook 的可构造形状；Qwen 未支持字段明确 fail-closed。"""
    if not isinstance(value, Mapping):
        return "PLUGIN_HOOK_HANDLER_INVALID"
    if value.get("type") != "command":
        return "PLUGIN_HOOK_TYPE_UNSUPPORTED"
    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        return "PLUGIN_HOOK_COMMAND_INVALID"
    if len(command) > HOOK_MAX_COMMAND_LENGTH:
        return "PLUGIN_HOOK_COMMAND_INVALID"

    if qwen and "args" in value:
        # Qwen Extension schema 本阶段没有 args 的受控执行语义；不能沿用
        # Claude 的 shell/exec 形态并把字段静默丢掉。
        return "PLUGIN_HOOK_ARGS_UNSUPPORTED"
    args = value.get("args")
    if not qwen and args is not None and (
        not isinstance(args, list) or not all(isinstance(item, str) for item in args)
    ):
        return "PLUGIN_HOOK_ARGS_INVALID"

    if qwen and "env" in value:
        return "PLUGIN_HOOK_ENV_UNSUPPORTED"

    timeout = value.get(
        "timeout",
        QWEN_HOOK_DEFAULT_TIMEOUT_MILLISECONDS
        if qwen
        else HOOK_DEFAULT_TIMEOUT_SECONDS,
    )
    if normalize_hook_timeout_seconds(timeout, qwen=qwen) is None:
        return "PLUGIN_HOOK_TIMEOUT_INVALID"

    asynchronous = value.get("async", False)
    if not isinstance(asynchronous, bool):
        return "PLUGIN_HOOK_ASYNC_INVALID"
    if qwen and event == "SubagentStop" and asynchronous:
        return "PLUGIN_HOOK_SUBAGENT_STOP_ASYNC_UNSUPPORTED"
    if event == "PreToolUse" and asynchronous:
        return "PLUGIN_HOOK_PRE_ASYNC_UNSUPPORTED"

    shell = value.get("shell")
    if shell is not None and (
        not isinstance(shell, str) or shell not in HOOK_SUPPORTED_SHELLS
    ):
        return "PLUGIN_HOOK_SHELL_INVALID"
    if shell == "powershell" and os.name != "nt":
        return "PLUGIN_HOOK_SHELL_UNAVAILABLE"
    return None
