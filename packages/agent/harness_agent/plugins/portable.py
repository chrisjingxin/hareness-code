"""Agent Plugins 1.0 离线 Adapter；只输出 canonical 描述，不接触运行时。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from harness_agent.plugins.common import (
    list_regular_files,
    read_json_object,
    relative_sources,
    require_portable_plugin_name,
    safe_package_path,
    validate_skill_manifests,
)
from harness_agent.plugins.mcp_schema import MCP_SCHEMA_ID, validate_mcp_document
from harness_agent.plugins.model import (
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
    capability_fingerprint,
)


PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
HARNESS_EXTENSION_NAMESPACE = "com.za38.harness"
_KNOWN_MANIFEST_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
_HARNESS_COMPONENT_FIELDS = {
    "commands": ("commands", (".md",)),
    "agents": ("agents", (".yaml", ".yml", ".json", ".md")),
    "policies": ("policies", (".yaml", ".yml", ".json")),
    "teams": ("teams", (".yaml", ".yml", ".json")),
    "hooks": ("hooks", (".json",)),
}


def is_portable_plugin(root: Path) -> bool:
    """只把声明 Agent Plugins canonical schema 的根 manifest 识别为 portable。"""
    manifest = root / "plugin.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        return read_json_object(root, "plugin.json").get("$schema") == PLUGIN_SCHEMA_ID
    except PluginError:
        return True


def load_portable_plugin(root: Path, *, package_digest: str) -> PluginDescriptor:
    """校验 Agent Plugins 1.0 manifest，并隔离报告各组件错误。"""
    manifest = read_json_object(root, "plugin.json")
    if manifest.get("$schema") != PLUGIN_SCHEMA_ID:
        raise PluginError(
            "PLUGIN_SCHEMA_UNSUPPORTED",
            "plugin.json 未声明受支持的 Agent Plugins 1.0 schema",
            field="$schema",
        )
    name = require_portable_plugin_name(manifest.get("name"))
    version = _manifest_string(manifest, "version")
    description = _manifest_string(manifest, "description")
    _validate_metadata(manifest)
    diagnostics = tuple(
        f"plugin.json: 未识别的顶层字段 {field}"
        for field in sorted(set(manifest) - _KNOWN_MANIFEST_FIELDS)
    )

    components: list[PluginComponentReport] = []
    skills = root / "skills"
    if skills.exists() or skills.is_symlink():
        manifests, errors = validate_skill_manifests(root, skills, require_name=True)
        components.append(
            PluginComponentReport(
                kind="skills",
                status="supported" if manifests or not errors else "invalid",
                count=len(manifests),
                sources=relative_sources(root, manifests),
                capabilities=("prompt:skill",),
                diagnostics=(
                    tuple(f"PLUGIN_COMPONENT_INVALID: {error}" for error in errors)
                    or ("已接入 Harness 启动期 Skill 快照",)
                ),
                effective=bool(manifests),
            )
        )

    mcp_path = root / "mcp.json"
    if mcp_path.exists() or mcp_path.is_symlink():
        components.append(_validate_mcp(root))

    extensions = manifest.get("extensions")
    if "extensions" in manifest and not isinstance(extensions, Mapping):
        diagnostics += ("plugin.json: extensions 不是 object，已忽略所有 client extension",)
    elif isinstance(extensions, Mapping):
        harness_extension = extensions.get(HARNESS_EXTENSION_NAMESPACE)
        if harness_extension is not None:
            if not isinstance(harness_extension, Mapping):
                components.append(
                    PluginComponentReport(
                        kind="harness-extension",
                        status="invalid",
                        count=1,
                        diagnostics=(f"{HARNESS_EXTENSION_NAMESPACE} 必须是 object",),
                    )
                )
            else:
                components.extend(_load_harness_extension(root, harness_extension))

    components_tuple = tuple(sorted(components, key=lambda item: item.kind))
    return PluginDescriptor(
        name=name,
        version=version,
        description=description,
        format="agent-plugins-1.0",
        manifest="plugin.json",
        package_digest=package_digest,
        capability_fingerprint=capability_fingerprint(components_tuple),
        components=components_tuple,
        diagnostics=diagnostics,
    )


def _validate_metadata(manifest: Mapping[str, Any]) -> None:
    """校验会被核心展示的 portable metadata 类型。"""
    author = manifest.get("author")
    if "author" in manifest and not isinstance(author, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "author 必须是 object", field="author")
    if isinstance(author, Mapping):
        if any(field not in {"name", "email", "url"} for field in author):
            raise PluginError(
                "PLUGIN_MANIFEST_FIELD_INVALID",
                "author 只允许 name、email、url 字段",
                field="author",
            )
        if any(not isinstance(value, str) for value in author.values()):
            raise PluginError(
                "PLUGIN_MANIFEST_FIELD_INVALID",
                "author 的字段值必须是字符串",
                field="author",
            )
    keywords = manifest.get("keywords")
    if "keywords" in manifest and (
        not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords)
    ):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "keywords 必须是字符串数组", field="keywords")
    for field in ("homepage", "repository", "license"):
        _manifest_string(manifest, field)


def _manifest_string(manifest: Mapping[str, Any], field: str) -> str | None:
    """按 portable schema 只校验 metadata 的 JSON string 类型。"""
    if field not in manifest:
        return None
    value = manifest[field]
    if not isinstance(value, str):
        raise PluginError(
            "PLUGIN_MANIFEST_FIELD_INVALID",
            f"{field} 必须是字符串",
            field=field,
        )
    return value


def _validate_mcp(root: Path) -> PluginComponentReport:
    """离线校验 MCP closed schema，并隔离单条 invalid/unsupported。"""
    try:
        document = read_json_object(root, "mcp.json")
        validation = validate_mcp_document(document, root=root)
        transports: set[str] = set()
        for server in validation.servers:
            transports.add(server.transport)
        capabilities = []
        if "stdio" in transports:
            capabilities.append("process:mcp")
        if transports & {"streamable-http", "sse"}:
            capabilities.append("network:mcp")
        diagnostics = tuple(
            f"PLUGIN_COMPONENT_INVALID: {error}" for error in validation.invalid
        ) + tuple(
            f"PLUGIN_COMPONENT_UNSUPPORTED: {error}" for error in validation.unsupported
        )
        if validation.servers:
            status = "supported"
        elif validation.invalid:
            status = "invalid"
        elif validation.unsupported:
            status = "unsupported"
        else:
            status = "supported"
        return PluginComponentReport(
            kind="mcp",
            status=status,
            count=len(validation.servers),
            sources=("mcp.json",),
            capabilities=tuple(capabilities),
            diagnostics=diagnostics or ("已接入 Harness 启动期 MCP 快照",),
            effective=bool(validation.servers),
        )
    except PluginError as exc:
        return PluginComponentReport(
            kind="mcp",
            status="invalid",
            count=0,
            sources=("mcp.json",),
            capabilities=("process:mcp", "network:mcp"),
            diagnostics=(f"{exc.code}: {exc}",),
        )


def _load_harness_extension(
    root: Path,
    extension: Mapping[str, object],
) -> tuple[PluginComponentReport, ...]:
    """库存 Harness namespace 中尚未进入生产的 Agent、Team 等组件。"""
    reports: list[PluginComponentReport] = []
    schema_version = extension.get("schemaVersion")
    if schema_version is not None and schema_version != "1.0.0":
        reports.append(
            PluginComponentReport(
                kind="harness-extension",
                status="invalid",
                count=1,
                diagnostics=("Harness extension schemaVersion 目前只支持 1.0.0",),
            )
        )
        return tuple(reports)
    for field, (kind, suffixes) in _HARNESS_COMPONENT_FIELDS.items():
        raw_path = extension.get(field)
        if raw_path is None:
            continue
        try:
            if not isinstance(raw_path, str):
                raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", f"{field} 必须是相对路径")
            path = safe_package_path(root, raw_path, require_exists=True)
            files = list_regular_files(path, suffixes=suffixes)
            reports.append(
                PluginComponentReport(
                    kind=kind,
                    status=(
                        "supported"
                        if kind in {"commands", "agents", "policies", "teams"}
                        else "unsupported"
                    ),
                    count=len(files),
                    sources=relative_sources(root, files),
                    capabilities=_harness_capabilities(kind),
                    diagnostics=(
                        ("已接入 Harness 启动期 Command/Agent/Policy 快照",)
                        if kind in {"commands", "agents", "policies", "teams"}
                        else ("格式已识别，但当前运行时尚未接入此组件",)
                    ),
                    effective=kind in {"commands", "agents", "policies", "teams"}
                    and bool(files),
                )
            )
        except PluginError as exc:
            reports.append(
                PluginComponentReport(
                    kind=kind,
                    status="invalid",
                    count=0,
                    diagnostics=(f"{exc.code}: {exc}",),
                )
            )
    return tuple(reports)


def _harness_capabilities(kind: str) -> tuple[str, ...]:
    """返回 Harness 私有组件的能力请求分类。"""
    return {
        "commands": ("prompt:command",),
        "agents": ("delegation:agent",),
        "policies": ("policy:request",),
        "teams": ("coordination:team",),
        "hooks": ("process:hook",),
    }.get(kind, ())
