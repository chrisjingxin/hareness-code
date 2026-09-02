"""Plugin 领域值对象；管理面只暴露名称、作用域和加载结果。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal


PluginFormat = Literal["agent-plugins-1.0", "claude-code", "qwen-code", "hybrid"]
PluginComponentStatus = Literal["supported", "adapted", "unsupported", "invalid"]
PluginActivation = Literal["enabled", "disabled"]
PluginProductStatus = Literal["loaded", "disabled", "warning", "failed"]

# Adapter 报告 revision 仍是内部缓存失效标记；它不代表用户授权状态。
PLUGIN_ADAPTER_REPORT_REVISION = "plugin-adapter-report-v2"

_COMPONENT_INVALID_DIAGNOSTIC_PREFIX = "PLUGIN_COMPONENT_INVALID:"
_COMPONENT_UNSUPPORTED_DIAGNOSTIC_PREFIX = "PLUGIN_COMPONENT_UNSUPPORTED:"
_COMPONENT_ERROR_CODE_RE = re.compile(r"\b(?:PLUGIN|SETTINGS)_[A-Z0-9_]+\b")


def component_warning_diagnostics(
    component: PluginComponentReport,
) -> tuple[str, ...]:
    """保留组件中可观察的错误诊断，过滤 Adapter 的成功提示。"""
    if not component.effective or component.status not in {"supported", "adapted"}:
        return component.diagnostics
    return tuple(
        diagnostic
        for diagnostic in component.diagnostics
        if _COMPONENT_ERROR_CODE_RE.search(diagnostic)
    )


class PluginError(ValueError):
    """Plugin 来源、格式、存储或状态变更不满足安全约束时抛出。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        """保存稳定错误码和可选字段，不在异常中附带外部绝对路径。"""
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class PluginComponentReport:
    """Adapter 内部组件报告；status/effective 不能直接成为产品状态。"""

    kind: str
    status: PluginComponentStatus
    count: int
    sources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    effective: bool = False

    def to_dict(self) -> dict[str, object]:
        """返回 registry 内部报告，供受信任的本地存储重建。"""
        return {
            "kind": self.kind,
            "status": self.status,
            "count": self.count,
            "sources": list(self.sources),
            "capabilities": list(self.capabilities),
            "diagnostics": list(self.diagnostics),
            "effective": self.effective,
        }

    def to_public_dict(self) -> dict[str, object]:
        """返回真实 consumer 组件摘要，不泄漏 Adapter 状态机字段。"""
        return {
            "kind": self.kind,
            "count": self.count,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class RuntimeComponentEligibility:
    """一个组件是否可以从 Adapter 报告进入 canonical runtime。"""

    kind: str
    eligible: bool
    component: PluginComponentReport | None = None
    reason: str | None = None

    @property
    def diagnostic(self) -> str | None:
        """返回不含路径、正文和凭据的稳定 gate 诊断。"""
        if self.eligible or self.reason is None:
            return None
        return f"PLUGIN_RUNTIME_COMPONENT_BLOCKED: kind={self.kind}; reason={self.reason}"


_RUNTIME_COMPONENT_FORMATS: dict[PluginFormat, frozenset[str]] = {
    "agent-plugins-1.0": frozenset(
        {"commands", "skills", "mcp", "agents", "policies", "teams"}
    ),
    "claude-code": frozenset(
        {
            "hooks",
            "lsp",
            "monitors",
            "mcp",
            "commands",
            "skills",
            "agents",
            "policies",
            "teams",
        }
    ),
    "hybrid": frozenset(
        {
            "hooks",
            "lsp",
            "monitors",
            "mcp",
            "commands",
            "skills",
            "agents",
            "policies",
            "teams",
        }
    ),
    "qwen-code": frozenset(
        {
            "hooks",
            "lsp",
            "commands",
            "skills",
            "agents",
            "contexts",
            "mcp",
            "settings",
        }
    ),
}


def runtime_component_eligibility(
    plugin: "InstalledPlugin",
    *,
    kind: str,
) -> RuntimeComponentEligibility:
    """统一判断 Adapter 组件能否进入 runtime。

    activation 已由 PluginManager 在构造 ``ExtensionCatalogSnapshot`` 时解析；
    这里只负责组件报告和格式门禁，避免把用户产品状态重新拆成 trust 状态机。
    """
    allowed_kinds = _RUNTIME_COMPONENT_FORMATS.get(plugin.format)
    if allowed_kinds is None:
        return RuntimeComponentEligibility(kind=kind, eligible=False, reason="FORMAT_UNSUPPORTED")
    if kind not in allowed_kinds:
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            reason="FORMAT_COMPONENT_UNSUPPORTED",
        )
    if (
        not isinstance(plugin.package_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", plugin.package_digest) is None
    ):
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            reason="PLUGIN_IDENTITY_INVALID",
        )
    reports = tuple(component for component in plugin.components if component.kind == kind)
    if not reports:
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            reason="COMPONENT_REPORT_MISSING",
        )
    if len(reports) != 1:
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            reason="COMPONENT_REPORT_AMBIGUOUS",
        )
    component = reports[0]
    if component.status not in {"supported", "adapted"}:
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            component=component,
            reason=f"COMPONENT_STATUS_{component.status.upper()}",
        )
    if not component.effective:
        return RuntimeComponentEligibility(
            kind=kind,
            eligible=False,
            component=component,
            reason="COMPONENT_NOT_EFFECTIVE",
        )
    return RuntimeComponentEligibility(kind=kind, eligible=True, component=component)


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """已在 staging 中完成格式与组件校验的 Plugin 包摘要。"""

    name: str
    version: str | None
    description: str | None
    format: PluginFormat
    manifest: str | None
    package_digest: str
    components: tuple[PluginComponentReport, ...]
    diagnostics: tuple[str, ...] = ()
    adapter_revision: str | None = PLUGIN_ADAPTER_REPORT_REVISION

    @property
    def can_enable(self) -> bool:
        """内部判断是否至少有一个组件可交给 canonical consumer。"""
        return any(component.effective for component in self.components) and (
            self.compatibility != "invalid"
        )

    @property
    def compatibility(self) -> str:
        """保留 Adapter 内部兼容汇总，管理面改用四种产品状态。"""
        return _aggregate_compatibility(self.components)

    def to_dict(self) -> dict[str, object]:
        """返回 validate 可用的公开摘要，不包含 digest 或 Adapter 状态字段。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "manifest": self.manifest,
            "components": [
                component.to_public_dict()
                for component in self.components
                if component.effective
            ],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """registry 中的 artifact 与 activation 记录。"""

    plugin_id: str
    source_id: str
    source_label: str
    name: str
    version: str | None
    description: str | None
    format: PluginFormat
    manifest: str | None
    package_digest: str
    components: tuple[PluginComponentReport, ...]
    diagnostics: tuple[str, ...]
    activation_user: PluginActivation
    activation_workspaces: tuple[tuple[str, PluginActivation], ...]
    installed_at_ms: int
    adapter_revision: str | None = None
    origin: str | None = None

    @property
    def can_enable(self) -> bool:
        """内部判断是否存在可运行组件；不是用户操作前置条件。"""
        return self.descriptor().can_enable

    @property
    def compatibility(self) -> str:
        """返回内部 Adapter 汇总；不进入正常 response。"""
        return self.descriptor().compatibility

    def activation_for(self, workspace_binding_digest: str | None = None) -> PluginActivation:
        """按 workspace override 优先规则计算有效 activation。"""
        if workspace_binding_digest is not None:
            for binding, activation in self.activation_workspaces:
                if binding == workspace_binding_digest:
                    return activation
        return self.activation_user

    def descriptor(self) -> PluginDescriptor:
        """重建无来源信息的不可变校验摘要。"""
        return PluginDescriptor(
            name=self.name,
            version=self.version,
            description=self.description,
            format=self.format,
            manifest=self.manifest,
            package_digest=self.package_digest,
            components=self.components,
            diagnostics=self.diagnostics,
            adapter_revision=self.adapter_revision,
        )

    def to_dict(self) -> dict[str, object]:
        """返回脱敏安装摘要；动态 status 由 Manager 按 workspace 投影。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "manifest": self.manifest,
            "source": {"label": self.source_label, "kind": "local"},
            "activation": {
                "user": self.activation_user,
                "workspaces": {
                    binding: activation
                    for binding, activation in self.activation_workspaces
                },
            },
            "installed_at_ms": self.installed_at_ms,
            "components": [
                component.to_public_dict()
                for component in self.components
                if component.effective
            ],
            "diagnostics": list(self.diagnostics),
        }

    def to_record(self) -> dict[str, object]:
        """转换为 registry v3 JSON 记录。"""
        origin: dict[str, object] | None = None
        if self.origin is not None:
            origin = {"kind": "local", "path": self.origin}
        return {
            "id": self.plugin_id,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "manifest": self.manifest,
            "package_digest": self.package_digest,
            "components": [component.to_dict() for component in self.components],
            "diagnostics": list(self.diagnostics),
            "activation": {
                "user": self.activation_user,
                "workspaces": {
                    binding: activation
                    for binding, activation in self.activation_workspaces
                },
            },
            "installed_at_ms": self.installed_at_ms,
            "adapter_revision": self.adapter_revision,
            "origin": origin,
        }


