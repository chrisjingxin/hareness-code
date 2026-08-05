"""Plugin 格式自动识别与 hybrid 合并入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from harness_agent.plugins.claude import (
    has_explicit_claude_manifest,
    has_unambiguous_claude_components,
    load_claude_plugin,
)
from harness_agent.plugins.model import (
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
    capability_fingerprint,
)
from harness_agent.plugins.portable import is_portable_plugin, load_portable_plugin


RequestedPluginFormat = Literal["auto", "agent-plugins-1.0", "claude-code"]


def load_plugin_descriptor(
    root: Path,
    *,
    package_digest: str,
    name_hint: str,
    requested_format: RequestedPluginFormat = "auto",
) -> PluginDescriptor:
    """选择唯一 Adapter；双 manifest 包以 portable 为 core、Claude 为补充。"""
    portable = is_portable_plugin(root)
    claude = has_explicit_claude_manifest(root)
    if requested_format == "agent-plugins-1.0":
        if not portable:
            raise PluginError("PLUGIN_FORMAT_MISMATCH", "来源不是 Agent Plugins 1.0 包")
        return load_portable_plugin(root, package_digest=package_digest)
    if requested_format == "claude-code":
        return load_claude_plugin(
            root,
            package_digest=package_digest,
            name_hint=name_hint,
        )
    if portable and claude:
        base = load_portable_plugin(root, package_digest=package_digest)
        supplement = load_claude_plugin(
            root,
            package_digest=package_digest,
            name_hint=name_hint,
            include_portable_components=False,
        )
        return _merge_hybrid(base, supplement)
    if portable:
        return load_portable_plugin(root, package_digest=package_digest)
    if claude or has_unambiguous_claude_components(root):
        return load_claude_plugin(
            root,
            package_digest=package_digest,
            name_hint=name_hint,
        )
    if (root / "plugin.json").exists():
        raise PluginError(
            "PLUGIN_SCHEMA_UNSUPPORTED",
            "plugin.json 未声明受支持的 Agent Plugins 1.0 schema",
        )
    raise PluginError(
        "PLUGIN_FORMAT_AMBIGUOUS",
        "无法自动识别 Plugin；仅含 skills/ 的 manifestless 包请显式指定 claude-code",
    )


def _merge_hybrid(
    portable: PluginDescriptor,
    claude: PluginDescriptor,
) -> PluginDescriptor:
    """合并双 manifest 包，并拒绝两个 manifest 对身份的不同声明。"""
    if portable.name != claude.name:
        raise PluginError("PLUGIN_IDENTITY_CONFLICT", "双 manifest 的 Plugin name 不一致")
    if portable.version and claude.version and portable.version != claude.version:
        raise PluginError("PLUGIN_IDENTITY_CONFLICT", "双 manifest 的 Plugin version 不一致")
    reports: dict[str, PluginComponentReport] = {
        component.kind: component for component in portable.components
    }
    for component in claude.components:
        current = reports.get(component.kind)
        if current is None:
            reports[component.kind] = component
            continue
        reports[component.kind] = PluginComponentReport(
            kind=component.kind,
            status="invalid"
            if "invalid" in {current.status, component.status}
            else "unsupported"
            if "unsupported" in {current.status, component.status}
            else "adapted"
            if "adapted" in {current.status, component.status}
            else "supported",
            count=current.count + component.count,
            sources=tuple(sorted(set(current.sources) | set(component.sources))),
            capabilities=tuple(sorted(set(current.capabilities) | set(component.capabilities))),
            diagnostics=tuple(dict.fromkeys((*current.diagnostics, *component.diagnostics))),
            effective=current.effective or component.effective,
        )
    components = tuple(sorted(reports.values(), key=lambda item: item.kind))
    return PluginDescriptor(
        name=portable.name,
        version=portable.version or claude.version,
        description=portable.description or claude.description,
        format="hybrid",
        manifest="plugin.json + .claude-plugin/plugin.json",
        package_digest=portable.package_digest,
        capability_fingerprint=capability_fingerprint(components),
        components=components,
        diagnostics=tuple(dict.fromkeys((*portable.diagnostics, *claude.diagnostics))),
    )
