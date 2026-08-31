"""Plugin 管理服务：校验、安装、显式信任启停、查询和删除。"""

from __future__ import annotations

import time
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from harness_agent.config.settings import (
    SettingBinding,
    SettingsError,
    parse_qwen_settings,
)
from harness_agent.plugins.adapters import RequestedPluginFormat, load_plugin_descriptor
from harness_agent.plugins.common import read_json_object
from harness_agent.plugins.mcp_adapter import PluginMcpLoadResult, load_plugin_mcp_servers
from harness_agent.plugins.model import (
    ExtensionCatalogSnapshot,
    InstalledPlugin,
    PLUGIN_ADAPTER_REPORT_REVISION,
    PluginError,
    catalog_snapshot_id,
    runtime_component_eligibility,
)
from harness_agent.plugins.resources import (
    PluginResourceSnapshot,
    build_plugin_resource_snapshot,
)
from harness_agent.plugins.store import PluginRegistryState, PluginStore


@dataclass(frozen=True, slots=True)
class PluginSkillLoadResult:
    """从同一 Plugin catalog 解析出的 Skill 来源与隔离诊断。"""

    sources: tuple["PluginSkillSource", ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginAgentLoadResult:
    """从同一 Plugin catalog 解析出的 AgentCatalog 来源与隔离诊断。"""

    sources: tuple["PluginAgentSource", ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginTeamLoadResult:
    """从同一 Plugin catalog 解析出的固定 Team 与隔离诊断。"""

    teams: tuple["TeamDefinition", ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginSettingsLoadResult:
    """从同一 enabled catalog 解析出的 Qwen Settings identity。"""

    bindings: tuple[SettingBinding, ...]
    diagnostics: tuple[str, ...]
    blocked_plugin_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginReauthorizationSummary:
    """陈旧 Plugin 的只读门禁索引；不属于可执行 runtime catalog。"""

    plugin_id: str
    authorization_state: str
    capability_fingerprint: str
    package_digest: str
    component_sources: tuple[tuple[str, str], ...] = ()
    component_ids: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ()
    team_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginCatalogRefreshResult:
    """一次 Adapter 重解析后的不可变 catalog 与授权变化摘要。"""

    catalog: ExtensionCatalogSnapshot
    changed_plugin_ids: tuple[str, ...] = ()
    reauthorization_required: tuple[str, ...] = ()
    reauthorization: tuple[PluginReauthorizationSummary, ...] = ()


class _SettingsCleanupPartial(Exception):
    """内部控制流：Settings cleanup partial 时不提交 registry mutation。"""

    def __init__(self, result: dict[str, object]) -> None:
        """保存已脱敏的 cleanup 摘要，交给 remove 返回稳定 partial 结果。"""
        self.result = result


def _qwen_command_name(relative: str) -> str:
    """把 Qwen commands 相对来源转换为自然的嵌套命令名。"""
    parts = relative.replace("\\", "/").split("/")
    if "commands" in parts:
        parts = parts[parts.index("commands") + 1 :]
    if parts and parts[-1].lower().endswith(".md"):
        parts[-1] = parts[-1][:-3]
    return ":".join(part for part in parts if part)


def _component_source_name(relative: str) -> str:
    """把报告内相对 source 转成稳定、无宿主路径的 basename。"""
    normalized = relative.replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name or "component"


def _reauthorization_summary(plugin: InstalledPlugin) -> PluginReauthorizationSummary:
    """从已校验安装报告构造 blocked component/source identity。"""
    component_sources: list[tuple[str, str]] = []
    component_ids: list[str] = []
    agent_ids: list[str] = []
    team_ids: list[str] = []
    for component in plugin.components:
        sources = component.sources or (component.kind,)
        for relative in sources:
            normalized = relative.replace("\\", "/")
            component_sources.append((component.kind, normalized))
            source_name = _component_source_name(normalized)
            if component.kind == "commands":
                command_name = (
                    _qwen_command_name(normalized)
                    if plugin.format == "qwen-code"
                    else source_name
                )
                component_ids.append(
                    f"plugin/{plugin.plugin_id}/command/{command_name}"
                )
            elif component.kind == "skills":
                if normalized == "SKILL.md":
                    skill_name = plugin.name
                else:
                    parts = normalized.split("/")
                    skill_name = parts[-2] if len(parts) > 1 else source_name
                component_ids.append(f"plugin/{plugin.plugin_id}/{skill_name}")
            elif component.kind == "agents":
                agent_ids.append(source_name)
                component_ids.append(
                    f"plugin/{plugin.plugin_id}/agent/{source_name}"
                )
            elif component.kind == "teams":
                team_ids.append(source_name)
                component_ids.append(
                    f"plugin/{plugin.plugin_id}/team/{source_name}"
                )
            else:
                component_ids.append(
                    f"plugin/{plugin.plugin_id}/{component.kind}/{source_name}"
                )
    return PluginReauthorizationSummary(
        plugin_id=plugin.plugin_id,
        authorization_state=plugin.authorization_state,
        capability_fingerprint=plugin.capability_fingerprint,
        package_digest=plugin.package_digest,
        component_sources=tuple(component_sources),
        component_ids=tuple(sorted(set(component_ids))),
        agent_ids=tuple(sorted(set(agent_ids))),
        team_ids=tuple(sorted(set(team_ids))),
    )


def _qwen_skill_resource_files(package_root: Path) -> tuple[tuple[Path, str], ...]:
    """只列出 Qwen Skill 顶层 references Markdown，不递归开发素材目录。"""
    references = package_root / "references"
    if references.is_symlink() or not references.is_dir():
        return ()
    try:
        entries = sorted(references.iterdir(), key=lambda path: path.name)
    except OSError:
        return ()
    return tuple(
        (path, f"references/{path.name}")
        for path in entries
        if not path.is_symlink()
        and path.is_file()
        and path.suffix.lower() == ".md"
    )


class PluginManager:
    """向 Server 提供与外部格式无关的 user-scope Plugin 管理 API。"""

    def __init__(self, *, home: Path | None = None) -> None:
        """绑定用户级 PluginStore。"""
        self.store = PluginStore(home=home)


    def validate(
        self,
        source: Path | str,
        *,
        format: RequestedPluginFormat = "auto",
    ) -> dict[str, object]:
        """安全复制并离线校验来源，不修改 store 或 registry。"""
        with self.store.stage(source) as staged:
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format=format,
            )
            return {
                "source": {"label": staged.source_label, "kind": "local"},
                "plugin": descriptor.to_dict(),
                "will_install_enabled": False,
            }

    def install(
        self,
        source: Path | str,
        *,
        format: RequestedPluginFormat = "auto",
    ) -> dict[str, object]:
        """校验并 copy-on-install；新版本始终以 disabled 状态进入 registry。"""
        with self.store.stage(source) as staged:
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format=format,
            )
            self.store.install_package(staged, plugin_name=descriptor.name)
            plugin_id = f"{staged.source_id}/{descriptor.name}"
            installed = InstalledPlugin(
                plugin_id=plugin_id,
                source_id=staged.source_id,
                source_label=staged.source_label,
                name=descriptor.name,
                version=descriptor.version,
                description=descriptor.description,
                format=descriptor.format,
                manifest=descriptor.manifest,
                package_digest=descriptor.package_digest,
                capability_fingerprint=descriptor.capability_fingerprint,
                components=descriptor.components,
                diagnostics=descriptor.diagnostics,
                enabled=False,
                trusted_capability_fingerprint=None,
                installed_at_ms=int(time.time() * 1000),
                adapter_revision=descriptor.adapter_revision,
            )
            resource_snapshot = build_plugin_resource_snapshot(
                installed,
                store=self.store,
            )

            def _replace(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
                return (
                    *(plugin for plugin in current.plugins if plugin.plugin_id != plugin_id),
                    installed,
                )

            state = self.store.mutate_registry(_replace)
            return {
                "revision": state.revision,
                "plugin": installed.to_dict(),
                "resource_snapshot": resource_snapshot.to_dict(),
                "effective_on": "next_host_after_enable",
            }

    def list(self, *, include_disabled: bool = True) -> dict[str, object]:
        """列出安装记录和当前 enabled catalog 摘要。"""
        state = self.store.read_registry()
        plugins = tuple(
            plugin for plugin in state.plugins if include_disabled or plugin.enabled
        )
        return {
            "revision": state.revision,
            "plugins": [plugin.to_dict() for plugin in plugins],
            "catalog": self._catalog_from_state(state).to_dict(),
            "resource_snapshots": [
                snapshot.to_dict()
                for snapshot in self.resource_snapshots(include_disabled=include_disabled)
            ],
            "static_preview": self.static_preview(include_disabled=include_disabled),
        }

    def inspect(self, plugin_id: str) -> dict[str, object]:
        """查看单个 Plugin 的兼容性与 trust 状态。"""
        state = self.store.read_registry()
        plugin = _find_plugin(state, plugin_id)
        return {
            "revision": state.revision,
            "plugin": plugin.to_dict(),
            "resource_snapshot": self.resource_snapshot(plugin_id).to_dict(),
        }

    def resource_snapshot(self, plugin_id: str) -> PluginResourceSnapshot:
        """读取已安装 Plugin 的静态资源快照，不要求启用且不进入运行时。"""
        plugin = _find_plugin(self.store.read_registry(), plugin_id)
        return build_plugin_resource_snapshot(plugin, store=self.store)

    def resource_snapshots(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[PluginResourceSnapshot, ...]:
        """按 registry 顺序返回已安装 Plugin 的脱敏资源快照。"""
        state = self.store.read_registry()
        return tuple(
            self.resource_snapshot(plugin.plugin_id)
            for plugin in state.plugins
            if include_disabled or plugin.enabled
        )

    def static_preview(
        self,
        *,
        include_disabled: bool = True,
    ) -> dict[str, list[dict[str, object]]]:
        """为尚未接入运行时的 Qwen 组件提供 disabled/static/non-runnable 预览。"""
        preview: dict[str, list[dict[str, object]]] = {
            "commands": [],
            "skills": [],
            "agents": [],
            "mcp": [],
        }
        state = self.store.read_registry()
        for plugin in state.plugins:
            if not include_disabled and not plugin.enabled:
                continue
            if plugin.format != "qwen-code":
                continue
            snapshot = self.resource_snapshot(plugin.plugin_id)
            snapshot_preview = snapshot.static_preview()
            trusted_enabled = (
                plugin.enabled
                and plugin.trusted_capability_fingerprint == plugin.capability_fingerprint
            )
            effective_sources: dict[str, set[str]] = {
                "commands": set(),
                "skills": set(),
                "agents": set(),
                "mcp": set(),
            }
            effective_mcp = False
            if trusted_enabled:
                for component in plugin.components:
                    eligibility = runtime_component_eligibility(
                        plugin,
                        kind=component.kind,
                    )
                    if eligibility.eligible and component.kind in effective_sources:
                        effective_sources[component.kind].update(component.sources)
                        if component.kind == "mcp":
                            effective_mcp = True
            for kind in preview:
                preview[kind].extend(
                    item
                    for item in snapshot_preview[kind]
                    if str(item.get("source", "")) not in effective_sources[kind]
                    and not (
                        kind == "mcp"
                        and effective_mcp
                        and "diagnostic" not in item
                    )
                )
        for items in preview.values():
            items.sort(key=lambda item: str(item["id"]))
        return preview

    def set_enabled(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        capability_fingerprint: str | None = None,
    ) -> dict[str, object]:
        """启用时要求用户确认当前 capability fingerprint；停用立即撤销 trust。"""
        if enabled:
            current = _find_plugin(self.store.read_registry(), plugin_id)
            if current.adapter_revision != PLUGIN_ADAPTER_REPORT_REVISION:
                # 旧 report 不能作为用户确认的事实来源；先在 registry lock 内用当前
                # Adapter 重建，再校验调用方传入的 fingerprint。
                self.refresh_catalog()
        updated: InstalledPlugin | None = None

        def _update(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            nonlocal updated
            plugin = _find_plugin(current, plugin_id)
            if enabled:
                if not any(component.effective for component in plugin.components):
                    raise PluginError(
                        "PLUGIN_NO_EFFECTIVE_COMPONENT",
                        "Plugin 没有可生效的 Harness 组件，无法启用",
                    )
                if capability_fingerprint != plugin.capability_fingerprint:
                    raise PluginError(
                        "PLUGIN_CAPABILITY_CONFIRMATION_REQUIRED",
                        "启用 Plugin 必须确认当前 capability_fingerprint",
                    )
                if not plugin.can_enable:
                    raise PluginError(
                        "PLUGIN_COMPONENT_INVALID",
                        "Plugin 包含无效组件，修复或更新后才能启用",
                    )
                self.store.verify_installed(plugin)
            updated = InstalledPlugin(
                plugin_id=plugin.plugin_id,
                source_id=plugin.source_id,
                source_label=plugin.source_label,
                name=plugin.name,
                version=plugin.version,
                description=plugin.description,
                format=plugin.format,
                manifest=plugin.manifest,
                package_digest=plugin.package_digest,
                capability_fingerprint=plugin.capability_fingerprint,
                components=plugin.components,
                diagnostics=plugin.diagnostics,
                enabled=enabled,
                trusted_capability_fingerprint=(
                    plugin.capability_fingerprint if enabled else None
                ),
                installed_at_ms=plugin.installed_at_ms,
                adapter_revision=plugin.adapter_revision,
            )
            return tuple(
                updated if item.plugin_id == plugin_id else item for item in current.plugins
            )

        state = self.store.mutate_registry(_update)
        assert updated is not None
        return {
            "revision": state.revision,
            "plugin": updated.to_dict(),
            "effective_on": "next_host",
            "catalog": self._catalog_from_state(state).to_dict(),
        }

    def remove(
        self,
        plugin_id: str,
        *,
        purge_data: bool = False,
        settings_cleanup: Callable[[InstalledPlugin, int], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """按 registry revision 绑定卸载 Settings，再移除记录；默认保留 data。"""
        removed: InstalledPlugin | None = None
        before = self.store.read_registry()
        removed = _find_plugin(before, plugin_id)
        settings_result: dict[str, object] | None = None
        current_state: PluginRegistryState | None = None
        data_purged = False

        def _remove(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            nonlocal current_state, data_purged, removed, settings_result
            current_state = current
            if current.revision != before.revision:
                raise PluginError("PLUGIN_REGISTRY_REVISION_CONFLICT", "Plugin registry 已变化")
            current_plugin = _find_plugin(current, plugin_id)
            if current_plugin.package_digest != removed.package_digest:
                raise PluginError("PLUGIN_REGISTRY_REVISION_CONFLICT", "Plugin identity 已变化")
            removed = current_plugin
            # Settings credential 属于成功卸载的固定清理步骤；purge_data 只
            # 决定是否额外删除 Plugin data，二者不能共用条件。
            if settings_cleanup is not None:
                settings_result = settings_cleanup(current_plugin, current.revision)
                if settings_result.get("partial"):
                    raise _SettingsCleanupPartial(settings_result)
            # 在同一 registry lock 内完成破坏性 data cleanup；若 revision 已漂移，
            # 上面的检查会先失败，避免先清理后返回 conflict。
            if purge_data:
                data_purged = self.store.purge_data(current_plugin)
            return tuple(plugin for plugin in current.plugins if plugin.plugin_id != plugin_id)

        try:
            state = self.store.mutate_registry(_remove)
        except _SettingsCleanupPartial as exc:
            stable_state = current_state or before
            return {
                "revision": stable_state.revision,
                "id": plugin_id,
                "removed": False,
                "data_retained": True,
                "data_purged": False,
                "settings_cleanup": exc.result,
                "diagnostics": ["SETTINGS_UNINSTALL_PARTIAL"],
                "catalog": self._catalog_from_state(stable_state).to_dict(),
            }
        assert removed is not None
        result: dict[str, object] = {
            "revision": state.revision,
            "id": plugin_id,
            "removed": True,
            "data_retained": not purge_data,
            "data_purged": data_purged,
            "catalog": self._catalog_from_state(state).to_dict(),
        }
        if settings_result is not None:
            result["settings_cleanup"] = settings_result
        return result

    def catalog(self) -> ExtensionCatalogSnapshot:
        """发布只包含已启用且 trust 未失效记录的不可变 catalog。"""
        return self._catalog_from_state(self.store.read_registry())

    def refresh_catalog(self) -> PluginCatalogRefreshResult:
        """用当前 Adapter 重解析已校验 package，再发布可运行 catalog。

        解析发生在 registry 文件锁内，package digest、Plugin identity 和原有 trust
        均不会被重解析过程替换。能力指纹变化只保留旧 trust，交由显式 enable 重新确认。
        """
        changed_plugin_ids: set[str] = set()
        initial = self.store.read_registry()
        if (
            initial.revision == 0
            and not initial.plugins
            and not self.store.registry_path.exists()
        ):
            # 空的默认 Host 不应因为探测 Plugin 而创建 user-scope lock；有
            # registry 或已安装记录时才进入跨进程刷新事务。
            return PluginCatalogRefreshResult(catalog=self._catalog_from_state(initial))

        def _refresh(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            refreshed: list[InstalledPlugin] = []
            for plugin in current.plugins:
                self.store.verify_installed(plugin)
                descriptor = load_plugin_descriptor(
                    self.store.package_path(plugin),
                    package_digest=plugin.package_digest,
                    name_hint=plugin.name,
                    requested_format=_adapter_requested_format(plugin),
                )
                if (
                    descriptor.name != plugin.name
                    or descriptor.format != plugin.format
                    or descriptor.package_digest != plugin.package_digest
                    or descriptor.adapter_revision
                    != PLUGIN_ADAPTER_REPORT_REVISION
                ):
                    raise PluginError(
                        "PLUGIN_ADAPTER_IDENTITY_MISMATCH",
                        "当前 Adapter 返回的 Plugin identity 无法绑定已安装 package",
                    )
                next_plugin = replace(
                    plugin,
                    version=descriptor.version,
                    description=descriptor.description,
                    manifest=descriptor.manifest,
                    capability_fingerprint=descriptor.capability_fingerprint,
                    components=descriptor.components,
                    diagnostics=descriptor.diagnostics,
                    adapter_revision=descriptor.adapter_revision,
                )
                if next_plugin != plugin:
                    changed_plugin_ids.add(plugin.plugin_id)
                refreshed.append(next_plugin)
            return tuple(refreshed)

        state = self.store.mutate_registry_if_changed(_refresh)
        reauthorization_required = tuple(
            plugin.plugin_id
            for plugin in state.plugins
            if plugin.enabled
            and plugin.trusted_capability_fingerprint != plugin.capability_fingerprint
        )
        reauthorization = tuple(
            _reauthorization_summary(plugin)
            for plugin in state.plugins
            if plugin.plugin_id in reauthorization_required
        )
        return PluginCatalogRefreshResult(
            catalog=self._catalog_from_state(state),
            changed_plugin_ids=tuple(sorted(changed_plugin_ids)),
            reauthorization_required=reauthorization_required,
            reauthorization=reauthorization,
        )

    def setting_bindings(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> PluginSettingsLoadResult:
        """从已启用 Qwen catalog 读取严格 ExtensionSetting，不读取 setting value。"""
        return self._load_setting_bindings(
            tuple((catalog or self.catalog()).plugins),
            require_runtime=True,
        )

    def setting_bindings_for_uninstall(
        self,
        plugin: InstalledPlugin,
    ) -> PluginSettingsLoadResult:
        """按已安装记录解析卸载 identity，不受 enabled/trusted runtime 过滤。"""
        return self._load_setting_bindings((plugin,), require_runtime=False)

    def _load_setting_bindings(
        self,
        plugins: tuple[InstalledPlugin, ...],
        *,
        require_runtime: bool,
    ) -> PluginSettingsLoadResult:
        """从指定安装记录解析 Settings；runtime 与 uninstall 共享唯一声明解析。"""
        bindings: list[SettingBinding] = []
        diagnostics: list[str] = []
        blocked_plugin_ids: set[str] = set()
        for plugin in plugins:
            if plugin.format != "qwen-code" or plugin.manifest is None:
                continue
            try:
                self.store.verify_installed(plugin)
                root = self.store.package_path(plugin)
                manifest = read_json_object(root, plugin.manifest)
                raw_settings = manifest.get("settings")
                if raw_settings is None:
                    continue
                declarations = parse_qwen_settings(raw_settings)
                if not declarations:
                    continue
                if require_runtime:
                    component = next(
                        (item for item in plugin.components if item.kind == "settings"),
                        None,
                    )
                    eligibility = runtime_component_eligibility(plugin, kind="settings")
                    if not eligibility.eligible:
                        blocked_plugin_ids.add(plugin.plugin_id)
                        if eligibility.diagnostic is not None:
                            diagnostics.append(
                                f"plugin:{plugin.plugin_id}: {eligibility.diagnostic}"
                            )
                        continue
                    if component is None or not component.effective:
                        blocked_plugin_ids.add(plugin.plugin_id)
                        diagnostics.append(
                            f"plugin:{plugin.plugin_id}: SETTINGS_DECLARATION_INVALID"
                        )
                        continue
                    if tuple(component.sources) != tuple(item.env_var for item in declarations):
                        blocked_plugin_ids.add(plugin.plugin_id)
                        diagnostics.append(
                            f"plugin:{plugin.plugin_id}: SETTINGS_DECLARATION_STALE"
                        )
                        continue
                bindings.extend(
                    SettingBinding(
                        plugin_id=plugin.plugin_id,
                        package_digest=plugin.package_digest,
                        declaration_digest=declaration.declaration_digest,
                        declaration=declaration,
                    )
                    for declaration in declarations
                )
            except (PluginError, SettingsError) as exc:
                code = exc.code if isinstance(exc, (PluginError, SettingsError)) else "SETTINGS_DECLARATION_INVALID"
                blocked_plugin_ids.add(plugin.plugin_id)
                diagnostics.append(f"plugin:{plugin.plugin_id}: {code}")
        return PluginSettingsLoadResult(
            tuple(bindings),
            tuple(diagnostics),
            tuple(sorted(blocked_plugin_ids)),
        )

    def skill_sources(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> PluginSkillLoadResult:
        """从一个 enabled catalog 生成显式 Skill 来源，并隔离损坏 Plugin。"""
        from harness_agent.extensions.plugin_skills import PluginSkillSource

        sources: list[PluginSkillSource] = []
        diagnostics: list[str] = []
        for plugin in (catalog or self.catalog()).plugins:
            try:
                self.store.verify_installed(plugin)
                package_root = self.store.package_path(plugin)
                for kind in ("skills", "commands"):
                    component = next(
                        (item for item in plugin.components if item.kind == kind),
                        None,
                    )
                    if component is None:
                        if _plugin_component_declared_for_skill_runtime(
                            plugin,
                            package_root,
                            kind,
                        ):
                            eligibility = runtime_component_eligibility(
                                plugin,
                                kind=kind,
                            )
                            if eligibility.diagnostic is not None:
                                diagnostics.append(
                                    f"plugin:{plugin.plugin_id}: {eligibility.diagnostic}"
                                )
                        continue
                    eligibility = runtime_component_eligibility(
                        plugin,
                        kind=kind,
                    )
                    if not eligibility.eligible:
                        if eligibility.diagnostic is not None:
                            diagnostics.append(
                                f"plugin:{plugin.plugin_id}: {eligibility.diagnostic}"
                            )
                        continue
                    for relative in component.sources:
                        manifest = package_root / relative
                        if kind == "skills":
                            name = plugin.name if relative == "SKILL.md" else manifest.parent.name
                            dialect = (
                                "qwen-skill"
                                if plugin.format == "qwen-code"
                                else ("claude" if plugin.format == "claude-code" else "portable")
                            )
                            canonical_suffix = None
                            force_user_invocable = None
                            force_model_invocable = None
                            resource_files = (
                                _qwen_skill_resource_files(package_root)
                                if plugin.format == "qwen-code"
                                else ()
                            )
                        else:
                            name = (
                                _qwen_command_name(relative)
                                if plugin.format == "qwen-code"
                                else manifest.stem
                            )
                            dialect = (
                                "qwen-command"
                                if plugin.format == "qwen-code"
                                else "claude-command"
                            )
                            canonical_suffix = f"command/{name}"
                            force_user_invocable = True
                            force_model_invocable = False
                            resource_files = ()
                        sources.append(
                            PluginSkillSource(
                                plugin_id=plugin.plugin_id,
                                name=name,
                                root=manifest.parent,
                                manifest=manifest,
                                dialect=dialect,
                                version=plugin.version,
                                package_digest=plugin.package_digest,
                                kind=(
                                    "command"
                                    if kind == "commands"
                                    else "skill"
                                ),
                                canonical_suffix=canonical_suffix,
                                force_user_invocable=force_user_invocable,
                                force_model_invocable=force_model_invocable,
                                resource_files=resource_files,
                            )
                        )
            except PluginError as exc:
                diagnostics.append(f"plugin:{plugin.plugin_id}: {exc.code}: {exc}")
        return PluginSkillLoadResult(
            sources=tuple(sources),
            diagnostics=tuple(diagnostics),
        )
    def mcp_servers(
        self,
        catalog: ExtensionCatalogSnapshot,
        *,
        workspace: Path,
        blocked_plugin_ids: tuple[str, ...] = (),
    ) -> PluginMcpLoadResult:
        """从指定 catalog 转换 MCP，避免运行装配重新读取 registry。"""
        return load_plugin_mcp_servers(
            catalog,
            store=self.store,
            workspace=workspace,
            blocked_plugin_ids=blocked_plugin_ids,
        )

    def agent_sources(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> PluginAgentLoadResult:
        """把 enabled catalog 中的 Agent/Policy 文件交给 canonical AgentCatalog。"""
        from harness_agent.runtime.agent_catalog import PluginAgentSource

        sources: list[PluginAgentSource] = []
        diagnostics: list[str] = []
        for plugin in (catalog or self.catalog()).plugins:
            try:
                self.store.verify_installed(plugin)
                root = self.store.package_path(plugin)
                agent_files: list[Path] = []
                policy_files: list[Path] = []
                for component in plugin.components:
                    if component.kind not in {"agents", "policies"}:
                        continue
                    eligibility = runtime_component_eligibility(
                        plugin,
                        kind=component.kind,
                    )
                    if not eligibility.eligible:
                        if eligibility.diagnostic is not None:
                            diagnostics.append(
                                f"plugin:{plugin.plugin_id}: {eligibility.diagnostic}"
                            )
                        continue
                    if component.kind == "agents":
                        agent_files.extend(root / relative for relative in component.sources)
                    elif component.kind == "policies":
                        policy_files.extend(root / relative for relative in component.sources)
                if agent_files:
                    sources.append(
                        PluginAgentSource(
                            plugin_id=(
                                _agent_source_id(plugin)
                                if plugin.format == "qwen-code"
                                else plugin.name
                            ),
                            root=root,
                            format=plugin.format,
                            agent_files=tuple(sorted(set(agent_files))),
                            policy_files=tuple(sorted(set(policy_files))),
                            package_digest=plugin.package_digest,
                        )
                    )
            except (PluginError, ValueError) as exc:
                code = exc.code if isinstance(exc, PluginError) else "PLUGIN_AGENT_INVALID"
                diagnostics.append(f"plugin:{plugin.plugin_id}: {code}: {exc}")
        return PluginAgentLoadResult(
            sources=tuple(sources),
            diagnostics=tuple(diagnostics),
        )

    def context_blocks_by_source(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> dict[str, tuple["ContextBlock", ...]]:
        """从 enabled+trusted Qwen 资源快照读取 canonical 稳定参考块。"""
        from harness_agent.threads.context_lifecycle import (
            ContextAuthority,
            ContextBlock,
            ContextStability,
        )

        result: dict[str, tuple[ContextBlock, ...]] = {}
        for plugin in (catalog or self.catalog()).plugins:
            if plugin.format != "qwen-code":
                continue
            context_gate = runtime_component_eligibility(plugin, kind="contexts")
            if not context_gate.eligible:
                continue
            self.store.verify_installed(plugin)
            snapshot = build_plugin_resource_snapshot(plugin, store=self.store)
            source = f"plugin:{_agent_source_id(plugin)}"
            blocks: list[ContextBlock] = []
            for index, asset in enumerate(
                asset for asset in snapshot.resources if asset.kind == "contexts"
            ):
                blocks.append(
                    ContextBlock(
                        key=f"plugin.context.{_agent_source_id(plugin)}.{index:04d}",
                        authority=ContextAuthority.REFERENCE,
                        stability=ContextStability.STABLE,
                        content=(
                            f"来源: {asset.virtual_path}\n"
                            + snapshot.read_text(asset.virtual_path)
                        ),
                    )
                )
            if blocks:
                result[source] = tuple(blocks)
        return result

    def context_blocks(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> tuple["ContextBlock", ...]:
        """返回当前 trusted Plugin 的稳定参考块，供主 Agent 快照使用。"""
        by_source = self.context_blocks_by_source(catalog)
        return tuple(block for blocks in by_source.values() for block in blocks)

    def team_definitions(
        self,
        catalog: ExtensionCatalogSnapshot | None = None,
    ) -> PluginTeamLoadResult:
        """读取 Harness extension Team 文件并转换为固定 DAG 定义。"""
        from harness_agent.plugins.team_adapter import load_plugin_teams

        teams = []
        diagnostics: list[str] = []
        for plugin in (catalog or self.catalog()).plugins:
            try:
                self.store.verify_installed(plugin)
                root = self.store.package_path(plugin)
                sources: list[Path] = []
                for component in plugin.components:
                    if component.kind != "teams":
                        continue
                    eligibility = runtime_component_eligibility(
                        plugin,
                        kind="teams",
                    )
                    if not eligibility.eligible:
                        if eligibility.diagnostic is not None:
                            diagnostics.append(
                                f"plugin:{plugin.plugin_id}: {eligibility.diagnostic}"
                            )
                        continue
                    sources.extend(root / relative for relative in component.sources)
                result = load_plugin_teams(
                    root,
                    sources=tuple(sources),
                    plugin_id=plugin.plugin_id,
                )
                teams.extend(result.teams)
                diagnostics.extend(result.diagnostics)
            except PluginError as exc:
                diagnostics.append(f"plugin:{plugin.plugin_id}: {exc.code}: {exc}")
        return PluginTeamLoadResult(
            teams=tuple(teams),
            diagnostics=tuple(diagnostics),
        )

    def runtime_catalog(
        self,
        catalog: ExtensionCatalogSnapshot,
        *,
        workspace: Path,
        blocked_plugin_ids: tuple[str, ...] = (),
    ) -> "PluginRuntimeCatalog":
        """从同一启动快照构造 Claude/Qwen Hook 与 LSP/Monitor 运行目录。"""
        from harness_agent.plugins.runtime import load_plugin_runtime_catalog

        return load_plugin_runtime_catalog(
            catalog,
            store=self.store,
            workspace=workspace,
            blocked_plugin_ids=blocked_plugin_ids,
        )

    def package_root(self, plugin_id: str) -> Path:
        """仅供后续生产装配按 catalog ID 获取已校验 store 根目录。"""
        state = self.store.read_registry()
        plugin = _find_plugin(state, plugin_id)
        if not plugin.enabled or (
            plugin.trusted_capability_fingerprint != plugin.capability_fingerprint
        ):
            raise PluginError("PLUGIN_NOT_ENABLED", "Plugin 尚未启用或 trust 已失效")
        self.store.verify_installed(plugin)
        return self.store.package_path(plugin)

    def _catalog_from_state(self, state: PluginRegistryState) -> ExtensionCatalogSnapshot:
        """过滤 enabled + trusted 记录并计算 snapshot ID。"""
        plugins = tuple(
            plugin
            for plugin in state.plugins
            if plugin.enabled
            and plugin.can_enable
            and plugin.trusted_capability_fingerprint == plugin.capability_fingerprint
        )
        return ExtensionCatalogSnapshot(
            snapshot_id=catalog_snapshot_id(state.revision, plugins),
            registry_revision=state.revision,
            plugins=plugins,
        )


def _plugin_component_declared_for_skill_runtime(
    plugin: InstalledPlugin,
    root: Path,
    kind: str,
) -> bool:
    """判断包内是否声明了 Command/Skill，避免干净包产生缺报告噪声。"""
    qwen_manifest_present = any(
        (root / manifest_name).is_file()
        for manifest_name in ("qwen-extension.json", "devagent-extension.json")
    )
    if kind == "skills" and (
        plugin.format in {"agent-plugins-1.0", "claude-code", "hybrid"}
        and ((root / "skills").exists() or (root / "skills").is_symlink())
    ):
        return True
    if kind == "skills" and plugin.format == "qwen-code" and (
        qwen_manifest_present
        and ((root / "skills").exists() or (root / "skills").is_symlink())
    ):
        return True
    if kind == "skills" and plugin.format in {"claude-code", "hybrid"} and (
        (root / "SKILL.md").exists() or (root / "SKILL.md").is_symlink()
    ):
        return True
    if kind == "commands" and plugin.format in {"claude-code", "hybrid"} and (
        (root / "commands").exists() or (root / "commands").is_symlink()
    ):
        return True
    if kind == "commands" and plugin.format == "qwen-code" and (
        qwen_manifest_present
        and ((root / "commands").exists() or (root / "commands").is_symlink())
    ):
        return True

    manifest_paths: list[str] = []
    if plugin.format in {"agent-plugins-1.0", "hybrid"}:
        manifest_paths.append("plugin.json")
    if plugin.format in {"claude-code", "hybrid"}:
        manifest_paths.append(".claude-plugin/plugin.json")
    if plugin.format == "qwen-code":
        manifest_paths.extend(("qwen-extension.json", "devagent-extension.json"))

    for relative in manifest_paths:
        path = root / relative
        if not path.is_file():
            continue
        manifest = read_json_object(root, relative)
        if relative == "plugin.json" and plugin.format in {
            "agent-plugins-1.0",
            "hybrid",
        }:
            extensions = manifest.get("extensions")
            harness = (
                extensions.get("com.za38.harness")
                if isinstance(extensions, Mapping)
                else None
            )
            if isinstance(harness, Mapping) and kind in harness:
                return True
        if kind in manifest:
            return True
    return False


def _adapter_requested_format(plugin: InstalledPlugin) -> RequestedPluginFormat:
    """按已安装格式固定 Adapter，hybrid 保留自动合并语义。"""
    if plugin.format == "hybrid":
        return "auto"
    return plugin.format  # type: ignore[return-value]


def _find_plugin(state: PluginRegistryState, plugin_id: str) -> InstalledPlugin:
    """按 canonical ID 查找安装记录。"""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PluginError("PLUGIN_ID_INVALID", "Plugin ID 无效")
    for plugin in state.plugins:
        if plugin.plugin_id == plugin_id:
            return plugin
    raise PluginError("PLUGIN_NOT_FOUND", f'Plugin "{plugin_id}" 不存在')


def _agent_source_id(plugin: InstalledPlugin) -> str:
    """将可含大写、句点或斜杠的 Plugin 身份收敛为 catalog 安全 ID。"""
    raw = f"{plugin.source_id}-{plugin.name}-{plugin.package_digest[:12]}".lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"plugin-{normalized}"
    return normalized[:64].rstrip("-")
