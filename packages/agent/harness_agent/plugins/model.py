"""Plugin 领域值对象；协议摘要不会暴露宿主绝对路径或扩展正文。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal


PluginFormat = Literal["agent-plugins-1.0", "claude-code", "hybrid"]
PluginComponentStatus = Literal["supported", "adapted", "unsupported", "invalid"]

# 组件报告没有单独增加 issue 字段；这些前缀只用于从现有 diagnostics
# 区分“有可运行条目但同类存在坏条目/不支持条目”的局部降级。
_COMPONENT_INVALID_DIAGNOSTIC_PREFIX = "PLUGIN_COMPONENT_INVALID:"
_COMPONENT_UNSUPPORTED_DIAGNOSTIC_PREFIX = "PLUGIN_COMPONENT_UNSUPPORTED:"


class PluginError(ValueError):
    """Plugin 来源、格式、存储或状态变更不满足安全约束时抛出。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        """保存稳定错误码和可选字段，不在异常中附带外部绝对路径。"""
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class PluginComponentReport:
    """一个组件类型的发现数量、兼容状态和安全能力请求。"""

    kind: str
    status: PluginComponentStatus
    count: int
    sources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    effective: bool = False

    def to_dict(self) -> dict[str, object]:
        """返回适合 JSON-RPC 的组件报告。"""
        return {
            "kind": self.kind,
            "status": self.status,
            "count": self.count,
            "sources": list(self.sources),
            "capabilities": list(self.capabilities),
            "diagnostics": list(self.diagnostics),
            "effective": self.effective,
        }


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """已在 staging 中完成格式与组件校验的 Plugin 包摘要。"""

    name: str
    version: str | None
    description: str | None
    format: PluginFormat
    manifest: str | None
    package_digest: str
    capability_fingerprint: str
    components: tuple[PluginComponentReport, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def can_enable(self) -> bool:
        """只有存在 effective 组件且聚合状态不是 invalid 时才允许启用。"""
        return any(component.effective for component in self.components) and (
            self.compatibility != "invalid"
        )

    @property
    def compatibility(self) -> str:
        """按 effective 能力和局部诊断汇总 Plugin 兼容状态。"""
        return _aggregate_compatibility(self.components)

    def to_dict(self) -> dict[str, object]:
        """返回不含来源根目录的校验摘要。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "manifest": self.manifest,
            "package_digest": self.package_digest,
            "capability_fingerprint": self.capability_fingerprint,
            "compatibility": self.compatibility,
            "can_enable": self.can_enable,
            "components": [component.to_dict() for component in self.components],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """Plugin registry 中的安装记录；store 相对位置由字段确定，不单独持久化。"""

    plugin_id: str
    source_id: str
    source_label: str
    name: str
    version: str | None
    description: str | None
    format: PluginFormat
    manifest: str | None
    package_digest: str
    capability_fingerprint: str
    components: tuple[PluginComponentReport, ...]
    diagnostics: tuple[str, ...]
    enabled: bool
    trusted_capability_fingerprint: str | None
    installed_at_ms: int

    @property
    def can_enable(self) -> bool:
        """复用校验结果判断记录是否允许启用。"""
        return self.descriptor().can_enable

    @property
    def compatibility(self) -> str:
        """返回与 PluginDescriptor 相同的兼容汇总。"""
        return self.descriptor().compatibility

    def descriptor(self) -> PluginDescriptor:
        """重建无来源信息的不可变校验摘要。"""
        return PluginDescriptor(
            name=self.name,
            version=self.version,
            description=self.description,
            format=self.format,
            manifest=self.manifest,
            package_digest=self.package_digest,
            capability_fingerprint=self.capability_fingerprint,
            components=self.components,
            diagnostics=self.diagnostics,
        )

    def to_dict(self) -> dict[str, object]:
        """返回不含 store、data 和原始来源绝对路径的安装摘要。"""
        result = self.descriptor().to_dict()
        result.update(
            {
                "id": self.plugin_id,
                "source": {"id": self.source_id, "label": self.source_label, "kind": "local"},
                "enabled": self.enabled,
                "trusted": (
                    self.enabled
                    and self.trusted_capability_fingerprint == self.capability_fingerprint
                ),
                "installed_at_ms": self.installed_at_ms,
            }
        )
        return result

    def to_record(self) -> dict[str, object]:
        """转换为 registry 的版本化 JSON 记录。"""
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
            "capability_fingerprint": self.capability_fingerprint,
            "components": [component.to_dict() for component in self.components],
            "diagnostics": list(self.diagnostics),
            "enabled": self.enabled,
            "trusted_capability_fingerprint": self.trusted_capability_fingerprint,
            "installed_at_ms": self.installed_at_ms,
        }


@dataclass(frozen=True, slots=True)
class ExtensionCatalogSnapshot:
    """由已启用 Plugin 记录组成的不可变启动期目录摘要。"""

    snapshot_id: str
    registry_revision: int
    plugins: tuple[InstalledPlugin, ...]

    def to_dict(self) -> dict[str, object]:
        """返回轻量 snapshot 摘要。"""
        return {
            "id": self.snapshot_id,
            "registry_revision": self.registry_revision,
            "count": len(self.plugins),
            "plugins": [plugin.to_dict() for plugin in self.plugins],
        }


def capability_fingerprint(components: tuple[PluginComponentReport, ...]) -> str:
    """仅按能力库存计算 trust 指纹，文档和普通资源变化不会伪造新增权限。"""
    payload = [
        {
            "kind": component.kind,
            "status": component.status,
            "count": component.count,
            "capabilities": list(component.capabilities),
            "effective": component.effective,
        }
        for component in sorted(components, key=lambda item: item.kind)
    ]
    return _sha256(payload)


def catalog_snapshot_id(revision: int, plugins: tuple[InstalledPlugin, ...]) -> str:
    """计算 enabled catalog 的稳定快照 ID。"""
    return _sha256(
        {
            "revision": revision,
            "plugins": [
                {
                    "id": plugin.plugin_id,
                    "digest": plugin.package_digest,
                    "capabilities": plugin.capability_fingerprint,
                }
                for plugin in plugins
            ],
        }
    )[:16]


def _sha256(value: object) -> str:
    """对 canonical JSON 计算 SHA-256。"""
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _aggregate_compatibility(
    components: tuple[PluginComponentReport, ...],
) -> str:
    """统一 Descriptor/InstalledPlugin 的 compatibility 聚合。"""
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
        # 没有 Harness 可运行能力时，纯识别/不支持仍是 recognized；
        # 组件格式已明确损坏才汇总为 invalid。两者都不能启用。
        return "invalid" if has_invalid else "recognized"
    if has_invalid or has_unsupported:
        return "partial"
    if any(component.status == "adapted" for component in components):
        return "recognized"
    return "ready"


def _has_component_diagnostic(component: PluginComponentReport, prefix: str) -> bool:
    """判断现有 diagnostics 是否记录了局部组件问题。"""
    return any(diagnostic.startswith(prefix) for diagnostic in component.diagnostics)


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