@dataclass(frozen=True, slots=True)
class ExtensionCatalogSnapshot:
    """由当前 workspace 有效 activation 组成的不可变启动期目录摘要。"""

    snapshot_id: str
    registry_revision: int
    plugins: tuple[InstalledPlugin, ...]

    def to_dict(self) -> dict[str, object]:
        """返回内部 catalog 摘要，不带宿主 store 路径。"""
        return {
            "id": self.snapshot_id,
            "registry_revision": self.registry_revision,
            "count": len(self.plugins),
            "plugins": [plugin.to_dict() for plugin in self.plugins],
        }


def catalog_snapshot_id(revision: int, plugins: tuple[InstalledPlugin, ...]) -> str:
    """计算 enabled catalog 的稳定内部快照 ID。"""
    return _sha256(
        {
            "revision": revision,
            "plugins": [
                {"id": plugin.plugin_id, "digest": plugin.package_digest}
                for plugin in plugins
            ],
        }
    )[:16]


def product_status(
    plugin: InstalledPlugin,
    *,
    activation: PluginActivation,
) -> PluginProductStatus:
    """把 activation 与 Adapter 结果投影为唯一用户产品状态。"""
    if activation == "disabled":
        return "disabled"
    effective = [component for component in plugin.components if component.effective]
    warnings = bool(plugin.diagnostics) or any(
        component.status in {"unsupported", "invalid"}
        or bool(component_warning_diagnostics(component))
        for component in plugin.components
    )
    if not effective:
        return "failed"
    return "warning" if warnings else "loaded"


