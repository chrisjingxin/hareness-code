"""Agent Plugins 1.0 离线 Adapter；只输出 canonical 描述，不接触运行时。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from harness_agent.plugins.common import (
    PLUGIN_NAME_RE,
    VERSION_RE,
    list_regular_files,
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


PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
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
    name = require_plugin_name(manifest.get("name"))
    version = optional_string(manifest.get("version"), "version")
    if version is not None and not VERSION_RE.fullmatch(version):
        raise PluginError("PLUGIN_VERSION_INVALID", "Plugin version 格式无效", field="version")
    description = optional_string(manifest.get("description"), "description")
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
                status="supported" if manifests else "invalid",
                count=len(manifests),
                sources=relative_sources(root, manifests),
                capabilities=("prompt:skill",),
                diagnostics=errors or ("已接入 Harness 启动期 Skill 快照",),
                effective=bool(manifests),
            )
        )

    mcp_path = root / "mcp.json"
    if mcp_path.exists() or mcp_path.is_symlink():
        components.append(_validate_mcp(root))

    extensions = manifest.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
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
    if author is not None and not isinstance(author, Mapping):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "author 必须是 object", field="author")
    keywords = manifest.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords)
    ):
        raise PluginError("PLUGIN_MANIFEST_FIELD_INVALID", "keywords 必须是字符串数组", field="keywords")
    for field in ("homepage", "repository", "license"):
        optional_string(manifest.get(field), field)


def _validate_mcp(root: Path) -> PluginComponentReport:
    """离线校验 Agent Plugins MCP 顶层结构和 transport 请求。"""
    try:
        document = read_json_object(root, "mcp.json")
        if document.get("$schema") != MCP_SCHEMA_ID:
            raise PluginError("PLUGIN_MCP_SCHEMA_UNSUPPORTED", "mcp.json schema 与 Plugin 版本不一致")
        raw_servers = document.get("mcpServers")
        if not isinstance(raw_servers, Mapping):
            raise PluginError("PLUGIN_MCP_INVALID", "mcpServers 必须是 object")
        transports: set[str] = set()
        valid_count = 0
        errors: list[str] = []
        for name, raw in raw_servers.items():
            try:
                if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
                    raise PluginError("PLUGIN_MCP_INVALID", "MCP server name 必须是 kebab-case")
                if not isinstance(raw, Mapping):
                    raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 必须是 object")
                transport = raw.get("type")
                if transport not in {"stdio", "streamable-http", "sse"}:
                    raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} transport 不受支持")
                if transport == "stdio":
                    if not isinstance(raw.get("command"), str) or not str(raw["command"]).strip():
                        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 缺少 command")
                    _require_string_list(raw.get("args", []), f"{name}.args")
                    _require_string_map(raw.get("env", {}), f"{name}.env")
                    if raw.get("cwd") is not None and not isinstance(raw.get("cwd"), str):
                        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name}.cwd 必须是字符串")
                else:
                    if not isinstance(raw.get("url"), str) or not str(raw["url"]).strip():
                        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {name} 缺少 url")
                    _require_string_map(raw.get("headers", {}), f"{name}.headers")
                transports.add(str(transport))
                valid_count += 1
            except PluginError as exc:
                errors.append(f"{name}: {exc}")
        capabilities = []
        if "stdio" in transports:
            capabilities.append("process:mcp")
        if transports & {"streamable-http", "sse"}:
            capabilities.append("network:mcp")
        return PluginComponentReport(
            kind="mcp",
            status="supported" if valid_count else "invalid",
            count=valid_count,
            sources=("mcp.json",),
            capabilities=tuple(capabilities),
            diagnostics=tuple(errors) or ("已接入 Harness 启动期 MCP 快照",),
            effective=valid_count > 0,
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
                    effective=kind in {"commands", "agents", "policies", "teams"},
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


def _require_string_list(value: object, field: str) -> None:
    """校验 MCP 字符串数组。"""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginError("PLUGIN_MCP_INVALID", f"{field} 必须是字符串数组")


def _require_string_map(value: object, field: str) -> None:
    """校验 MCP 字符串映射。"""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise PluginError("PLUGIN_MCP_INVALID", f"{field} 必须是字符串映射")
