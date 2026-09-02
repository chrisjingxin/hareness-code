"""Qwen/DevAgent Extension Adapter：格式校验、静态报告与受控 canonical 接入。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

import yaml

from harness_agent.config.settings import (
    SettingsError,
    parse_qwen_setting,
)
from harness_agent.plugins.common import (
    validate_command_hook_handler,
    validate_hook_matcher,
    VERSION_RE,
    list_regular_files,
    parse_qwen_markdown,
    read_json_object,
    relative_sources,
    safe_package_path,
    validate_skill_manifest_file,
)
from harness_agent.plugins.model import (
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
)
from harness_agent.plugins.mcp_schema import validate_qwen_mcp_document
from harness_agent.plugins.qwen_lsp import validate_qwen_lsp_document
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
    "lspServers",
    "monitors",
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
    components.extend(_command_component(root, manifest, manifest_name))
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
    components.extend(_mcp_component(root, manifest))
    components.extend(_lsp_component(root, manifest))
    components.extend(_unsupported_runtime_components(manifest))
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


def _command_component(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
) -> list[PluginComponentReport]:
    """逐个校验 Qwen Command；坏 Markdown 只隔离当前条目。"""
    paths = _component_paths(root, manifest, manifest_name, "commands")
    if not paths:
        return []
    unique_files, path_errors = _qwen_component_files(root, paths, suffixes=(".md",))
    valid: list[Path] = []
    errors: list[str] = list(path_errors)
    for path in unique_files:
        relative = path.relative_to(root).as_posix()
        try:
            parse_qwen_markdown(
                path,
                name_hint=_qwen_command_name(root, path),
                kind="command",
            )
        except ValueError as exc:
            errors.append(f"PLUGIN_COMPONENT_INVALID: {relative}: {exc}")
        else:
            valid.append(path)
    if valid:
        report = _effective_component(
            root,
            "commands",
            tuple(valid),
            "prompt:command",
            diagnostic="Qwen Markdown Command 已转换为 canonical SkillRegistry command",
        )
        if errors:
            report = PluginComponentReport(
                kind=report.kind,
                status=report.status,
                count=report.count,
                sources=report.sources,
                capabilities=report.capabilities,
                diagnostics=tuple(errors),
                effective=True,
            )
        return [report]
    return [
        PluginComponentReport(
            kind="commands",
            status="invalid",
            count=0,
            sources=(),
            capabilities=("prompt:command",),
            diagnostics=tuple(errors) or ("PLUGIN_COMPONENT_INVALID: commands 没有有效 Markdown",),
            effective=False,
        )
    ]


def _skill_component(
    root: Path,
    manifest: Mapping[str, object],
    manifest_name: str,
) -> list[PluginComponentReport]:
    """发现 Qwen skills 路径中的 SKILL.md，并复用既有 front matter 校验。"""
    paths = _component_paths(root, manifest, manifest_name, "skills")
    if not paths:
        return []
    manifests, errors = _qwen_skill_files(root, paths)
    valid_manifests: list[Path] = []
    for path in manifests:
        relative = path.relative_to(root).as_posix()
        error = validate_skill_manifest_file(
            root,
            path,
            require_name=False,
            expected_directory_name=(
                path.parent.name if path.parent != root else None
            ),
        )
        if error is not None:
            errors.append(f"{relative}: {error}")
            continue
        try:
            parse_qwen_markdown(
                path,
                name_hint=path.parent.name,
                kind="skill",
            )
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")
        else:
            valid_manifests.append(path)
    unique_manifests = tuple(sorted(set(valid_manifests)))
    if unique_manifests:
        report = _effective_component(
            root,
            "skills",
            unique_manifests,
            "prompt:skill",
            diagnostic="Qwen Markdown Skill 已转换为 canonical SkillRegistry Skill",
        )
    else:
        report = PluginComponentReport(
            kind="skills",
            status="invalid",
            count=0,
            sources=(),
            capabilities=("prompt:skill",),
            diagnostics=tuple(
                f"PLUGIN_COMPONENT_INVALID: {error}"
                for error in (errors or ["skills 没有有效 SKILL.md"])
            ),
            effective=False,
        )
        return [report]
    if errors:
        report = PluginComponentReport(
            kind=report.kind,
            status=report.status,
            count=report.count,
            sources=report.sources,
            capabilities=report.capabilities,
            diagnostics=tuple(
                [*report.diagnostics, *(
                    f"PLUGIN_COMPONENT_INVALID: {error}"
                    for error in errors
                )]
            ),
            effective=True,
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


def _mcp_component(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """校验 Qwen stdio MCP；只有 canonical adapter 可构造的条目才 effective。"""
    if "mcpServers" not in manifest:
        return []
    raw_servers = manifest["mcpServers"]
    if not isinstance(raw_servers, Mapping):
        raise PluginError(
            "PLUGIN_MANIFEST_FIELD_INVALID",
            "mcpServers 必须是 object",
            field="mcpServers",
        )
    # 安装阶段以 staging 根校验 extensionPath 目标；workspacePath 直到 Host
    # 运行快照才解析，但同一校验函数会在 adapter 侧再次执行。
    validation = validate_qwen_mcp_document(manifest, root=root)
    valid = len(validation.servers)
    diagnostics = (*validation.invalid, *validation.unsupported)
    if valid:
        return [
            PluginComponentReport(
                kind="mcp",
                status="adapted",
                count=valid,
                sources=("mcpServers",),
                capabilities=("process:mcp",),
                diagnostics=diagnostics
                or ("Qwen stdio MCP 已转换为 canonical McpServerConfig",),
                effective=True,
            )
        ]
    status = "invalid" if validation.invalid else "unsupported"
    return [
        PluginComponentReport(
            kind="mcp",
            status=status,
            count=0,
            sources=("mcpServers",),
            capabilities=(),
            diagnostics=diagnostics or (_STATIC_DIAGNOSTIC,),
            effective=False,
        )
    ]


def _unsupported_runtime_components(
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """显式报告当前仍没有 canonical consumer 的 Qwen Monitor。"""
    reports: list[PluginComponentReport] = []
    if "monitors" in manifest:
        reports.append(
            _unsupported_component(
                "monitors",
                "monitors",
                diagnostics=("Qwen Monitor 当前不支持，不会执行",),
            )
        )
    return reports


def _lsp_component(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """报告可由现有 PluginLspManager 构造的 Qwen stdio LSP 条目。"""
    if "lspServers" not in manifest and not (root / ".lsp.json").is_file():
        return []
    source_name = "lspServers"
    document = manifest
    if "lspServers" not in document:
        document = {**manifest, "lspServers": ".lsp.json"}
        source_name = ".lsp.json"
    validation = validate_qwen_lsp_document(document, root=root)
    diagnostics = (*validation.invalid, *validation.unsupported)
    if validation.servers:
        return [
            PluginComponentReport(
                kind="lsp",
                status="adapted",
                count=len(validation.servers),
                sources=(source_name,),
                capabilities=("process:lsp",),
                diagnostics=diagnostics
                or ("Qwen stdio LSP 已转换为 canonical PluginLspManager",),
                effective=True,
            )
        ]
    status = "invalid" if validation.invalid else "unsupported"
    return [
        PluginComponentReport(
            kind="lsp",
            status=status,
            count=0,
            sources=(source_name,),
            capabilities=(),
            diagnostics=diagnostics or (_STATIC_DIAGNOSTIC,),
            effective=False,
        )
    ]


def _hook_component(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """按事件逐项报告 Qwen Hook；执行仍由 canonical HookRunner 持有。"""
    if "hooks" not in manifest:
        return []
    raw_hooks = manifest["hooks"]
    if not isinstance(raw_hooks, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "hooks 必须是 object", field="hooks")
    supported_count = 0
    errors: list[str] = []
    unsupported: list[str] = []
    supported_events = {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStop",
    }
    for event, definitions in raw_hooks.items():
        if not isinstance(event, str) or not isinstance(definitions, list):
            errors.append(f"Hook event {event!r} 必须对应数组")
            continue
        if event not in supported_events:
            unsupported.append(f"PLUGIN_COMPONENT_UNSUPPORTED: Hook event {event}")
            continue
        for definition in definitions:
            if not isinstance(definition, Mapping):
                errors.append(f"Hook event {event} 条目必须是 object")
                continue
            matcher = definition.get("matcher", "*")
            matcher_error = validate_hook_matcher(matcher)
            if matcher_error is not None:
                errors.append(f"Hook event {event} {matcher_error}")
                continue
            nested = definition.get("hooks")
            if isinstance(nested, list):
                if not nested:
                    errors.append(f"Hook event {event} hooks 不能为空")
                for hook in nested:
                    valid, error = _valid_hook_handler(hook, event=event)
                    if valid:
                        supported_count += 1
                    else:
                        errors.append(f"Hook event {event} {error}")
            elif "hooks" in definition:
                errors.append(f"Hook event {event} hooks 必须是数组")
            else:
                valid, error = _valid_hook_handler(definition, event=event)
                if valid:
                    supported_count += 1
                else:
                    errors.append(f"Hook event {event} {error}")
    diagnostics = tuple(
        [f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors]
        + unsupported
    )
    if supported_count:
        return [
            PluginComponentReport(
                kind="hooks",
                status="adapted",
                count=supported_count,
                sources=("hooks",),
                capabilities=("process:hook",),
                diagnostics=diagnostics
                or ("Qwen Tool Hook 与 SubagentStop 已接入 canonical HookRunner",),
                effective=True,
            )
        ]
    if errors:
        return [
            PluginComponentReport(
                kind="hooks",
                status="invalid",
                count=0,
                sources=("hooks",),
                capabilities=(),
                diagnostics=diagnostics,
                effective=False,
            )
        ]
    if unsupported:
        return [
            PluginComponentReport(
                kind="hooks",
                status="unsupported",
                count=0,
                sources=("hooks",),
                capabilities=(),
                diagnostics=diagnostics or (_STATIC_DIAGNOSTIC,),
                effective=False,
            )
        ]
    return []


def _settings_component(manifest: Mapping[str, object]) -> list[PluginComponentReport]:
    """把 Qwen ExtensionSetting 逐项校验为可管理的 adapted 报告。"""
    if "settings" not in manifest:
        return []
    settings = manifest["settings"]
    if not isinstance(settings, list):
        return [
            PluginComponentReport(
                kind="settings",
                status="invalid",
                count=0,
                sources=("settings",),
                diagnostics=("SETTINGS_DECLARATION_INVALID: field=settings",),
                effective=False,
            )
        ]
    if not settings:
        return []
    valid_env_vars: list[str] = []
    diagnostics: list[str] = []
    for index, item in enumerate(settings):
        try:
            declaration = parse_qwen_setting(item)
        except SettingsError as exc:
            # index 是 manifest 内稳定位置，不把原文或任何潜在值放入诊断。
            diagnostics.append(f"{exc.code}: index={index}")
            continue
        valid_env_vars.append(declaration.env_var)
    if len(valid_env_vars) != len(set(valid_env_vars)):
        diagnostics.append("SETTINGS_DECLARATION_AMBIGUOUS: duplicate envVar")
    effective = bool(valid_env_vars) and not diagnostics
    return [
        PluginComponentReport(
            kind="settings",
            status="adapted" if valid_env_vars else "invalid",
            count=len(settings),
            sources=tuple(valid_env_vars),
            capabilities=(),
            diagnostics=tuple(diagnostics),
            effective=effective,
        )
    ]


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
    if manifest_name == "qwen-extension.json" or (
        manifest_name in QWEN_MANIFEST_NAMES and field in {"commands", "skills"}
    ):
        default_path = root / field
        if default_path.exists() or default_path.is_symlink():
            return (field,)
    return ()


def _qwen_command_name(root: Path, path: Path) -> str:
    """把 commands 下的相对 Markdown 路径映射为稳定的 ``:`` 名称。"""
    relative = path.relative_to(root).with_suffix("").as_posix()
    parts = relative.split("/")
    if "commands" in parts:
        parts = parts[parts.index("commands") + 1 :]
    return ":".join(part for part in parts if part)


def _qwen_component_files(
    root: Path,
    paths: tuple[str, ...],
    *,
    suffixes: tuple[str, ...],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """递归发现普通文件；单个越界或 symlink 只隔离当前来源。"""
    files: list[Path] = []
    diagnostics: list[str] = []

    def walk(path: Path, relative: str) -> None:
        if path.is_symlink():
            diagnostics.append(f"PLUGIN_SYMLINK_REJECTED: {relative}")
            return
        if path.is_file():
            if not suffixes or path.suffix.lower() in suffixes:
                files.append(path)
            return
        if not path.is_dir():
            diagnostics.append(f"PLUGIN_COMPONENT_PATH_INVALID: {relative}")
            return
        try:
            entries = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            diagnostics.append(f"PLUGIN_COMPONENT_READ_FAILED: {relative}")
            return
        for entry in entries:
            walk(entry, entry.relative_to(root).as_posix())

    for relative in paths:
        try:
            walk(safe_package_path(root, relative, require_exists=True), relative)
        except PluginError as exc:
            diagnostics.append(f"{relative}: {exc.code}")
    return tuple(sorted(set(files))), tuple(diagnostics)


def _qwen_skill_files(
    root: Path,
    paths: tuple[str, ...],
) -> tuple[list[Path], list[str]]:
    """发现 Qwen Skill.md，目录 symlink 不得跟随进入运行快照。"""
    manifests: list[Path] = []
    diagnostics: list[str] = []

    def walk(path: Path, relative: str) -> None:
        if path.is_symlink():
            diagnostics.append(f"PLUGIN_SYMLINK_REJECTED: {relative}")
            return
        if path.is_file():
            if path.name == "SKILL.md":
                manifests.append(path)
            else:
                diagnostics.append(f"{relative}: Skill 文件必须命名为 SKILL.md")
            return
        if not path.is_dir():
            diagnostics.append(f"PLUGIN_COMPONENT_PATH_INVALID: {relative}")
            return
        try:
            entries = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            diagnostics.append(f"PLUGIN_COMPONENT_READ_FAILED: {relative}")
            return
        found_child = False
        for entry in entries:
            if entry.is_symlink():
                diagnostics.append(
                    f"PLUGIN_SYMLINK_REJECTED: {entry.relative_to(root).as_posix()}"
                )
                continue
            if entry.is_dir():
                manifest = entry / "SKILL.md"
                if manifest.exists() or manifest.is_symlink():
                    found_child = True
                    walk(manifest, manifest.relative_to(root).as_posix())
                else:
                    walk(entry, entry.relative_to(root).as_posix())
            elif entry.is_file() and entry.name == "SKILL.md":
                found_child = True
                manifests.append(entry)
        if not found_child and path.name not in {"skills", "skill"}:
            diagnostics.append(f"{relative}: Skill 目录缺少 SKILL.md")

    for relative in paths:
        try:
            walk(safe_package_path(root, relative, require_exists=True), relative)
        except PluginError as exc:
            diagnostics.append(f"{relative}: {exc.code}")
    return manifests, diagnostics


def _nonempty_string(value: object) -> bool:
    """判断静态字段是否为可用的非空字符串。"""
    return isinstance(value, str) and bool(value.strip())


def _valid_hook_handler(value: object, *, event: str) -> tuple[bool, str]:
    """校验 Hook handler 最小形状，保持事件级 async 语义一致。"""
    error = validate_command_hook_handler(
        value,
        event=event,
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