def plugin_warnings(plugin: InstalledPlugin) -> tuple[str, ...]:
    """收集有界加载 warning；过滤 Adapter 的成功提示和内部状态名。"""
    warnings = list(plugin.diagnostics)
    for component in plugin.components:
        if not component.effective or component.status not in {"supported", "adapted"}:
            warnings.extend(component.diagnostics)
            continue
        # 一个组件可以同时包含成功条目和被隔离的坏条目；这种 partial
        # report 仍是有效 runtime component，但坏条目必须出现在 Plugin warning。
        warnings.extend(component_warning_diagnostics(component))
    return tuple(dict.fromkeys(item for item in warnings if item))


def merge_component_reports(
    current: PluginComponentReport,
    incoming: PluginComponentReport,
) -> PluginComponentReport:
    """合并同类报告并保留仍可运行的条目。"""
    reports = (current, incoming)
    effective = any(report.effective for report in reports)
    statuses = {report.status for report in reports}
    if effective:
        status: PluginComponentStatus = (
            "adapted"
            if "adapted" in statuses
            else "supported"
            if "supported" in statuses
            else "unsupported"
            if "unsupported" in statuses
            else "invalid"
        )
    else:
        status = (
            "invalid"
            if "invalid" in statuses
            else "unsupported"
            if "unsupported" in statuses
            else "adapted"
            if "adapted" in statuses
            else "supported"
        )
    diagnostics = list(dict.fromkeys((*current.diagnostics, *incoming.diagnostics)))
    if "invalid" in statuses and not any(
        item.startswith(_COMPONENT_INVALID_DIAGNOSTIC_PREFIX) for item in diagnostics
    ):
        diagnostics.append(f"{_COMPONENT_INVALID_DIAGNOSTIC_PREFIX} 同类组件存在无效条目")
    if "unsupported" in statuses and not any(
        item.startswith(_COMPONENT_UNSUPPORTED_DIAGNOSTIC_PREFIX) for item in diagnostics
    ):
        diagnostics.append(f"{_COMPONENT_UNSUPPORTED_DIAGNOSTIC_PREFIX} 同类组件存在未支持条目")
    return PluginComponentReport(
        kind=incoming.kind,
        status=status,
        count=current.count + incoming.count,
        sources=tuple(sorted(set(current.sources) | set(incoming.sources))),
        capabilities=tuple(sorted(set(current.capabilities) | set(incoming.capabilities))),
        diagnostics=tuple(diagnostics),
        effective=effective,
    )


def _aggregate_compatibility(components: tuple[PluginComponentReport, ...]) -> str:
    """保留给 Adapter 内部测试的兼容汇总。"""
    has_effective = any(component.effective for component in components)
    has_invalid = any(
        component.status == "invalid"
        or _has_component_diagnostic(component, _COMPONENT_INVALID_DIAGNOSTIC_PREFIX)
        for component in components
    )
    has_unsupported = any(
        component.status == "unsupported"
        or _has_component_diagnostic(component, _COMPONENT_UNSUPPORTED_DIAGNOSTIC_PREFIX)
        for component in components
    )
    if not has_effective:
        return "invalid" if has_invalid else "recognized"
    if has_invalid or has_unsupported:
        return "partial"
    if any(component.status == "adapted" for component in components):
        return "recognized"
    return "ready"


def _has_component_diagnostic(component: PluginComponentReport, prefix: str) -> bool:
    """判断内部组件报告是否包含局部 invalid/unsupported 诊断。"""
    return any(diagnostic.startswith(prefix) for diagnostic in component.diagnostics)


def _sha256(value: object) -> str:
    """对 canonical JSON 计算 SHA-256。"""
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
