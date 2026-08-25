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
    merge_component_reports,
)
from harness_agent.plugins.portable import is_portable_plugin, load_portable_plugin
from harness_agent.plugins.qwen import (
    load_qwen_plugin,
    qwen_manifest_paths,
)


RequestedPluginFormat = Literal["auto", "agent-plugins-1.0", "claude-code", "qwen-code"]


def load_plugin_descriptor(
    root: Path,
    *,
    package_digest: str,
    name_hint: str,
    requested_format: RequestedPluginFormat = "auto",
) -> PluginDescriptor:
    """选择唯一 Adapter；双 manifest 包以 portable 为 core、Claude 为补充。"""
    qwen_manifests = qwen_manifest_paths(root)
    qwen = bool(qwen_manifests)
    portable = is_portable_plugin(root)
    claude = has_explicit_claude_manifest(root)
    portable_manifest = (root / "plugin.json").exists() or (root / "plugin.json").is_symlink()
    if len(qwen_manifests) > 1:
        raise PluginError(
            "PLUGIN_FORMAT_CONFLICT",
            "qwen-extension.json 与 devagent-extension.json 不能同时存在",
        )
    if qwen and (portable_manifest or claude):
        raise PluginError(
            "PLUGIN_FORMAT_CONFLICT",
            "Qwen/DevAgent 清单不能与 portable 或 Claude manifest 共存",
        )
    if requested_format == "qwen-code":
        if not qwen:
            raise PluginError(
                "PLUGIN_FORMAT_MISMATCH",
                "显式 qwen-code 要求唯一的 Qwen/DevAgent Extension 清单",
            )
        return load_qwen_plugin(root, package_digest=package_digest)
    if qwen and requested_format != "auto":
        raise PluginError(
            "PLUGIN_FORMAT_MISMATCH",
            f"显式 {requested_format} 与 Qwen/DevAgent Extension 清单不匹配",
        )
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
    if qwen:
        return load_qwen_plugin(root, package_digest=package_digest)
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
        reports[component.kind] = merge_component_reports(current, component)
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
