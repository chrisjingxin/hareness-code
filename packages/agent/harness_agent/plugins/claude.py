"""Claude Code Plugin Adapter；跟踪当前组件面并显式报告未实现能力。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from harness_agent.plugins.common import (
    VERSION_RE,
    list_regular_files,
    normalize_component_paths,
    optional_string,
    read_json_object,
    relative_sources,
    require_plugin_name,
    safe_package_path,
    validate_skill_manifests,
)
from harness_agent.plugins.model import (
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
    capability_fingerprint,
)


CLAUDE_MANIFEST = ".claude-plugin/plugin.json"
_KNOWN_FIELDS = {
    "$schema",
    "name",
    "displayName",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "defaultEnabled",
    "skills",
    "commands",
    "agents",
    "workflows",
    "hooks",
    "mcpServers",
    "outputStyles",
    "lspServers",
    "experimental",
    "themes",
    "monitors",
    "userConfig",
    "channels",
    "dependencies",
}
_PATH_COMPONENTS = {
    "commands": ("commands", (".md",), ("prompt:command",)),
    "agents": ("agents", (".md",), ("delegation:agent",)),
    "workflows": ("workflows", (), ("process:workflow",)),
    "outputStyles": ("output-styles", (".md",), ("prompt:output-style",)),
}
_DEFAULT_PATHS = {
    "commands": "commands",
    "agents": "agents",
    "workflows": "workflows",
    "outputStyles": "output-styles",
}


def has_explicit_claude_manifest(root: Path) -> bool:
    """判断是否存在 Claude manifest，不在此阶段吞掉损坏 JSON。"""
    path = root / CLAUDE_MANIFEST
    return path.exists() or path.is_symlink()


def has_unambiguous_claude_components(root: Path) -> bool:
    """manifestless auto 只接受 Claude 私有默认组件，单独 skills/ 保持格式不歧义。"""
    candidates = (
        "commands",
        "agents",
        "workflows",
        "hooks",
        ".mcp.json",
        ".lsp.json",
        "output-styles",
        "themes",
        "monitors",
        "bin",
        "settings.json",
        "SKILL.md",
    )
    return any((root / relative).exists() or (root / relative).is_symlink() for relative in candidates)


def load_claude_plugin(
    root: Path,
    *,
    package_digest: str,
    name_hint: str,
    include_portable_components: bool = True,
) -> PluginDescriptor:
    """解析当前 Claude manifest/default layout，并生成逐组件兼容报告。"""
    manifest: Mapping[str, object] = {}
    manifest_path: str | None = None
    diagnostics: list[str] = []
    if has_explicit_claude_manifest(root):
        manifest = read_json_object(root, CLAUDE_MANIFEST)
        manifest_path = CLAUDE_MANIFEST
        name = require_plugin_name(manifest.get("name"))
        diagnostics.extend(
            f"{CLAUDE_MANIFEST}: 未识别的顶层字段 {field}"
            for field in sorted(set(manifest) - _KNOWN_FIELDS)
        )
        _validate_manifest_types(manifest)
    else:
        name = _name_from_hint(name_hint)
        diagnostics.append("Claude manifestless Plugin：名称由安装来源目录推导")
    version = optional_string(manifest.get("version"), "version")
    if version is not None and not VERSION_RE.fullmatch(version):
        raise PluginError("PLUGIN_VERSION_INVALID", "Plugin version 格式无效", field="version")
    description = optional_string(manifest.get("description"), "description")

    components: list[PluginComponentReport] = []
    if include_portable_components:
        components.extend(_skills_reports(root, manifest))
        components.extend(_mcp_reports(root, manifest))
    components.extend(_path_component_reports(root, manifest))
    components.extend(_structured_component_reports(root, manifest))
    components.extend(_default_file_reports(root, manifest))

    components_tuple = tuple(sorted(_merge_reports(components), key=lambda item: item.kind))
    return PluginDescriptor(
        name=name,
        version=version,
        description=description,
        format="claude-code",
        manifest=manifest_path,
        package_digest=package_digest,
        capability_fingerprint=capability_fingerprint(components_tuple),
        components=components_tuple,
        diagnostics=tuple(diagnostics),
    )


def _validate_manifest_types(manifest: Mapping[str, object]) -> None:
    """校验 Claude 已知 metadata 类型；未知字段只报告警告。"""
    for field in ("displayName", "homepage", "repository", "license"):
        optional_string(manifest.get(field), field)
    author = manifest.get("author")
    if author is not None and not isinstance(author, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "author 必须是 object", field="author")
    keywords = manifest.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords)
    ):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "keywords 必须是字符串数组", field="keywords")
    default_enabled = manifest.get("defaultEnabled")
    if default_enabled is not None and not isinstance(default_enabled, bool):
        raise PluginError(
            "PLUGIN_MANIFEST_FIELD_INVALID",
            "defaultEnabled 必须是 boolean",
            field="defaultEnabled",
        )
    experimental = manifest.get("experimental")
    if experimental is not None and not isinstance(experimental, Mapping):
        raise PluginError(
            "PLUGIN_MANIFEST_FIELD_INVALID",
            "experimental 必须是 object",
            field="experimental",
        )
    for field in ("userConfig",):
        value = manifest.get(field)
        if value is not None and not isinstance(value, Mapping):
            raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是 object", field=field)
    for field in ("channels", "dependencies"):
        value = manifest.get(field)
        if value is not None and not isinstance(value, list):
            raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是 array", field=field)


def _skills_reports(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """发现 Claude 默认和自定义 Skill 目录，允许 front matter 省略 name。"""
    paths = ["skills"]
    if "skills" in manifest:
        paths.extend(normalize_component_paths(manifest["skills"], "skills"))
    root_skill = root / "SKILL.md"
    reports: list[PluginComponentReport] = []
    manifests: list[Path] = []
    errors: list[str] = []
    for relative in _dedupe(paths):
        try:
            path = safe_package_path(root, relative)
            if not path.exists():
                if relative != "skills":
                    errors.append(f"{relative}: 路径不存在")
                continue
            found, found_errors = validate_skill_manifests(root, path, require_name=False)
            manifests.extend(found)
            errors.extend(found_errors)
        except PluginError as exc:
            errors.append(f"{relative}: {exc.code}: {exc}")
    if root_skill.is_file() and not root_skill.is_symlink():
        # 根单 Skill 不是子目录布局，做最小正文检查后计入。
        try:
            content = root_skill.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("正文不能为空")
            manifests.append(root_skill)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"SKILL.md: {exc}")
    if manifests or errors:
        reports.append(
            PluginComponentReport(
                kind="skills",
                status="supported" if manifests else "invalid",
                count=len(set(manifests)),
                sources=relative_sources(root, set(manifests)),
                capabilities=("prompt:skill",),
                diagnostics=tuple(errors) or ("Claude Skill 已适配到 Harness 启动期 Skill 快照",),
                effective=bool(manifests),
            )
        )
    return reports


def _mcp_reports(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """发现 Claude `.mcp.json`、manifest 路径或 inline MCP 配置。"""
    reports: list[PluginComponentReport] = []
    sources: list[str] = []
    count = 0
    errors: list[str] = []
    raw = manifest.get("mcpServers")
    if raw is not None:
        if isinstance(raw, Mapping):
            count += len(raw)
            sources.append("inline:mcpServers")
        else:
            try:
                for relative in normalize_component_paths(raw, "mcpServers"):
                    count += _count_json_mapping(root, relative, sources)
            except PluginError as exc:
                errors.append(f"{exc.code}: {exc}")
    default = root / ".mcp.json"
    if default.exists() or default.is_symlink():
        try:
            count += _count_json_mapping(root, ".mcp.json", sources)
        except PluginError as exc:
            errors.append(f"{exc.code}: {exc}")
    if count or errors:
        reports.append(
            PluginComponentReport(
                kind="mcp",
                status="invalid" if errors else "supported",
                count=count,
                sources=tuple(sorted(set(sources))),
                capabilities=("process:mcp", "network:mcp"),
                diagnostics=tuple(errors) or ("Claude MCP 已适配到 Harness 启动期 MCP 快照",),
                effective=not errors,
            )
        )
    return reports


def _path_component_reports(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """发现 commands、agents、workflows 和 output styles。"""
    reports: list[PluginComponentReport] = []
    for field, (kind, suffixes, capabilities) in _PATH_COMPONENTS.items():
        values: tuple[str, ...]
        if field in manifest:
            values = normalize_component_paths(manifest[field], field)
        else:
            values = (_DEFAULT_PATHS[field],)
        files: list[Path] = []
        errors: list[str] = []
        for relative in values:
            try:
                path = safe_package_path(root, relative)
                if not path.exists():
                    if field in manifest:
                        errors.append(f"{relative}: 路径不存在")
                    continue
                files.extend(list_regular_files(path, suffixes=suffixes))
            except PluginError as exc:
                errors.append(f"{relative}: {exc.code}: {exc}")
        if files or errors:
            reports.append(
                PluginComponentReport(
                    kind=kind,
                    status=(
                        "invalid"
                        if errors and not files
                        else "supported"
                        if kind in {"commands", "agents"}
                        else "unsupported"
                    ),
                    count=len(set(files)),
                    sources=relative_sources(root, set(files)),
                    capabilities=capabilities,
                    diagnostics=tuple(errors) or (
                        ("Claude Command/Agent 已接入 Harness 启动快照",)
                        if kind in {"commands", "agents"}
                        else ("Claude 组件已识别，但 Harness 当前尚未执行",)
                    ),
                    effective=kind in {"commands", "agents"} and bool(files),
                )
            )
    return reports


def _structured_component_reports(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """发现 Hook、LSP、theme、monitor、userConfig、channel 和 dependency。"""
    reports: list[PluginComponentReport] = []
    reports.extend(_json_or_inline_report(root, manifest, "hooks", "hooks", "hooks/hooks.json", ("process:hook",)))
    reports.extend(_json_or_inline_report(root, manifest, "lspServers", "lsp", ".lsp.json", ("process:lsp",)))
    experimental = manifest.get("experimental")
    experimental_map = experimental if isinstance(experimental, Mapping) else {}
    theme_value = experimental_map.get("themes", manifest.get("themes"))
    monitor_value = experimental_map.get("monitors", manifest.get("monitors"))
    reports.extend(_optional_paths_report(root, "themes", theme_value, "themes", (".json",), ("ui:theme",)))
    reports.extend(
        _optional_paths_report(
            root,
            "monitors",
            monitor_value,
            "monitors/monitors.json",
            (".json",),
            ("process:monitor",),
        )
    )
    for field, kind, capability in (
        ("userConfig", "user-config", "config:user"),
        ("channels", "channels", "network:channel"),
        ("dependencies", "dependencies", "plugin:dependency"),
    ):
        value = manifest.get(field)
        if value:
            count = len(value) if isinstance(value, (list, Mapping)) else 1
            reports.append(
                PluginComponentReport(
                    kind=kind,
                    status="unsupported",
                    count=count,
                    sources=(f"inline:{field}",),
                    capabilities=(capability,),
                    diagnostics=("Claude 组件已识别，但 Harness 当前尚未执行",),
                )
            )
    return reports


def _default_file_reports(
    root: Path,
    manifest: Mapping[str, object],
) -> list[PluginComponentReport]:
    """发现 Claude 最新默认 bin 与 settings 文件。"""
    reports: list[PluginComponentReport] = []
    bin_root = root / "bin"
    if bin_root.exists() or bin_root.is_symlink():
        files = list_regular_files(bin_root)
        reports.append(
            PluginComponentReport(
                kind="bin",
                status="unsupported",
                count=len(files),
                sources=relative_sources(root, files),
                capabilities=("process:path",),
                diagnostics=("不会把 Plugin bin 自动加入 Shell PATH",),
            )
        )
    settings = root / "settings.json"
    if settings.exists() or settings.is_symlink():
        try:
            read_json_object(root, "settings.json")
            reports.append(
                PluginComponentReport(
                    kind="settings",
                    status="unsupported",
                    count=1,
                    sources=("settings.json",),
                    capabilities=("config:default",),
                    diagnostics=("Claude settings 已识别，但不会修改 Harness 配置",),
                )
            )
        except PluginError as exc:
            reports.append(
                PluginComponentReport(
                    kind="settings",
                    status="invalid",
                    count=0,
                    sources=("settings.json",),
                    diagnostics=(f"{exc.code}: {exc}",),
                )
            )
    return reports


def _json_or_inline_report(
    root: Path,
    manifest: Mapping[str, object],
    field: str,
    kind: str,
    default_path: str,
    capabilities: tuple[str, ...],
) -> list[PluginComponentReport]:
    """处理可为路径、路径数组或 inline object 的 Claude 组件。"""
    raw = manifest.get(field)
    sources: list[str] = []
    documents: list[Mapping[str, object]] = []
    errors: list[str] = []
    if isinstance(raw, Mapping):
        documents.append(raw)
        sources.append(f"inline:{field}")
    elif raw is not None:
        try:
            for relative in normalize_component_paths(raw, field):
                documents.append(read_json_object(root, relative))
                sources.append(relative.replace("\\", "/"))
        except PluginError as exc:
            errors.append(f"{exc.code}: {exc}")
    default = root / default_path
    if raw is None and (default.exists() or default.is_symlink()):
        try:
            documents.append(read_json_object(root, default_path))
            sources.append(default_path)
        except PluginError as exc:
            errors.append(f"{exc.code}: {exc}")
    if not documents and not errors:
        return []
    if kind == "hooks":
        count, unsupported, component_errors = _inspect_hook_documents(documents)
    elif kind == "lsp":
        count, unsupported, component_errors = _inspect_lsp_documents(documents)
    else:
        count, unsupported, component_errors = len(documents), 0, []
    errors.extend(component_errors)
    status = (
        "invalid"
        if errors and not count and not unsupported
        else "unsupported"
        if unsupported and not count
        else "adapted"
        if count
        else "invalid"
    )
    diagnostics = [
        *errors,
        *(
            [f"{unsupported} 个 Claude {kind} 子项尚未支持"]
            if unsupported
            else []
        ),
    ]
    if count:
        diagnostics.append(
            "已接入 Harness Plugin Runtime；仅明确支持的 "
            "command Hook / stdio LSP 会生效"
        )
    return [
        PluginComponentReport(
            kind=kind,
            status=status,
            count=count,
            sources=tuple(sorted(set(sources))),
            capabilities=capabilities,
            diagnostics=tuple(diagnostics),
            effective=count > 0,
        )
    ]


def _optional_paths_report(
    root: Path,
    kind: str,
    raw: object,
    default_path: str,
    suffixes: tuple[str, ...],
    capabilities: tuple[str, ...],
) -> list[PluginComponentReport]:
    """处理 experimental theme/monitor 的显式或默认路径。"""
    explicit = raw is not None
    values = normalize_component_paths(raw, kind) if explicit else (default_path,)
    files: list[Path] = []
    errors: list[str] = []
    for relative in values:
        try:
            path = safe_package_path(root, relative)
            if not path.exists():
                if explicit:
                    errors.append(f"{relative}: 路径不存在")
                continue
            files.extend(list_regular_files(path, suffixes=suffixes))
        except PluginError as exc:
            errors.append(f"{relative}: {exc.code}: {exc}")
    if not files and not errors:
        return []
    count = len(set(files))
    unsupported = 0
    if kind == "monitors" and files:
        count, unsupported, monitor_errors = _inspect_monitor_files(files)
        errors.extend(monitor_errors)
    status = (
        "invalid"
        if errors and not count and not unsupported
        else "unsupported"
        if unsupported and not count
        else "adapted"
        if kind == "monitors" and count
        else "unsupported"
    )
    diagnostics = list(errors)
    if unsupported:
        diagnostics.append(f"{unsupported} 个 Claude monitor 子项尚未支持")
    if kind == "monitors" and count:
        diagnostics.append("已接入 Harness Plugin Runtime 的有界后台 Monitor")
    elif not diagnostics:
        diagnostics.append("Claude 组件已识别，但 Harness 当前尚未执行")
    return [
        PluginComponentReport(
            kind=kind,
            status=status,
            count=count,
            sources=relative_sources(root, set(files)),
            capabilities=capabilities,
            diagnostics=tuple(diagnostics),
            effective=kind == "monitors" and count > 0,
        )
    ]


def _inspect_hook_documents(
    documents: list[Mapping[str, object]],
) -> tuple[int, int, list[str]]:
    """统计可执行 command Hook，并区分 unsupported handler 与损坏配置。"""
    supported = 0
    unsupported = 0
    errors: list[str] = []
    for document in documents:
        events = document.get("hooks", document)
        if not isinstance(events, Mapping):
            errors.append("PLUGIN_HOOK_INVALID: hooks 根节点必须是 object")
            continue
        for event, groups in events.items():
            if not isinstance(event, str) or not isinstance(groups, list):
                errors.append("PLUGIN_HOOK_INVALID: event 必须映射到数组")
                continue
            if event not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
                unsupported += 1
                continue
            for group in groups:
                if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
                    errors.append(f"PLUGIN_HOOK_INVALID: {event}")
                    continue
                matcher = group.get("matcher", "*")
                if not isinstance(matcher, str):
                    errors.append(f"PLUGIN_HOOK_MATCHER_INVALID: {event}")
                    continue
                try:
                    re.compile(matcher)
                except re.error:
                    errors.append(f"PLUGIN_HOOK_MATCHER_INVALID: {event}")
                    continue
                for handler in group["hooks"]:
                    if not isinstance(handler, Mapping):
                        errors.append(f"PLUGIN_HOOK_INVALID: {event}")
                    elif handler.get("type") != "command":
                        unsupported += 1
                    elif not isinstance(handler.get("command"), str):
                        errors.append(f"PLUGIN_HOOK_COMMAND_INVALID: {event}")
                    elif "args" in handler and (
                        not isinstance(handler["args"], list)
                        or not all(isinstance(item, str) for item in handler["args"])
                    ):
                        errors.append(f"PLUGIN_HOOK_ARGS_INVALID: {event}")
                    elif event == "PreToolUse" and handler.get("async") is True:
                        unsupported += 1
                    else:
                        supported += 1
    return supported, unsupported, errors


def _inspect_lsp_documents(
    documents: list[Mapping[str, object]],
) -> tuple[int, int, list[str]]:
    """统计字段完整的 stdio LSP，socket transport 保持 unsupported。"""
    supported = 0
    unsupported = 0
    errors: list[str] = []
    merged: dict[str, object] = {}
    for document in documents:
        values = document.get("lspServers", document)
        if not isinstance(values, Mapping):
            errors.append("PLUGIN_LSP_INVALID: 根节点必须是 object")
            continue
        for name, value in values.items():
            if not isinstance(name, str) or name in merged:
                errors.append("PLUGIN_LSP_DUPLICATE")
            else:
                merged[name] = value
    for name, value in merged.items():
        if not isinstance(value, Mapping):
            errors.append(f"PLUGIN_LSP_INVALID: {name}")
            continue
        if value.get("transport", "stdio") != "stdio":
            unsupported += 1
            continue
        extensions = value.get("extensionToLanguage")
        if (
            not isinstance(value.get("command"), str)
            or not isinstance(extensions, Mapping)
            or not extensions
            or not all(
                isinstance(extension, str)
                and extension.startswith(".")
                and isinstance(language, str)
                and language
                for extension, language in extensions.items()
            )
        ):
            errors.append(f"PLUGIN_LSP_INVALID: {name}")
            continue
        supported += 1
    return supported, unsupported, errors


def _inspect_monitor_files(
    files: list[Path],
) -> tuple[int, int, list[str]]:
    """统计当前支持的 shell Monitor entry。"""
    supported = 0
    errors: list[str] = []
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"PLUGIN_MONITOR_INVALID: {path.name}")
            continue
        entries = (
            value.get("monitors")
            if isinstance(value, Mapping) and "monitors" in value
            else value
        )
        if not isinstance(entries, list):
            errors.append(f"PLUGIN_MONITOR_INVALID: {path.name}")
            continue
        for entry in entries:
            if (
                isinstance(entry, Mapping)
                and isinstance(entry.get("name"), str)
                and isinstance(entry.get("command"), str)
            ):
                supported += 1
            else:
                errors.append(f"PLUGIN_MONITOR_INVALID: {path.name}")
    return supported, 0, errors


def _count_json_mapping(root: Path, relative: str, sources: list[str]) -> int:
    """读取 MCP JSON 并以顶层 mapping 数量作为服务器库存。"""
    document = read_json_object(root, relative)
    sources.append(relative.replace("\\", "/"))
    nested = document.get("mcpServers")
    if isinstance(nested, Mapping):
        return len(nested)
    return len(document)


def _count_json_document(root: Path, relative: str, sources: list[str]) -> int:
    """校验一个 JSON 配置来源并计为一个组件。"""
    read_json_object(root, relative)
    sources.append(relative.replace("\\", "/"))
    return 1


def _name_from_hint(value: str) -> str:
    """从 manifestless 来源目录生成稳定 kebab-case 名称。"""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return require_plugin_name(normalized)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    """保持 manifest 顺序去重。"""
    return tuple(dict.fromkeys(values))


def _merge_reports(reports: list[PluginComponentReport]) -> tuple[PluginComponentReport, ...]:
    """合并同类默认路径和 manifest 路径的报告。"""
    merged: dict[str, PluginComponentReport] = {}
    status_rank = {"supported": 0, "adapted": 1, "unsupported": 2, "invalid": 3}
    for report in reports:
        current = merged.get(report.kind)
        if current is None:
            merged[report.kind] = report
            continue
        status = max((current.status, report.status), key=lambda value: status_rank[value])
        merged[report.kind] = PluginComponentReport(
            kind=report.kind,
            status=status,
            count=current.count + report.count,
            sources=tuple(sorted(set(current.sources) | set(report.sources))),
            capabilities=tuple(sorted(set(current.capabilities) | set(report.capabilities))),
            diagnostics=tuple(dict.fromkeys((*current.diagnostics, *report.diagnostics))),
            effective=current.effective or report.effective,
        )
    return tuple(merged.values())
