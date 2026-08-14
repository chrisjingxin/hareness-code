"""Plugin 管理服务：校验、安装、显式信任启停、查询和删除。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from harness_agent.plugins.adapters import RequestedPluginFormat, load_plugin_descriptor
from harness_agent.plugins.mcp_adapter import PluginMcpLoadResult, load_plugin_mcp_servers
from harness_agent.plugins.model import (
    ExtensionCatalogSnapshot,
    InstalledPlugin,
    PluginError,
    catalog_snapshot_id,
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
        }

    def inspect(self, plugin_id: str) -> dict[str, object]:
        """查看单个 Plugin 的兼容性与 trust 状态。"""
        state = self.store.read_registry()
        plugin = _find_plugin(state, plugin_id)
        return {"revision": state.revision, "plugin": plugin.to_dict()}

    def set_enabled(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        capability_fingerprint: str | None = None,
    ) -> dict[str, object]:
        """启用时要求用户确认当前 capability fingerprint；停用立即撤销 trust。"""
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

    def remove(self, plugin_id: str, *, purge_data: bool = False) -> dict[str, object]:
        """移除 registry 记录；store 留作不可变孤儿，data 默认保留。"""
        removed: InstalledPlugin | None = None

        def _remove(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            nonlocal removed
            removed = _find_plugin(current, plugin_id)
            return tuple(plugin for plugin in current.plugins if plugin.plugin_id != plugin_id)

        state = self.store.mutate_registry(_remove)
        assert removed is not None
        data_purged = self.store.purge_data(removed) if purge_data else False
        return {
            "revision": state.revision,
            "id": plugin_id,
            "removed": True,
            "data_retained": not purge_data,
            "data_purged": data_purged,
            "catalog": self._catalog_from_state(state).to_dict(),
        }

    def catalog(self) -> ExtensionCatalogSnapshot:
        """发布只包含已启用且 trust 未失效记录的不可变 catalog。"""
        return self._catalog_from_state(self.store.read_registry())

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
                for component in plugin.components:
                    if (
                        component.kind not in {"skills", "commands"}
                        or component.status not in {"supported", "adapted"}
                    ):
                        continue
                    for relative in component.sources:
                        manifest = package_root / relative
                        if component.kind == "skills":
                            name = plugin.name if relative == "SKILL.md" else manifest.parent.name
                            dialect = "claude" if plugin.format == "claude-code" else "portable"
                            canonical_suffix = None
                            force_user_invocable = None
                            force_model_invocable = None
                        else:
                            name = manifest.stem
                            dialect = "claude-command"
                            canonical_suffix = f"command/{name}"
                            force_user_invocable = True
                            force_model_invocable = False
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
                                    if component.kind == "commands"
                                    else "skill"
                                ),
                                canonical_suffix=canonical_suffix,
                                force_user_invocable=force_user_invocable,
                                force_model_invocable=force_model_invocable,
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
    ) -> PluginMcpLoadResult:
        """从指定 catalog 转换 MCP，避免运行装配重新读取 registry。"""
        return load_plugin_mcp_servers(
            catalog,
            store=self.store,
            workspace=workspace,
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
                    if component.status not in {"supported", "adapted"}:
                        continue
                    if component.kind == "agents":
                        agent_files.extend(root / relative for relative in component.sources)
                    elif component.kind == "policies":
                        policy_files.extend(root / relative for relative in component.sources)
                if agent_files:
                    sources.append(
                        PluginAgentSource(
                            plugin_id=plugin.name,
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
                sources = tuple(
                    root / relative
                    for component in plugin.components
                    if component.kind == "teams"
                    and component.status in {"supported", "adapted"}
                    for relative in component.sources
                )
                result = load_plugin_teams(
                    root,
                    sources=sources,
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
    ) -> "PluginRuntimeCatalog":
        """从同一启动快照构造 Claude Hook/LSP/Monitor 运行目录。"""
        from harness_agent.plugins.runtime import load_plugin_runtime_catalog

        return load_plugin_runtime_catalog(
            catalog,
            store=self.store,
            workspace=workspace,
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


def _find_plugin(state: PluginRegistryState, plugin_id: str) -> InstalledPlugin:
    """按 canonical ID 查找安装记录。"""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PluginError("PLUGIN_ID_INVALID", "Plugin ID 无效")
    for plugin in state.plugins:
        if plugin.plugin_id == plugin_id:
            return plugin
    raise PluginError("PLUGIN_NOT_FOUND", f'Plugin "{plugin_id}" 不存在')
