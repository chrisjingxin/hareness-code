"""Plugin 管理服务：本地安装、名称/作用域 activation 与状态投影。"""

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
    component_warning_diagnostics,
    plugin_warnings,
    product_status,
    runtime_component_eligibility,
)
from harness_agent.plugins.resources import (
    PluginResourceSnapshot,
    build_plugin_resource_snapshot,
)
from harness_agent.plugins.store import (
    PluginRegistryState,
    PluginStore,
    plugin_workspace_binding_digest,
)


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
class PluginCatalogRefreshResult:
    """一次 Adapter 重解析后的不可变 catalog 与变化摘要。"""

    catalog: ExtensionCatalogSnapshot
    changed_plugin_ids: tuple[str, ...] = ()


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
    """向 Host 提供与外部格式无关的本地 Plugin 管理 API。"""

    def __init__(self, *, home: Path | None = None, workspace: Path | None = None) -> None:
        """绑定用户级 PluginStore 与默认 workspace。"""
        self.store = PluginStore(home=home)
        self.workspace = workspace.expanduser().resolve() if workspace is not None else None

    def _workspace_digest(
        self,
        workspace: Path | None,
        *,
        strict: bool = False,
    ) -> str | None:
        """把可选 workspace 转成 activation digest；显式 workspace 才严格要求存在。"""
        selected = workspace or self.workspace
        if selected is None:
            return None
        return plugin_workspace_binding_digest(selected, strict=strict)

    @staticmethod
    def _scope(scope: str | None) -> str:
        """校验 Plugin 管理作用域。"""
        selected = scope or "user"
        if selected not in {"user", "workspace"}:
            raise PluginError("PLUGIN_SCOPE_INVALID", "Plugin scope 只能是 user 或 workspace")
        return selected

    def _activation_digest_for_scope(
        self,
        scope: str,
        workspace: Path | None,
    ) -> str | None:
        """解析 scope 所需 workspace；workspace scope 缺失时 fail closed。"""
        selected = self._scope(scope)
        if selected == "user":
            return None
        digest = self._workspace_digest(workspace, strict=True)
        if digest is None:
            raise PluginError("PLUGIN_SCOPE_INVALID", "workspace scope 需要当前 workspace")
        return digest

    def _state(self) -> PluginRegistryState:
        """读取 registry，并在 Adapter revision 过期时重解析当前 package。"""
        state = self.store.read_registry()
        if any(
            plugin.adapter_revision != PLUGIN_ADAPTER_REPORT_REVISION
            for plugin in state.plugins
        ):
            self.refresh_catalog()
            state = self.store.read_registry()
        return state

    @staticmethod
    def _assert_no_name_conflict(state: PluginRegistryState) -> None:
        """名称索引有歧义时禁止猜测目标。"""
        if state.name_conflicts:
            raise PluginError("PLUGIN_NAME_CONFLICT", "Plugin 名称大小写不敏感地发生冲突")

    @staticmethod
    def _plugin_summary(
        plugin: InstalledPlugin,
        *,
        activation: str,
        scope: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, object]:
        """构造唯一的公开 Plugin summary；internal locator 只供高级 inspect。"""
        components = [
            component.to_public_dict()
            for component in plugin.components
            if component.effective
        ]
        result: dict[str, object] = {
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "format": plugin.format,
            "source": {"label": plugin.source_label, "kind": "local"},
            "activation": activation,
            "status": product_status(plugin, activation=activation),
            "components": components,
            "warnings": list(plugin_warnings(plugin)),
        }
        if scope is not None:
            result["scope"] = scope
        if include_internal:
            result["internal"] = {"id": plugin.plugin_id}
        return result

    @staticmethod
    def _validation_summary(descriptor: object) -> dict[str, object]:
        """把离线 Adapter 结果裁剪为 validate 专用公开摘要。"""
        components = [
            component.to_public_dict()
            for component in descriptor.components
            if component.effective
        ]
        warnings = list(descriptor.diagnostics)
        for component in descriptor.components:
            if not component.effective or component.status not in {"supported", "adapted"}:
                warnings.extend(component.diagnostics)
            else:
                warnings.extend(component_warning_diagnostics(component))
        return {
            "name": descriptor.name,
            "version": descriptor.version,
            "description": descriptor.description,
            "format": descriptor.format,
            "components": components,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def _find_by_name(
        self,
        state: PluginRegistryState,
        name: str,
    ) -> InstalledPlugin:
        """通过大小写不敏感名称查找唯一 artifact。"""
        self._assert_no_name_conflict(state)
        if not isinstance(name, str) or not name.strip():
            raise PluginError("PLUGIN_NAME_INVALID", "Plugin name 无效")
        matches = [plugin for plugin in state.plugins if plugin.name.casefold() == name.casefold()]
        if not matches:
            raise PluginError("PLUGIN_NOT_FOUND", f'Plugin "{name}" 不存在')
        if len(matches) != 1:
            raise PluginError("PLUGIN_NAME_CONFLICT", "Plugin 名称大小写不敏感地发生冲突")
        return matches[0]

    @staticmethod
    def _public_components(plugin: InstalledPlugin) -> list[dict[str, object]]:
        """只返回实际进入 canonical consumer 的组件。"""
        return [
            component.to_public_dict()
            for component in plugin.components
            if component.effective
        ]

    def _mutation_result(
        self,
        *,
        state: PluginRegistryState,
        plugin: InstalledPlugin,
        operation: str,
        scope: str | None = None,
        workspace: Path | None = None,
        include_internal: bool = False,
    ) -> dict[str, object]:
        """统一生成 mutation response，避免重新暴露 digest/revision 身份。"""
        if scope is None:
            # update 是 artifact-level 操作；结果仍可展示当前 Host 的有效 activation，
            # 但不存在的 workspace 不能阻塞这个不带 scope 的管理动作。
            activation_digest = self._workspace_digest(workspace)
        else:
            activation_digest = self._activation_digest_for_scope(scope, workspace)
        activation = plugin.activation_for(activation_digest)
        summary = self._plugin_summary(
            plugin,
            activation=activation,
            scope=scope,
            include_internal=include_internal,
        )
        return {
            "operation": operation,
            "name": plugin.name,
            "status": summary["status"],
            "components": summary["components"],
            "warnings": summary["warnings"],
            "plugin": summary,
        }


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
                "plugin": self._validation_summary(descriptor),
                "operation": "validate",
            }

    def install(
        self,
        source: Path | str,
        *,
        scope: str = "user",
        workspace: Path | None = None,
        expected_package_digest: str | None = None,
    ) -> dict[str, object]:
        """校验并 copy-on-install；安装一次即启用所选 scope。"""
        selected_scope = self._scope(scope)
        workspace_digest = self._activation_digest_for_scope(selected_scope, workspace)
        with self.store.stage(source) as staged:
            if (
                expected_package_digest is not None
                and staged.package_digest != expected_package_digest
            ):
                raise PluginError(
                    "PLUGIN_OPERATION_CONFLICT",
                    "Plugin 来源已在 consent 后改变，请重试",
                )
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format="auto",
            )
            current = self._state()
            self._assert_no_name_conflict(current)
            if any(
                plugin.name.casefold() == descriptor.name.casefold()
                for plugin in current.plugins
            ):
                raise PluginError(
                    "PLUGIN_ALREADY_INSTALLED",
                    f'Plugin "{descriptor.name}" 已安装，请使用 update',
                )
            self.store.install_package(staged, plugin_name=descriptor.name)
            plugin_id = f"{staged.source_id}/{descriptor.name}"
            activation_user = "disabled" if selected_scope == "workspace" else "enabled"
            activation_workspaces = (
                ((workspace_digest, "enabled"),)
                if workspace_digest is not None
                else ()
            )
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
                components=descriptor.components,
                diagnostics=descriptor.diagnostics,
                activation_user=activation_user,  # type: ignore[arg-type]
                activation_workspaces=activation_workspaces,  # type: ignore[arg-type]
                installed_at_ms=int(time.time() * 1000),
                adapter_revision=descriptor.adapter_revision,
                origin=staged.origin,
            )

            def _replace(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
                self._assert_no_name_conflict(current)
                if any(
                    plugin.name.casefold() == installed.name.casefold()
                    for plugin in current.plugins
                ):
                    raise PluginError(
                        "PLUGIN_ALREADY_INSTALLED",
                        f'Plugin "{installed.name}" 已安装，请使用 update',
                    )
                return (
                    *current.plugins,
                    installed,
                )

            state = self.store.mutate_registry(_replace)
            return self._mutation_result(
                state=state,
                plugin=installed,
                operation="install",
                scope=selected_scope,
                workspace=workspace,
            )

    def list(
        self,
        *,
        scope: str = "user",
        workspace: Path | None = None,
        include_disabled: bool = True,
    ) -> dict[str, object]:
        """按 user 或显式 workspace scope 列出唯一的产品状态。"""
        selected_scope = self._scope(scope)
        workspace_digest = (
            self._workspace_digest(workspace, strict=True)
            if selected_scope == "workspace"
            else None
        )
        state = self._state()
        self._assert_no_name_conflict(state)
        plugins = tuple(
            plugin
            for plugin in state.plugins
            if include_disabled
            or plugin.activation_for(workspace_digest)
            == "enabled"
        )
        return {
            "scope": selected_scope,
            "plugins": [
                self._plugin_summary(
                    plugin,
                    activation=plugin.activation_for(workspace_digest),
                    scope=selected_scope,
                )
                for plugin in plugins
            ],
        }

    def inspect(
        self,
        name: str,
        *,
        scope: str = "user",
        workspace: Path | None = None,
    ) -> dict[str, object]:
        """按名称查看 user 或显式 workspace scope 的状态与 warning。"""
        selected_scope = self._scope(scope)
        workspace_digest = (
            self._workspace_digest(workspace, strict=True)
            if selected_scope == "workspace"
            else None
        )
        state = self._state()
        plugin = self._find_by_name(state, name)
        activation = plugin.activation_for(workspace_digest)
        return {
            "scope": selected_scope,
            "plugin": self._plugin_summary(
                plugin,
                activation=activation,
                scope=selected_scope,
                include_internal=True,
            ),
        }

    def resource_snapshot(self, plugin_id: str) -> PluginResourceSnapshot:
        """读取已安装 Plugin 的静态资源快照，不要求启用且不进入运行时。"""
        plugin = next(
            (item for item in self._state().plugins if item.plugin_id == plugin_id),
            None,
        )
        if plugin is None:
            raise PluginError("PLUGIN_NOT_FOUND", f'Plugin "{plugin_id}" 不存在')
        return build_plugin_resource_snapshot(plugin, store=self.store)

    def resource_snapshots(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[PluginResourceSnapshot, ...]:
        """按 registry 顺序返回已安装 Plugin 的脱敏资源快照。"""
        state = self._state()
        return tuple(
            self.resource_snapshot(plugin.plugin_id)
            for plugin in state.plugins
            if include_disabled or plugin.activation_user == "enabled"
        )

    def preview_install(
        self,
        source: Path | str,
        *,
        scope: str = "user",
        workspace: Path | None = None,
    ) -> dict[str, object]:
        """生成 install consent 所需的有界预览，不写入 registry。"""
        preview, _package_digest = self._preview_install_with_identity(
            source,
            scope=scope,
            workspace=workspace,
        )
        return preview

    def _preview_install_with_identity(
        self,
        source: Path | str,
        *,
        scope: str = "user",
        workspace: Path | None = None,
    ) -> tuple[dict[str, object], str]:
        """生成预览及内部 package identity，供 Host 绑定 consent。"""
        selected_scope = self._scope(scope)
        self._activation_digest_for_scope(selected_scope, workspace)
        with self.store.stage(source) as staged:
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format="auto",
            )
            preview = self._build_mutation_preview(
                operation="install",
                descriptor=descriptor,
                source_label=staged.source_label,
                scope=selected_scope,
                root=staged.root,
            )
            return preview, staged.package_digest

    def preview_update(
        self,
        name: str,
        *,
        source: Path | str | None = None,
    ) -> dict[str, object]:
        """生成 update consent 预览；缺省时读取已保存的本地 origin。"""
        preview, _old_digest, _package_digest = self._preview_update_with_identity(
            name,
            source=source,
        )
        return preview

    def _preview_update_with_identity(
        self,
        name: str,
        *,
        source: Path | str | None = None,
    ) -> tuple[dict[str, object], str, str]:
        """生成预览及新旧 artifact identity，供 Host 绑定 consent。"""
        current = self._find_by_name(self._state(), name)
        selected_source = self._update_source(current, source)
        with self.store.stage(selected_source) as staged:
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format="auto",
            )
            self._assert_update_name(current, descriptor.name)
            preview = self._build_mutation_preview(
                operation="update",
                descriptor=descriptor,
                source_label=staged.source_label,
                old_version=current.version,
                root=staged.root,
            )
            return preview, current.package_digest, staged.package_digest

    def update(
        self,
        name: str,
        *,
        source: Path | str | None = None,
        settings_rebind: Callable[[InstalledPlugin, InstalledPlugin], dict[str, object]] | None = None,
        expected_old_package_digest: str | None = None,
        expected_package_digest: str | None = None,
    ) -> dict[str, object]:
        """替换同名 artifact，保留全部 activation；正常入口固定 auto Adapter。"""
        before = self._state()
        old = self._find_by_name(before, name)
        if (
            expected_old_package_digest is not None
            and old.package_digest != expected_old_package_digest
        ):
            raise PluginError(
                "PLUGIN_OPERATION_CONFLICT",
                "Plugin artifact 已在 consent 后改变，请重试",
            )
        selected_source = self._update_source(old, source)
        with self.store.stage(selected_source) as staged:
            if (
                expected_package_digest is not None
                and staged.package_digest != expected_package_digest
            ):
                raise PluginError(
                    "PLUGIN_OPERATION_CONFLICT",
                    "Plugin 来源已在 consent 后改变，请重试",
                )
            descriptor = load_plugin_descriptor(
                staged.root,
                package_digest=staged.package_digest,
                name_hint=staged.name_hint,
                requested_format="auto",
            )
            self._assert_update_name(old, descriptor.name)
            self.store.install_package(staged, plugin_name=descriptor.name)
            updated = InstalledPlugin(
                plugin_id=old.plugin_id,
                source_id=staged.source_id,
                source_label=staged.source_label,
                name=old.name,
                version=descriptor.version,
                description=descriptor.description,
                format=descriptor.format,
                manifest=descriptor.manifest,
                package_digest=descriptor.package_digest,
                components=descriptor.components,
                diagnostics=descriptor.diagnostics,
                activation_user=old.activation_user,
                activation_workspaces=old.activation_workspaces,
                installed_at_ms=old.installed_at_ms,
                adapter_revision=descriptor.adapter_revision,
                origin=staged.origin,
            )
            rebind_result: dict[str, object] | None = None

            def _replace(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
                current_plugin = self._find_by_name(current, old.name)
                if current.revision != before.revision or current_plugin.plugin_id != old.plugin_id:
                    raise PluginError("PLUGIN_OPERATION_CONFLICT", "Plugin registry 已变化，请重试")
                if settings_rebind is not None:
                    # 先在 registry lock 内复核 artifact identity，再重绑内部
                    # Settings；并发 update 不应在失败前污染 credential binding。
                    nonlocal rebind_result
                    rebind_result = settings_rebind(current_plugin, updated)
                return tuple(
                    updated if item.plugin_id == old.plugin_id else item
                    for item in current.plugins
                )

            state = self.store.mutate_registry(_replace)
            result = self._mutation_result(
                state=state,
                plugin=updated,
                operation="update",
                workspace=self.workspace,
            )
            if rebind_result is not None:
                result["warnings"] = list(
                    dict.fromkeys(
                        (
                            *result["warnings"],
                            *rebind_result.get("warnings", []),
                        )
                    )
                )
            return result

    def _update_source(
        self,
        plugin: InstalledPlugin,
        source: Path | str | None,
    ) -> Path | str:
        """解析 update 的显式 source 或已保存 local origin。"""
        if source is not None:
            return source
        if plugin.origin is None or not Path(plugin.origin).exists():
            raise PluginError("PLUGIN_SOURCE_UNAVAILABLE", "Plugin 本地 update 来源不可用")
        return plugin.origin

    @staticmethod
    def _assert_update_name(old: InstalledPlugin, new_name: str) -> None:
        """update 不得把现有 artifact 变成另一个名称。"""
        if old.name.casefold() != new_name.casefold():
            raise PluginError("PLUGIN_NAME_CONFLICT", "update 不能改变 Plugin name")

    @staticmethod
    def _build_mutation_preview(
        *,
        operation: str,
        descriptor: object,
        source_label: str,
        scope: str | None = None,
        old_version: str | None = None,
        root: Path,
    ) -> dict[str, object]:
        """把 Adapter 事实裁剪成 consent 可展示的摘要。"""
        assert hasattr(descriptor, "name")
        plugin_descriptor = descriptor
        components = [
            component.to_public_dict()
            for component in plugin_descriptor.components
            if component.effective
        ]
        warnings = list(plugin_descriptor.diagnostics)
        for component in plugin_descriptor.components:
            if not component.effective or component.status not in {"supported", "adapted"}:
                warnings.extend(component.diagnostics)
            else:
                warnings.extend(component_warning_diagnostics(component))
        settings: list[dict[str, object]] = []
        if plugin_descriptor.format == "qwen-code" and plugin_descriptor.manifest:
            try:
                manifest = read_json_object(root, plugin_descriptor.manifest)
                raw = manifest.get("settings")
                if raw is not None:
                    settings = [
                        {
                            "name": declaration.name,
                            "description": declaration.description,
                            "required": False,
                            "configured_at_scope": scope,
                        }
                        for declaration in parse_qwen_settings(raw)
                    ]
            except (PluginError, SettingsError) as exc:
                warnings.append(exc.code)
        preview: dict[str, object] = {
            "operation": operation,
            "name": plugin_descriptor.name,
            "new_version": plugin_descriptor.version,
            "source_label": source_label,
            "components": components,
            "settings": settings,
            "warnings": list(dict.fromkeys(warnings)),
        }
        if old_version is not None:
            preview["old_version"] = old_version
        if scope is not None:
            preview["activation_scope"] = scope
        return preview

    def set_enabled(
        self,
        name: str,
        *,
        enabled: bool,
        scope: str = "user",
        workspace: Path | None = None,
    ) -> dict[str, object]:
        """按名称写入 user 或 workspace activation，不修改 artifact。"""
        if not isinstance(enabled, bool):
            raise PluginError("PLUGIN_STATE_INVALID", "enabled 必须是 boolean")
        selected_scope = self._scope(scope)
        workspace_digest = self._activation_digest_for_scope(selected_scope, workspace)
        updated: InstalledPlugin | None = None

        def _update(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            nonlocal updated
            plugin = self._find_by_name(current, name)
            self.store.verify_installed(plugin)
            activation_value = "enabled" if enabled else "disabled"
            user_activation = (
                activation_value if selected_scope == "user" else plugin.activation_user
            )
            workspace_entries = dict(plugin.activation_workspaces)
            if selected_scope == "workspace":
                assert workspace_digest is not None
                workspace_entries[workspace_digest] = activation_value  # type: ignore[assignment]
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
                components=plugin.components,
                diagnostics=plugin.diagnostics,
                activation_user=user_activation,  # type: ignore[arg-type]
                activation_workspaces=tuple(sorted(workspace_entries.items())),  # type: ignore[arg-type]
                installed_at_ms=plugin.installed_at_ms,
                adapter_revision=plugin.adapter_revision,
                origin=plugin.origin,
            )
            return tuple(
                updated if item.plugin_id == plugin.plugin_id else item
                for item in current.plugins
            )

        state = self.store.mutate_registry(_update)
        assert updated is not None
        return self._mutation_result(
            state=state,
            plugin=updated,
            operation="enable" if enabled else "disable",
            scope=selected_scope,
            workspace=workspace,
        )

    def remove(
        self,
        name: str,
        *,
        purge_data: bool = False,
        settings_cleanup: Callable[[InstalledPlugin, int], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """按名称卸载 artifact；清理失败时保留 registry 记录。"""
        before = self._state()
        removed = self._find_by_name(before, name)
        settings_result: dict[str, object] | None = None
        data_purged = False

        def _remove(current: PluginRegistryState) -> tuple[InstalledPlugin, ...]:
            nonlocal data_purged, removed, settings_result
            if current.revision != before.revision:
                raise PluginError("PLUGIN_OPERATION_CONFLICT", "Plugin registry 已变化，请重试")
            current_plugin = self._find_by_name(current, name)
            if current_plugin.package_digest != removed.package_digest:
                raise PluginError("PLUGIN_OPERATION_CONFLICT", "Plugin identity 已变化，请重试")
            removed = current_plugin
            if settings_cleanup is not None:
                settings_result = settings_cleanup(current_plugin, current.revision)
                if settings_result.get("partial"):
                    raise PluginError("SETTINGS_UNINSTALL_PARTIAL", "Plugin Settings 清理未完成")
            if purge_data:
                data_purged = self.store.purge_data(current_plugin)
            return tuple(
                plugin
                for plugin in current.plugins
                if plugin.plugin_id != current_plugin.plugin_id
            )

        state = self.store.mutate_registry(_remove)
        result: dict[str, object] = {
            "operation": "remove",
            "name": removed.name,
            "removed": True,
            "data_retained": not purge_data,
            "data_purged": data_purged,
            "status": "disabled",
            "components": [],
            "warnings": [],
        }
        if settings_result is not None:
            result["settings_cleanup"] = settings_result
        return result

    def catalog(self, *, workspace: Path | None = None) -> ExtensionCatalogSnapshot:
        """发布当前 workspace 的不可变 runtime catalog。"""
        return self._catalog_from_state(self._state(), workspace=workspace)

    def refresh_catalog(self) -> PluginCatalogRefreshResult:
        """用当前 Adapter 重解析已校验 package，保留 activation 不变。"""
        changed_plugin_ids: set[str] = set()
        initial = self.store.read_registry()
        if (
            initial.revision == 0
            and not initial.plugins
            and not self.store.registry_path.exists()
        ):
            # 空的默认 Host 不应因为探测 Plugin 而创建 user-scope lock；有
            # registry 或已安装记录时才进入跨进程刷新事务。
            return PluginCatalogRefreshResult(
                catalog=self._catalog_from_state(initial, workspace=self.workspace)
            )

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
                    components=descriptor.components,
                    diagnostics=descriptor.diagnostics,
                    adapter_revision=descriptor.adapter_revision,
                )
                if next_plugin != plugin:
                    changed_plugin_ids.add(plugin.plugin_id)
                refreshed.append(next_plugin)
            return tuple(refreshed)

        state = self.store.mutate_registry_if_changed(_refresh)
        return PluginCatalogRefreshResult(
            catalog=self._catalog_from_state(state, workspace=self.workspace),
            changed_plugin_ids=tuple(sorted(changed_plugin_ids)),
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
        """按已安装记录解析卸载 identity，不受 activation runtime 过滤。"""
        return self._load_setting_bindings((plugin,), require_runtime=False)

    def setting_bindings_for_management(
        self,
        name: str | None = None,
    ) -> PluginSettingsLoadResult:
        """读取管理面可见的当前声明；disabled Plugin 也能被设置或清理。"""
        state = self._state()
        self._assert_no_name_conflict(state)
        plugins = (
            (self._find_by_name(state, name),)
            if name is not None
            else state.plugins
        )
        return self._load_setting_bindings(tuple(plugins), require_runtime=False)

    def plugin_names_by_id(self) -> dict[str, str]:
        """返回 Host 内部用于脱敏 Settings summary 的 id 到 name 映射。"""
        return {plugin.plugin_id: plugin.name for plugin in self._state().plugins}

    def plugin_id_for_name(self, name: str) -> str:
        """按名称返回 Host 内部使用的安装 ID，不作为用户输入契约。"""
        return self._find_by_name(self._state(), name).plugin_id

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
        """从 enabled Qwen 资源快照读取 canonical 稳定参考块。"""
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
        """返回当前 enabled Plugin 的稳定参考块，供主 Agent 快照使用。"""
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
        state = self._state()
        plugin = _find_plugin(state, plugin_id)
        if plugin.activation_for(self._workspace_digest(None)) != "enabled":
            raise PluginError("PLUGIN_NOT_ENABLED", "Plugin 尚未在当前 workspace 启用")
        self.store.verify_installed(plugin)
        return self.store.package_path(plugin)

    def _catalog_from_state(
        self,
        state: PluginRegistryState,
        *,
        workspace: Path | None = None,
    ) -> ExtensionCatalogSnapshot:
        """过滤当前 workspace enabled 且有实际 consumer 的记录。"""
        self._assert_no_name_conflict(state)
        # 启动 Host 可能先构造一个尚未 mkdir 的 workspace；user activation
        # 不依赖该路径，workspace override 则在可用时按同一 identity 解析。
        workspace_digest = self._workspace_digest(workspace)
        plugins = tuple(
            plugin
            for plugin in state.plugins
            if plugin.activation_for(workspace_digest) == "enabled"
            and any(component.effective for component in plugin.components)
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
