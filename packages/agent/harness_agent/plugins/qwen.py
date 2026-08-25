"""Qwen/DevAgent Extension Adapter：格式校验、静态报告与受控 canonical 接入。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

import yaml

from harness_agent.plugins.common import (
    validate_command_hook_handler,
    validate_hook_matcher,
    VERSION_RE,
    list_regular_files,
    read_json_object,
    relative_sources,
    safe_package_path,
    validate_skill_manifest_file,
    validate_skill_manifests,
)
from harness_agent.plugins.model import (
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
    capability_fingerprint,
)
from harness_agent.runtime.agent_catalog import AgentCatalogError, validate_qwen_agent_file


QWEN_MANIFEST_NAMES = ("qwen-extension.json", "devagent-extension.json")
_QWEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KNOWN_FIELDS = {
    "name",
    "version",
    "description",
    "contextFileName",
    "commands",
    "skills",
    "agents",
    "mcpServers",
    "hooks",
    "settings",
    "channels",
    "themes",
    "workflows",
}
_UNSUPPORTED_FIELDS = ("channels", "themes", "workflows")
_STATIC_DIAGNOSTIC = "Qwen/DevAgent 未接入运行时的组件仅生成静态报告，不会执行"


def qwen_manifest_paths(root: Path) -> tuple[Path, ...]:
    """返回根目录中存在的 Qwen 家族清单，不根据组件目录推断格式。"""
    return tuple(
        root / name
        for name in QWEN_MANIFEST_NAMES
        if (root / name).exists() or (root / name).is_symlink()
    )


def has_explicit_qwen_manifest(root: Path) -> bool:
    """判断根目录是否含有任一 Qwen/DevAgent 专属清单。"""
    return bool(qwen_manifest_paths(root))


def load_qwen_plugin(root: Path, *, package_digest: str) -> PluginDescriptor:
    """读取唯一 Qwen 家族清单并输出不接入运行时的静态组件报告。"""
    manifests = qwen_manifest_paths(root)
    if len(manifests) != 1:
        if len(manifests) > 1:
            raise PluginError(
                "PLUGIN_FORMAT_CONFLICT",
                "qwen-extension.json 与 devagent-extension.json 不能同时存在",
            )
        raise PluginError(
            "PLUGIN_FORMAT_MISMATCH",
            "来源没有唯一的 Qwen/DevAgent Extension 清单",
        )

    manifest_path = manifests[0]
    manifest_name = manifest_path.name
    manifest = read_json_object(root, manifest_name)
    name = _require_qwen_name(manifest.get("name"))
    version = _optional_qwen_version(manifest)
    description = _optional_qwen_string(manifest, "description")

    components: list[PluginComponentReport] = []
    components.extend(
        _path_component(
            root,
            manifest,
            manifest_name,
            "commands",
            (".md",),
            "prompt:command",
        )
    )
    components.extend(_skill_component(root, manifest, manifest_name))
    components.extend(
        _path_component(
            root,
            manifest,
            manifest_name,
            "agents",
            (".md", ".yaml", ".yml", ".json"),
            "delegation:agent",
        )
    )
    components.extend(_context_component(root, manifest, manifest_name))
    components.extend(_mcp_component(manifest))
    components.extend(_hook_component(root, manifest))
    components.extend(_settings_component(manifest))

    diagnostics: list[str] = []
    unsupported_fields: list[str] = []
    for field in _UNSUPPORTED_FIELDS:
        if field in manifest:
            unsupported_fields.append(field)
            components.append(_unsupported_component(field, manifest_name))
    unknown_fields = sorted(set(manifest) - _KNOWN_FIELDS)
    if unknown_fields:
        unsupported_fields.extend(unknown_fields)
        diagnostics.extend(
            f"{manifest_name}: 字段 {field} 当前不支持，不会执行"
            for field in unknown_fields
        )
        components.append(
            _unsupported_component(
                "unsupported",
                manifest_name,
                count=len(unknown_fields),
                diagnostics=tuple(
                    f"{manifest_name}: 字段 {field} 当前不支持，不会执行"
                    for field in unknown_fields
                ),
            )
        )
    if unsupported_fields:
        diagnostics.append(
            f"{manifest_name}: 非首版字段已报告 unsupported，不会静默执行"
        )

    components_tuple = tuple(sorted(components, key=lambda item: item.kind))
    return PluginDescriptor(
        name=name,
        version=version,
        description=description,
        format="qwen-code",
        manifest=manifest_name,
        package_digest=package_digest,
        capability_fingerprint=capability_fingerprint(components_tuple),
        components=components_tuple,
        diagnostics=tuple(diagnostics),
    )


def _require_qwen_name(value: object) -> str:
    """校验允许 Qwen 生态大小写、句点和下划线的 Plugin 身份。"""
    if not isinstance(value, str) or not _QWEN_NAME_RE.fullmatch(value):
        raise PluginError(
            "PLUGIN_NAME_INVALID",
            "Qwen/DevAgent Plugin name 必须是安全的非空身份字符串",
            field="name",
        )
    return value


def _optional_qwen_version(manifest: Mapping[str, object]) -> str | None:
    """校验 Qwen version，保持 descriptor 只保存字符串身份。"""
    if "version" not in manifest:
        return None
    value = manifest["version"]
    if not isinstance(value, str) or not value.strip() or not VERSION_RE.fullmatch(value):
        raise PluginError("PLUGIN_VERSION_INVALID", "Qwen/DevAgent version 格式无效", field="version")
    return value


def _optional_qwen_string(manifest: Mapping[str, object], field: str) -> str | None:
    """校验可选的 Qwen 字符串 metadata。"""
    if field not in manifest:
        return None
    value = manifest[field]
    if not isinstance(value, str) or not value.strip():
        raise PluginError(
            "PLUGIN_MANIFEST_FIELD_INVALID",
            f"{field} 必须是非空字符串",
            field=field,
        )
    return value.strip()


def _path_component(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
    field: str,
    suffixes: tuple[str, ...],
    capability: str,
) -> list[PluginComponentReport]:
    """发现一个包内路径组件；越界/缺失路径直接稳定失败。"""
    paths = _component_paths(root, manifest, manifest_name, field)
    if not paths:
        return []
    files: list[Path] = []
    for relative in paths:
        files.extend(list_regular_files(safe_package_path(root, relative, require_exists=True), suffixes=suffixes))
    unique_files = tuple(sorted(set(files)))
    if field == "agents" and unique_files:
        errors: list[str] = []
        for path in unique_files:
            try:
                validate_qwen_agent_file(path)
            except (AgentCatalogError, OSError, yaml.YAMLError) as exc:
                errors.append(f"{path.name}: {exc}")
        if errors:
            return [
                PluginComponentReport(
                    kind="agents",
                    status="invalid",
                    count=0,
                    sources=(),
                    capabilities=(capability,),
                    diagnostics=tuple(
                        f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors
                    ),
                    effective=False,
                )
            ]
        return [_effective_component(root, field, unique_files, capability)]
    return [_static_component(root, field, unique_files, capability)]


def _skill_component(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
) -> list[PluginComponentReport]:
    """发现 Qwen skills 路径中的 SKILL.md，并复用既有 front matter 校验。"""
    paths = _component_paths(root, manifest, manifest_name, "skills")
    if not paths:
        return []
    manifests: list[Path] = []
    errors: list[str] = []
    for relative in paths:
        path = safe_package_path(root, relative, require_exists=True)
        if path.is_file():
            if path.name != "SKILL.md":
                errors.append(f"{relative}: Skill 文件必须命名为 SKILL.md")
            else:
                error = validate_skill_manifest_file(
                    root,
                    path,
                    require_name=False,
                    expected_directory_name=(
                        path.parent.name if path.parent != root else None
                    ),
                )
                if error is None:
                    manifests.append(path)
                else:
                    errors.append(f"{relative}: {error}")
            continue
        found, found_errors = validate_skill_manifests(root, path, require_name=False)
        manifests.extend(found)
        errors.extend(found_errors)
    report = _static_component(root, "skills", tuple(sorted(set(manifests))), "prompt:skill")
    if errors:
        report = PluginComponentReport(
            kind=report.kind,
            status="invalid",
            count=report.count,
            sources=report.sources,
            capabilities=report.capabilities,
            diagnostics=tuple(f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors),
            effective=False,
        )
    return [report]


def _context_component(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
) -> list[PluginComponentReport]:
    """按 Qwen 默认语义或显式路径校验 Context，只报告不注入。"""
    paths = _context_paths(root, manifest, manifest_name)
    if not paths:
        return []
    files: list[Path] = []
    for relative in paths:
        path = safe_package_path(root, relative, require_exists=False)
        if not path.exists():
            raise PluginError(
                "PLUGIN_COMPONENT_MISSING",
                f"contextFileName 指向的文件不存在：{relative}",
                field="contextFileName",
            )
        path = safe_package_path(root, relative, require_exists=True)
        if path.is_symlink() or not path.is_file():
            raise PluginError(
                "PLUGIN_COMPONENT_PATH_INVALID",
                "contextFileName 必须指向包内普通文件",
                field="contextFileName",
            )
        files.append(path)
    unique_files = tuple(dict.fromkeys(files))
    return [
        _effective_component(
            root,
            "contexts",
            unique_files,
            "context:plugin",
            diagnostic="Qwen Context 已转换为 canonical ContextLifecycle 参考块",
        )
    ]


def _context_paths(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
) -> tuple[str, ...]:
    """规范化 Qwen Context 路径，区分默认文件和显式路径的缺失语义。"""
    if "contextFileName" not in manifest:
        return _qwen_default_context_paths(root, manifest_name)

    value = manifest["contextFileName"]
    if manifest_name != "qwen-extension.json":
        if not isinstance(value, str) or not value.strip():
            raise PluginError(
                "PLUGIN_MANIFEST_FIELD_INVALID",
                "DevAgent contextFileName 必须是非空字符串",
                field="contextFileName",
            )
        return (value,)

    if isinstance(value, str):
        if not value.strip():
            raise PluginError(
                "PLUGIN_MANIFEST_FIELD_INVALID",
                "contextFileName 必须是非空字符串或字符串数组",
                field="contextFileName",
            )
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if not value:
            return _qwen_default_context_paths(root, manifest_name)
        if any(not item.strip() for item in value):
            raise PluginError(
                "PLUGIN_MANIFEST_FIELD_INVALID",
                "contextFileName 数组不能包含空路径",
                field="contextFileName",
            )
        return tuple(value)
    raise PluginError(
        "PLUGIN_MANIFEST_FIELD_INVALID",
        "contextFileName 必须是字符串或字符串数组",
        field="contextFileName",
    )


def _qwen_default_context_paths(root: Path, manifest_name: str) -> tuple[str, ...]:
    """返回标准 Qwen 缺省 QWEN.md；DevAgent 不进行目录推断。"""
    if manifest_name != "qwen-extension.json":
        return ()
    default = root / "QWEN.md"
    if default.exists() or default.is_symlink():
        return ("QWEN.md",)
    return ()


def _mcp_component(manifest: Mapping[str, object]) -> list[PluginComponentReport]:
    """静态检查 Qwen mcpServers 条目，不读取路径、不构造运行配置。"""
    if "mcpServers" not in manifest:
        return []
    raw_servers = manifest["mcpServers"]
    if not isinstance(raw_servers, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "mcpServers 必须是 object", field="mcpServers")
    valid = 0
    transports: set[str] = set()
    errors: list[str] = []
    for server_name, raw_server in raw_servers.items():
        if not isinstance(server_name, str) or not server_name.strip():
            errors.append("MCP server name 必须是非空字符串")
            continue
        if not isinstance(raw_server, Mapping):
            errors.append(f"MCP server {server_name} 必须是 object")
            continue
        server_transports: set[str] = set()
        server_errors: list[str] = []
        if "command" in raw_server:
            if _nonempty_string(raw_server["command"]):
                server_transports.add("stdio")
            else:
                server_errors.append(f"MCP server {server_name} command 必须是非空字符串")
        for field in ("url", "httpUrl"):
            if field in raw_server:
                if _nonempty_string(raw_server[field]):
                    server_transports.add("network")
                else:
                    server_errors.append(
                        f"MCP server {server_name} {field} 必须是非空字符串"
                    )
        if "type" in raw_server and not _nonempty_string(raw_server["type"]):
            server_errors.append(f"MCP server {server_name} type 必须是非空字符串")
        if not server_transports:
            server_errors.append(f"MCP server {server_name} 缺少有效静态传输字段")
        if server_errors:
            errors.extend(server_errors)
            continue
        valid += 1
        transports.update(server_transports)
    capabilities = tuple(
        capability
        for transport, capability in (
            ("stdio", "process:mcp"),
            ("network", "network:mcp"),
        )
        if transport in transports
    )
    status = "invalid" if errors else "unsupported"
    diagnostics = tuple(f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors)
    if not diagnostics:
        diagnostics = (_STATIC_DIAGNOSTIC,)
    return [
        PluginComponentReport(
            kind="mcp",
            status=status,
            count=valid,
            sources=("mcpServers",),
            capabilities=capabilities,
            diagnostics=diagnostics,
            effective=False,
        )
    ]


def _hook_component(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """校验并报告 Qwen SubagentStop；执行仍由 canonical HookRunner 持有。"""
    if "hooks" not in manifest:
        return []
    raw_hooks = manifest["hooks"]
    if not isinstance(raw_hooks, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "hooks 必须是 object", field="hooks")
    count = 0
    errors: list[str] = []
    for event, definitions in raw_hooks.items():
        if not isinstance(event, str) or not isinstance(definitions, list):
            errors.append(f"Hook event {event!r} 必须对应数组")
            continue
        if event != "SubagentStop":
            errors.append(f"Hook event {event} 当前阶段不支持")
            continue
        for definition in definitions:
            if not isinstance(definition, Mapping):
                errors.append(f"Hook event {event} 条目必须是 object")
                continue
            matcher = definition.get("matcher", "*")
            matcher_error = validate_hook_matcher(matcher)
            if matcher_error is not None:
                errors.append(f"Hook event {event} {matcher_error}")
            nested = definition.get("hooks")
            if isinstance(nested, list):
                if not nested:
                    errors.append(f"Hook event {event} hooks 不能为空")
                for hook in nested:
                    valid, error = _valid_hook_handler(hook)
                    if valid:
                        count += 1
                    else:
                        errors.append(f"Hook event {event} {error}")
            elif "hooks" in definition:
                errors.append(f"Hook event {event} hooks 必须是数组")
            else:
                valid, error = _valid_hook_handler(definition)
                if valid:
                    count += 1
                else:
                    errors.append(f"Hook event {event} {error}")
    if errors:
        return [
            PluginComponentReport(
                kind="hooks",
                status="invalid",
                count=count,
                sources=("hooks",),
                capabilities=("process:hook",) if count else (),
                diagnostics=tuple(
                    f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors
                ),
                effective=False,
            )
        ]
    if count == 0:
        return [
            PluginComponentReport(
                kind="hooks",
                status="unsupported",
                count=0,
                sources=("hooks",),
                capabilities=(),
                diagnostics=(_STATIC_DIAGNOSTIC,),
                effective=False,
            )
        ]
    return [
        PluginComponentReport(
            kind="hooks",
            status="adapted",
            count=count,
            sources=("hooks",),
            capabilities=("process:hook",),
            diagnostics=("Qwen SubagentStop 已接入 canonical HookRunner 与 child gate",),
            effective=True,
        )
    ]


def _settings_component(manifest: Mapping[str, object]) -> list[PluginComponentReport]:
    """允许空 settings；非空配置进入明确 unsupported 报告。"""
    if "settings" not in manifest:
        return []
    settings = manifest["settings"]
    if not isinstance(settings, list):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "settings 必须是数组", field="settings")
    if not settings:
        return []
    return [_unsupported_component("settings", "settings", count=1)]


def _path_values(value: object, field: str) -> tuple[str, ...]:
    """把 Qwen string 或 string[] 路径字段规范化。"""
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是路径或路径数组", field=field)
    if not values or any(not item.strip() for item in values):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 不能为空", field=field)
    return values


def _component_paths(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
    field: str,
) -> tuple[str, ...]:
    """解析显式路径；标准 Qwen 清单缺省时使用已有默认目录。"""
    if field in manifest:
        return _path_values(manifest[field], field)
    if manifest_name == "qwen-extension.json":
        default_path = root / field
        if default_path.exists() or default_path.is_symlink():
            return (field,)
    return ()


def _nonempty_string(value: object) -> bool:
    """判断静态字段是否为可用的非空字符串。"""
    return isinstance(value, str) and bool(value.strip())


def _valid_hook_handler(value: object) -> tuple[bool, str]:
    """校验不会执行的 Hook handler 最小形状，拒绝空对象假阳性。"""
    error = validate_command_hook_handler(
        value,
        event="SubagentStop",
        qwen=True,
    )
    return error is None, error or ""


def _static_component(
    root: Path,
    kind: str,
    files: tuple[Path, ...],
    capability: str,
) -> PluginComponentReport:
    """创建第一阶段不进入运行时的静态组件报告。"""
    return PluginComponentReport(
        kind=kind,
        status="unsupported",
        count=len(files),
        sources=relative_sources(root, files),
        capabilities=(capability,),
        diagnostics=(_STATIC_DIAGNOSTIC,),
        effective=False,
    )


def _effective_component(
    root: Path,
    kind: str,
    files: tuple[Path, ...],
    capability: str,
    *,
    diagnostic: str = "Qwen Agent Markdown 已转换为 canonical AgentCatalog",
) -> PluginComponentReport:
    """将已完成 Qwen 静态校验且已接入 canonical 链路的组件标为 effective。"""
    return PluginComponentReport(
        kind=kind,
        status="adapted",
        count=len(files),
        sources=relative_sources(root, files),
        capabilities=(capability,),
        diagnostics=(diagnostic,),
        effective=True,
    )


def _unsupported_component(
    kind: str,
    source: str,
    *,
    count: int = 1,
    diagnostics: tuple[str, ...] = (),
) -> PluginComponentReport:
    """为首版不执行的清单字段生成显式报告。"""
    return PluginComponentReport(
        kind=kind,
        status="unsupported",
        count=count,
        sources=(source,),
        capabilities=("config:unsupported",),
        diagnostics=diagnostics or (f"{source}: 当前版本 unsupported，不会执行",),
        effective=False,
    )
