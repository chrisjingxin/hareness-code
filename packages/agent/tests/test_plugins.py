"""Plugin Adapter、Store、registry 与 trust 边界测试。"""

from __future__ import annotations

import json
import os
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.claude import _merge_reports
from harness_agent.plugins.model import (
    InstalledPlugin,
    PluginComponentReport,
    PluginDescriptor,
    PluginError,
    capability_fingerprint,
    runtime_component_eligibility,
)
from harness_agent.plugins.store import package_digest


def _write_json(path: Path, value: object) -> None:
    """写入测试 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_skill(path: Path, *, include_name: bool = True) -> None:
    """写入 portable 或 Claude Skill。"""
    name = "name: review\n" if include_name else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{name}description: Review code safely\n---\n\nReview the requested code.\n",
        encoding="utf-8",
    )


def _portable_plugin(root: Path) -> None:
    """创建含 Skill、MCP 和 Harness Agent 的 portable fixture。"""
    _write_json(
        root / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "secure-review",
            "version": "1.0.0",
            "description": "Review plugin",
            "extensions": {
                "com.za38.harness": {
                    "schemaVersion": "1.0.0",
                    "agents": "./com.za38.harness/agents",
                }
            },
        },
    )
    _write_skill(root / "skills" / "review" / "SKILL.md")
    _write_json(
        root / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "local-check": {
                    "type": "stdio",
                    "command": "./bin/check",
                    "args": [],
                }
            },
        },
    )
    agent = root / "com.za38.harness" / "agents" / "reviewer.yaml"
    agent.parent.mkdir(parents=True)
    agent.write_text("id: reviewer\n", encoding="utf-8")


def _descriptor_with_components(
    components: tuple[PluginComponentReport, ...],
) -> PluginDescriptor:
    """构造只关注聚合语义的 PluginDescriptor。"""
    return PluginDescriptor(
        name="semantic-plugin",
        version="1.0.0",
        description=None,
        format="agent-plugins-1.0",
        manifest="plugin.json",
        package_digest="d" * 64,
        capability_fingerprint="f" * 64,
        components=components,
    )


def _component(
    status: str,
    *,
    effective: bool,
    diagnostics: tuple[str, ...] = (),
) -> PluginComponentReport:
    """构造聚合测试用的组件报告。"""
    return PluginComponentReport(
        kind=f"component-{status}-{effective}",
        status=status,  # type: ignore[arg-type]
        count=1 if effective else 0,
        diagnostics=diagnostics,
        effective=effective,
    )


def _runtime_plugin(
    *,
    kind: str = "skills",
    status: str = "supported",
    effective: bool = True,
    enabled: bool = True,
    trusted: bool = True,
    format: str = "claude-code",
) -> InstalledPlugin:
    """构造不接触文件系统的 runtime gate fixture。"""
    component = PluginComponentReport(
        kind=kind,
        status=status,  # type: ignore[arg-type]
        count=1,
        effective=effective,
    )
    fingerprint = capability_fingerprint((component,))
    return InstalledPlugin(
        plugin_id="fixture/runtime-gate",
        source_id="fixture",
        source_label="runtime-gate",
        name="runtime-gate",
        version="1.0.0",
        description=None,
        format=format,  # type: ignore[arg-type]
        manifest="plugin.json",
        package_digest="d" * 64,
        capability_fingerprint=fingerprint,
        components=(component,),
        diagnostics=(),
        enabled=enabled,
        trusted_capability_fingerprint=fingerprint if trusted else None,
        installed_at_ms=0,
    )


@pytest.mark.parametrize(
    ("status", "effective", "expected_reason"),
    (
        ("unsupported", False, "COMPONENT_STATUS_UNSUPPORTED"),
        ("invalid", False, "COMPONENT_STATUS_INVALID"),
        ("supported", False, "COMPONENT_NOT_EFFECTIVE"),
        ("supported", True, None),
        ("adapted", True, None),
    ),
)
def test_runtime_component_gate_requires_supported_effective_report(
    status: str,
    effective: bool,
    expected_reason: str | None,
) -> None:
    """统一 gate 同时约束 status 与 effective，不能只看字段或数量。"""
    result = runtime_component_eligibility(
        _runtime_plugin(status=status, effective=effective),
        kind="skills",
    )

    assert result.eligible is (expected_reason is None)
    if expected_reason is None:
        assert result.diagnostic is None
    else:
        assert result.diagnostic == (
            "PLUGIN_RUNTIME_COMPONENT_BLOCKED: "
            f"kind=skills; reason={expected_reason}"
        )


def test_runtime_component_gate_rejects_plugin_state_and_format_mismatch() -> None:
    """disabled、untrusted 和未开放格式的组件均不能构造 runtime。"""
    disabled = runtime_component_eligibility(
        _runtime_plugin(enabled=False),
        kind="skills",
    )
    untrusted = runtime_component_eligibility(
        _runtime_plugin(trusted=False),
        kind="skills",
    )
    qwen_lsp = runtime_component_eligibility(
        _runtime_plugin(kind="lsp", format="qwen-code"),
        kind="lsp",
    )

    assert disabled.diagnostic == (
        "PLUGIN_RUNTIME_COMPONENT_BLOCKED: kind=skills; reason=PLUGIN_DISABLED"
    )
    assert untrusted.diagnostic == (
        "PLUGIN_RUNTIME_COMPONENT_BLOCKED: kind=skills; reason=PLUGIN_UNTRUSTED"
    )
    assert qwen_lsp.eligible is True
    assert qwen_lsp.diagnostic is None


def test_runtime_component_gate_rejects_component_report_fingerprint_drift() -> None:
    """组件报告被替换但未重新授权时，gate 必须稳定阻断。"""
    plugin = _runtime_plugin()
    component = replace(plugin.components[0], capabilities=("prompt:changed",))
    drifted = replace(plugin, components=(component,))

    result = runtime_component_eligibility(drifted, kind="skills")

    assert result.eligible is False
    assert result.diagnostic == (
        "PLUGIN_RUNTIME_COMPONENT_BLOCKED: "
        "kind=skills; reason=COMPONENT_FINGERPRINT_DRIFT"
    )


def test_runtime_component_gate_excludes_diagnostics_from_execution_binding() -> None:
    """诊断文案变化不改变已授权的执行来源绑定。"""
    plugin = _runtime_plugin()
    changed = replace(
        plugin.components[0],
        diagnostics=("diagnostic wording changed",),
    )

    result = runtime_component_eligibility(
        replace(plugin, components=(changed,)),
        kind="skills",
    )

    assert result.eligible is True
    assert result.diagnostic is None


def test_declared_component_report_missing_has_stable_diagnostics(
    tmp_path: Path,
) -> None:
    """已声明的 portable Command/Skill/MCP 缺报告时不能静默跳过。"""
    source = tmp_path / "missing-report"
    source.mkdir()
    _portable_plugin(source)
    manifest_path = source / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extension = manifest["extensions"]["com.za38.harness"]
    extension["commands"] = "commands"
    _write_skill(source / "commands" / "review.md")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    plugin = manager.store.read_registry().plugins[0]
    reports = tuple(
        component
        for component in plugin.components
        if component.kind not in {"commands", "skills", "mcp"}
    )
    fingerprint = capability_fingerprint(reports)
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(
                item,
                components=reports,
                capability_fingerprint=fingerprint,
                trusted_capability_fingerprint=fingerprint,
                enabled=True,
            )
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )

    catalog = manager.catalog()
    skill_result = manager.skill_sources(catalog)
    mcp_result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")

    assert skill_result.sources == ()
    assert mcp_result.servers == ()
    for kind in ("commands", "skills"):
        assert any(
            f"kind={kind}; reason=COMPONENT_REPORT_MISSING" in diagnostic
            for diagnostic in skill_result.diagnostics
        )
    assert any(
        "kind=mcp; reason=COMPONENT_REPORT_MISSING" in diagnostic
        for diagnostic in mcp_result.diagnostics
    )


def test_component_report_fingerprint_drift_blocks_portable_consumers(
    tmp_path: Path,
) -> None:
    """portable 的 Command/Skill/MCP report 漂移时均不可进入 runtime。"""
    source = tmp_path / "portable-drift"
    source.mkdir()
    _portable_plugin(source)
    manifest_path = source / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extension = manifest["extensions"]["com.za38.harness"]
    extension["commands"] = "commands"
    _write_skill(source / "commands" / "review.md")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    plugin = manager.store.read_registry().plugins[0]
    drifted = tuple(
        replace(component, capabilities=(*component.capabilities, "report:drift"))
        if component.kind in {"commands", "skills", "mcp"}
        else component
        for component in plugin.components
    )
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, components=drifted, enabled=True)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )

    catalog = manager.catalog()
    assert {component.kind for component in catalog.plugins[0].components} >= {
        "commands",
        "skills",
        "mcp",
    }
    assert runtime_component_eligibility(
        catalog.plugins[0],
        kind="skills",
    ).diagnostic is not None
    skill_result = manager.skill_sources(catalog)
    mcp_result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")

    assert skill_result.sources == ()
    assert mcp_result.servers == ()
    for kind in ("commands", "skills"):
        assert any(
            f"kind={kind}; reason=COMPONENT_FINGERPRINT_DRIFT" in diagnostic
            for diagnostic in skill_result.diagnostics
        ), skill_result.diagnostics
    assert any(
        "kind=mcp; reason=COMPONENT_FINGERPRINT_DRIFT" in diagnostic
        for diagnostic in mcp_result.diagnostics
    ), mcp_result.diagnostics


def test_component_source_drift_blocks_portable_source_consumers(
    tmp_path: Path,
) -> None:
    """只替换 Command/Skill/Agent source 时，所有 source consumer 都必须停下。"""
    source = tmp_path / "portable-source-drift"
    source.mkdir()
    _portable_plugin(source)
    manifest_path = source / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extension = manifest["extensions"]["com.za38.harness"]
    extension["commands"] = "commands"
    _write_json(manifest_path, manifest)
    (source / "commands").mkdir()
    (source / "commands" / "safe.md").write_text("# safe\n", encoding="utf-8")
    (source / "commands" / "other.md").write_text("# other\n", encoding="utf-8")
    _write_skill(source / "skills" / "other" / "SKILL.md")
    (source / "com.za38.harness" / "agents" / "other.yaml").write_text(
        "id: other\n",
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    plugin = manager.store.read_registry().plugins[0]
    replacements = {
        "commands": ("commands/other.md",),
        "skills": ("skills/other/SKILL.md",),
        "agents": ("com.za38.harness/agents/other.yaml",),
    }
    drifted = tuple(
        replace(component, sources=replacements[component.kind])
        if component.kind in replacements
        else component
        for component in plugin.components
    )
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, components=drifted, enabled=True)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )

    catalog = manager.catalog()
    skill_result = manager.skill_sources(catalog)
    agent_result = manager.agent_sources(catalog)

    assert skill_result.sources == ()
    assert agent_result.sources == ()
    for kind in ("commands", "skills", "agents"):
        assert any(
            f"kind={kind}; reason=COMPONENT_FINGERPRINT_DRIFT" in diagnostic
            for diagnostic in (*skill_result.diagnostics, *agent_result.diagnostics)
        )


@pytest.mark.parametrize(
    ("components", "compatibility", "can_enable"),
    (
        ((), "recognized", False),
        ((_component("unsupported", effective=False),), "recognized", False),
        ((_component("invalid", effective=False),), "invalid", False),
        ((_component("supported", effective=True),), "ready", True),
        ((_component("adapted", effective=True),), "recognized", True),
        (
            (
                _component("invalid", effective=False),
                _component("supported", effective=True),
            ),
            "partial",
            True,
        ),
        (
            (
                _component("unsupported", effective=False),
                _component("supported", effective=True),
            ),
            "partial",
            True,
        ),
        (
            (
                _component(
                    "supported",
                    effective=True,
                    diagnostics=("PLUGIN_COMPONENT_INVALID: broken entry",),
                ),
            ),
            "partial",
            True,
        ),
    ),
)
def test_plugin_descriptor_aggregates_compatibility_and_enable_gate(
    components: tuple[PluginComponentReport, ...],
    compatibility: str,
    can_enable: bool,
) -> None:
    """聚合状态和启用条件保持一致，并隔离有效组件与局部坏条目。"""
    descriptor = _descriptor_with_components(components)
    assert descriptor.compatibility == compatibility
    assert descriptor.can_enable is can_enable
    assert not (descriptor.compatibility == "invalid" and descriptor.can_enable)


def test_claude_component_merge_preserves_effective_files_and_reports_bad_entries() -> None:
    """Claude 同类路径合并不因坏路径丢失有效文件，也不依赖已删除的状态排序表。"""
    merged = _merge_reports(
        [
            PluginComponentReport(
                kind="commands",
                status="supported",
                count=1,
                sources=("commands/review.md",),
                effective=True,
            ),
            PluginComponentReport(
                kind="commands",
                status="invalid",
                count=0,
                diagnostics=("PLUGIN_COMPONENT_INVALID: commands/missing: 路径不存在",),
            ),
        ]
    )

    assert len(merged) == 1
    report = merged[0]
    assert report.status == "supported"
    assert report.count == 1
    assert report.effective is True
    assert report.sources == ("commands/review.md",)
    assert report.diagnostics == (
        "PLUGIN_COMPONENT_INVALID: commands/missing: 路径不存在",
    )
    descriptor = _descriptor_with_components(tuple(merged))
    assert descriptor.compatibility == "partial"
    assert descriptor.can_enable is True


def test_installed_plugin_reuses_descriptor_semantics(tmp_path: Path) -> None:
    """registry 安装记录重建 Descriptor 后仍使用同一聚合结果。"""
    source = tmp_path / "partial-plugin"
    source.mkdir()
    _portable_plugin(source)
    broken_skill = source / "skills" / "broken" / "SKILL.md"
    _write_skill(broken_skill)
    broken_skill.write_text(
        broken_skill.read_text(encoding="utf-8").replace("name: review", "name: other"),
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["compatibility"] == "partial"
    assert installed["can_enable"] is True

    record = manager.store.read_registry().plugins[0]
    assert record.compatibility == "partial"
    assert record.can_enable is True


def test_zero_effective_plugin_install_is_disabled_and_enable_has_stable_error(
    tmp_path: Path,
) -> None:
    """空 portable 包可以安装，但没有 effective 组件时启用失败关闭。"""
    source = tmp_path / "empty-plugin"
    source.mkdir()
    _write_json(
        source / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "empty-plugin",
            "version": "1.0.0",
        },
    )
    manager = PluginManager(home=tmp_path / "home")

    validation = manager.validate(source)["plugin"]
    assert isinstance(validation, dict)
    assert validation["compatibility"] == "recognized"
    assert validation["can_enable"] is False

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    with pytest.raises(PluginError) as rejected:
        manager.set_enabled(
            str(installed["id"]),
            enabled=True,
            capability_fingerprint=str(installed["capability_fingerprint"]),
        )
    assert rejected.value.code == "PLUGIN_NO_EFFECTIVE_COMPONENT"


def test_portable_install_is_disabled_and_enable_requires_current_fingerprint(
    tmp_path: Path,
) -> None:
    """安装默认停用，启用必须显式确认精确能力指纹。"""
    source = tmp_path / "secure-review"
    source.mkdir()
    _portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")

    validation = manager.validate(source)
    assert validation["will_install_enabled"] is False
    plugin_summary = validation["plugin"]
    assert isinstance(plugin_summary, dict)
    assert plugin_summary["format"] == "agent-plugins-1.0"
    assert plugin_summary["compatibility"] == "ready"
    assert {item["kind"] for item in plugin_summary["components"]} == {
        "agents",
        "mcp",
        "skills",
    }

    installed = manager.install(source)
    plugin = installed["plugin"]
    assert isinstance(plugin, dict)
    assert plugin["enabled"] is False
    plugin_id = str(plugin["id"])
    fingerprint = str(plugin["capability_fingerprint"])

    with pytest.raises(PluginError, match="capability_fingerprint") as rejected:
        manager.set_enabled(plugin_id, enabled=True, capability_fingerprint="0" * 64)
    assert rejected.value.code == "PLUGIN_CAPABILITY_CONFIRMATION_REQUIRED"

    enabled = manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=fingerprint,
    )
    enabled_plugin = enabled["plugin"]
    assert isinstance(enabled_plugin, dict)
    assert enabled_plugin["enabled"] is True
    assert enabled_plugin["trusted"] is True
    assert manager.catalog().plugins[0].plugin_id == plugin_id


@pytest.mark.parametrize("source_kind", ("directory", "zip"))
def test_plugin_management_lifecycle_covers_directory_and_zip_sources(
    tmp_path: Path,
    source_kind: str,
) -> None:
    """目录和 ZIP 经过同一管理入口，状态只在下一 Host 快照边界生效。"""
    source = tmp_path / "lifecycle-plugin"
    source.mkdir()
    _portable_plugin(source)
    executable = source / "bin" / "check"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    package = source
    if source_kind == "zip":
        package = tmp_path / "lifecycle-plugin.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, (Path(source.name) / path.relative_to(source)).as_posix())

    manager = PluginManager(home=tmp_path / f"home-{source_kind}")
    validated = manager.validate(package)
    assert validated["will_install_enabled"] is False
    assert manager.list()["plugins"] == []

    installed = manager.install(package)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    fingerprint = str(installed["capability_fingerprint"])
    assert installed["enabled"] is False
    assert manager.inspect(plugin_id)["plugin"]["id"] == plugin_id  # type: ignore[index]
    assert manager.list()["catalog"]["count"] == 0  # type: ignore[index]

    with pytest.raises(PluginError) as wrong_fingerprint:
        manager.set_enabled(
            plugin_id,
            enabled=True,
            capability_fingerprint="0" * 64,
        )
    assert wrong_fingerprint.value.code == "PLUGIN_CAPABILITY_CONFIRMATION_REQUIRED"

    enabled = manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=fingerprint,
    )
    assert enabled["effective_on"] == "next_host"
    assert enabled["catalog"]["count"] == 1  # type: ignore[index]
    assert manager.list(include_disabled=False)["plugins"]  # type: ignore[index]

    disabled = manager.set_enabled(plugin_id, enabled=False)
    assert disabled["effective_on"] == "next_host"
    assert disabled["catalog"]["count"] == 0  # type: ignore[index]
    assert manager.list(include_disabled=False)["plugins"] == []

    removed = manager.remove(plugin_id)
    assert removed["removed"] is True
    assert manager.list()["plugins"] == []


def test_static_preview_only_projects_non_effective_qwen_resources(
    tmp_path: Path,
) -> None:
    """静态 preview 只展示尚未接入运行时的 Qwen 资源，不复制既有格式。"""
    manager = PluginManager(home=tmp_path / "home")

    portable = tmp_path / "portable-preview"
    portable.mkdir()
    _portable_plugin(portable)
    portable_installed = manager.install(portable)["plugin"]
    assert isinstance(portable_installed, dict)
    assert manager.static_preview() == {
        "commands": [],
        "skills": [],
        "agents": [],
        "mcp": [],
    }

    manager.set_enabled(
        str(portable_installed["id"]),
        enabled=True,
        capability_fingerprint=str(portable_installed["capability_fingerprint"]),
    )
    assert manager.catalog().plugins[0].plugin_id == portable_installed["id"]
    assert manager.static_preview() == {
        "commands": [],
        "skills": [],
        "agents": [],
        "mcp": [],
    }

    claude = tmp_path / "claude-preview"
    claude.mkdir()
    _write_json(
        claude / ".claude-plugin" / "plugin.json",
        {"name": "claude-preview", "version": "1.0.0"},
    )
    _write_skill(claude / "skills" / "review" / "SKILL.md", include_name=False)
    claude_installed = manager.install(claude)["plugin"]
    assert isinstance(claude_installed, dict)
    assert claude_installed["format"] == "claude-code"

    hybrid = tmp_path / "hybrid-preview"
    hybrid.mkdir()
    _portable_plugin(hybrid)
    _write_json(
        hybrid / ".claude-plugin" / "plugin.json",
        {"name": "secure-review", "version": "1.0.0"},
    )
    hybrid_installed = manager.install(hybrid)["plugin"]
    assert isinstance(hybrid_installed, dict)
    assert hybrid_installed["format"] == "hybrid"

    assert manager.static_preview() == {
        "commands": [],
        "skills": [],
        "agents": [],
        "mcp": [],
    }


def test_static_preview_hides_qwen_when_an_effective_component_exists(
    tmp_path: Path,
) -> None:
    """未来 Qwen 组件接入运行时后不应再与 static preview 重复。"""
    from dataclasses import replace

    qwen = tmp_path / "qwen-effective"
    qwen.mkdir()
    _write_json(qwen / "qwen-extension.json", {"name": "qwen-effective"})
    (qwen / "commands").mkdir()
    (qwen / "commands" / "review.md").write_text(
        "---\ndescription: Review\n---\nReview the change.\n",
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(qwen)["plugin"]
    assert isinstance(installed, dict)
    assert manager.static_preview()["commands"]

    state = manager.store.read_registry()
    plugin = state.plugins[0]
    effective_components = tuple(
        replace(
            component,
            status="supported",
            effective=True,
            count=max(component.count, 1),
        )
        for component in plugin.components
    )
    effective_fingerprint = capability_fingerprint(effective_components)
    assert all(
        isinstance(component, PluginComponentReport)
        for component in effective_components
    )
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(
                item,
                components=effective_components,
                enabled=True,
                capability_fingerprint=effective_fingerprint,
                trusted_capability_fingerprint=effective_fingerprint,
            )
            if item.plugin_id == plugin.plugin_id
            else item
            for item in current.plugins
        )
    )

    assert manager.catalog().plugins[0].plugin_id == plugin.plugin_id
    assert manager.static_preview() == {
        "commands": [],
        "skills": [],
        "agents": [],
        "mcp": [],
    }


def test_catalog_excludes_legacy_enabled_plugin_without_effective_components(
    tmp_path: Path,
) -> None:
    """旧记录即使仍标记 enabled/trusted，也不能绕过当前运行目录门禁。"""
    source = tmp_path / "legacy-enabled"
    source.mkdir()
    _portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    fingerprint = str(installed["capability_fingerprint"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=fingerprint,
    )
    assert [plugin.plugin_id for plugin in manager.catalog().plugins] == [plugin_id]

    manager.store.mutate_registry(
        lambda state: tuple(
            replace(plugin, components=()) if plugin.plugin_id == plugin_id else plugin
            for plugin in state.plugins
        )
    )
    legacy = manager.store.read_registry().plugins[0]
    assert legacy.enabled is True
    assert legacy.trusted_capability_fingerprint == fingerprint
    assert legacy.can_enable is False
    assert manager.catalog().plugins == ()


def test_plugin_summaries_do_not_expose_source_or_store_paths(tmp_path: Path) -> None:
    """管理 API 只返回来源标签和内容身份，不泄露宿主路径。"""
    source = tmp_path / "source" / "secure-review"
    source.mkdir(parents=True)
    _portable_plugin(source)
    home = tmp_path / "private-home"
    manager = PluginManager(home=home)
    installed = manager.install(source)
    plugin_id = str(installed["plugin"]["id"])  # type: ignore[index]

    serialized = json.dumps(
        {
            "validation": manager.validate(source),
            "list": manager.list(),
            "inspect": manager.inspect(plugin_id),
        },
        ensure_ascii=False,
    )
    assert str(source) not in serialized
    assert str(home) not in serialized
    assert '"label": "secure-review"' in serialized


def test_remove_retains_data_unless_purge_is_explicit(tmp_path: Path) -> None:
    """默认卸载只移除 registry，显式 purge 才删除持久 data。"""
    source = tmp_path / "plugin"
    source.mkdir()
    _portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)
    plugin_id = str(installed["plugin"]["id"])  # type: ignore[index]
    record = manager.store.read_registry().plugins[0]
    data_path = manager.store.data_path(record)
    data_path.mkdir(parents=True)
    (data_path / "cache.txt").write_text("keep", encoding="utf-8")

    removed = manager.remove(plugin_id)
    assert removed["data_retained"] is True
    assert data_path.is_dir()
    assert manager.list()["plugins"] == []

    manager.install(source)
    purged = manager.remove(plugin_id, purge_data=True)
    assert purged["data_retained"] is False
    assert purged["data_purged"] is True
    assert not data_path.exists()


def test_remove_does_not_cleanup_settings_after_registry_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """registry revision 变化时必须先拒绝，不能清完 Settings 再留下 Plugin。"""
    source = tmp_path / "plugin"
    source.mkdir()
    _portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)
    plugin_id = str(installed["plugin"]["id"])  # type: ignore[index]
    calls: list[tuple[str, int]] = []

    def mutate_with_revision_drift(operation):
        current = manager.store.read_registry()
        drifted = replace(current, revision=current.revision + 1)
        return operation(drifted)

    monkeypatch.setattr(manager.store, "mutate_registry", mutate_with_revision_drift)

    with pytest.raises(PluginError) as error:
        manager.remove(
            plugin_id,
            purge_data=True,
            settings_cleanup=lambda plugin, revision: calls.append(
                (plugin.plugin_id, revision)
            ) or {"partial": []},
        )

    assert error.value.code == "PLUGIN_REGISTRY_REVISION_CONFLICT"
    assert calls == []


def test_claude_current_components_are_never_silently_ignored(tmp_path: Path) -> None:
    """Claude 新组件进入 compatibility report，尚未实现的能力明确为 unsupported。"""
    source = tmp_path / "claude-plugin"
    source.mkdir()
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-suite",
            "version": "2.1.0",
            "skills": "./skills",
            "agents": "./agents",
            "workflows": "./workflows",
            "hooks": {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "true", "args": []}
                            ],
                        }
                    ]
                }
            },
            "mcpServers": {"local": {"command": "server"}},
            "lspServers": "./.lsp.json",
            "experimental": {
                "themes": "./themes",
                "monitors": "./monitors/monitors.json",
            },
            "userConfig": {"endpoint": {"type": "string"}},
            "channels": [{"name": "alerts"}],
            "dependencies": ["helper"],
        },
    )
    _write_skill(source / "skills" / "review" / "SKILL.md", include_name=False)
    for relative, content in (
        ("agents/reviewer.md", "---\nname: reviewer\n---\nReview."),
        ("workflows/check.py", "print('check')"),
        (
            ".lsp.json",
            '{"python":{"command":"pyright-langserver","args":["--stdio"],'
            '"extensionToLanguage":{".py":"python"}}}',
        ),
        ("themes/dark.json", "{}"),
        (
            "monitors/monitors.json",
            '[{"name":"errors","command":"tail -F ./errors.log"}]',
        ),
        ("bin/helper", "#!/bin/sh\n"),
        ("settings.json", "{}"),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    assert summary["format"] == "claude-code"
    components = {item["kind"]: item for item in summary["components"]}
    assert components["skills"]["status"] == "supported"
    assert components["skills"]["effective"] is True
    assert components["mcp"]["status"] == "supported"
    assert components["mcp"]["effective"] is True
    assert components["agents"]["status"] == "supported"
    assert components["agents"]["effective"] is True
    for kind in ("hooks", "lsp", "monitors"):
        assert components[kind]["status"] == "adapted"
        assert components[kind]["effective"] is True
    for kind in (
        "workflows",
        "themes",
        "user-config",
        "channels",
        "dependencies",
        "bin",
        "settings",
    ):
        assert components[kind]["status"] == "unsupported"
        assert components[kind]["effective"] is False


def test_claude_unsupported_hook_and_socket_lsp_are_reported(tmp_path: Path) -> None:
    """未接入的 Hook handler 与 socket LSP 必须明确报告为 unsupported。"""
    source = tmp_path / "claude-unsupported-runtime"
    source.mkdir()
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-unsupported-runtime",
            "version": "1.0.0",
            "hooks": {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "prompt", "prompt": "Decide whether to run."}
                            ],
                        }
                    ]
                }
            },
            "lspServers": {
                "remote": {
                    "command": "remote-lsp",
                    "transport": "socket",
                    "extensionToLanguage": {".remote": "remote"},
                }
            },
        },
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    components = {item["kind"]: item for item in summary["components"]}
    assert components["hooks"]["status"] == "unsupported"
    assert components["hooks"]["effective"] is False
    assert components["hooks"]["count"] == 0
    assert components["hooks"]["diagnostics"]
    assert components["lsp"]["status"] == "unsupported"
    assert components["lsp"]["effective"] is False
    assert components["lsp"]["count"] == 0
    assert components["lsp"]["diagnostics"]


def test_enabled_claude_agent_enters_canonical_agent_catalog(tmp_path: Path) -> None:
    """Claude `agents/*.md` 原样安装后进入统一 Agent/Policy 快照。"""
    from harness_agent.runtime.agent_catalog import AgentCatalog
    from harness_agent.config.config import ModelCatalog, ModelProfile, ModelSettings

    source = tmp_path / "claude-agents"
    source.mkdir()
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-agents",
            "version": "1.0.0",
            "agents": "./agents",
        },
    )
    agent = source / "agents" / "security-reviewer.md"
    agent.parent.mkdir()
    agent.write_text(
        "---\n"
        "name: security-reviewer\n"
        "description: Review authentication boundaries\n"
        "tools: Read, Glob, Grep\n"
        "model: inherit\n"
        "maxTurns: 12\n"
        "---\n\n"
        "Only report findings with repository evidence.\n",
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    loaded = manager.agent_sources(manager.catalog())
    assert loaded.diagnostics == ()
    catalog = AgentCatalog(
        model_catalog=ModelCatalog(
            default_profile="default",
            profiles={
                "default": ModelProfile(
                    "default",
                    ModelSettings("test-model", "https://example.test"),
                    "test",
                )
            },
            role_profiles={},
        ),
        sources=loaded.sources,
    )

    definition = catalog.require_agent("security-reviewer")
    policy = catalog.require_policy(definition.execution_policy_id)
    assert definition.prompt == "Only report findings with repository evidence."
    assert definition.model_profile_id == "inherit"
    assert definition.max_turns == 12
    assert policy.tools is not None
    assert policy.tools.allow == ("glob", "grep", "read_file")
    assert policy.filesystem_write == ()
    assert catalog.diagnostics == ()


def test_portable_team_file_becomes_fixed_dag_definition(tmp_path: Path) -> None:
    """Harness extension 的 Team YAML 可加载，任务依赖由 TeamDefinition 校验。"""
    from harness_agent.runtime.team_coordinator import TeamFailurePolicy

    source = tmp_path / "team-plugin"
    source.mkdir()
    _write_json(
        source / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "team-plugin",
            "version": "1.0.0",
            "extensions": {
                "com.za38.harness": {
                    "schemaVersion": "1.0.0",
                    "teams": "./com.za38.harness/teams",
                }
            },
        },
    )
    team = source / "com.za38.harness" / "teams" / "review.yaml"
    team.parent.mkdir(parents=True)
    team.write_text(
        "id: secure-review\n"
        "description: Review then synthesize\n"
        "maxParallelism: 2\n"
        "failurePolicy: continue-to-synthesis\n"
        "tasks:\n"
        "  - id: security\n"
        "    agent: security-reviewer\n"
        "    input: '{{request}}'\n"
        "  - id: synthesis\n"
        "    agent: review-lead\n"
        "    dependsOn: [security]\n"
        "    input: '{{tasks.security.result}}'\n",
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.team_definitions(manager.catalog())

    assert result.diagnostics == ()
    assert len(result.teams) == 1
    definition = result.teams[0]
    assert definition.team_id == "secure-review"
    assert definition.failure_policy is TeamFailurePolicy.CONTINUE_TO_SYNTHESIS
    assert definition.tasks[1].depends_on == ("security",)


def test_manifestless_claude_requires_unambiguous_component_or_explicit_format(
    tmp_path: Path,
) -> None:
    """仅有 skills/ 时 auto 不猜格式，显式 Claude 后可以推导名称。"""
    source = tmp_path / "My Skills"
    source.mkdir()
    _write_skill(source / "skills" / "review" / "SKILL.md", include_name=False)
    manager = PluginManager(home=tmp_path / "home")

    with pytest.raises(PluginError) as ambiguous:
        manager.validate(source)
    assert ambiguous.value.code == "PLUGIN_FORMAT_AMBIGUOUS"

    result = manager.validate(source, format="claude-code")["plugin"]
    assert isinstance(result, dict)
    assert result["name"] == "my-skills"
    assert result["manifest"] is None


def test_enabled_portable_skill_and_mcp_enter_one_runtime_catalog(tmp_path: Path) -> None:
    """portable Skill/MCP 使用 Plugin namespace、按需资源和最小进程环境。"""
    from harness_agent.extensions.mcp import McpConnectionManager, build_mcp_snapshot
    from harness_agent.extensions.plugin_skills import SkillRegistry
    from harness_agent.threads.virtual_files import HarnessVirtualBackend

    source = tmp_path / "secure-review"
    source.mkdir()
    _portable_plugin(source)
    skill = source / "skills" / "review" / "SKILL.md"
    skill.write_text(
        "---\n"
        "name: review\n"
        "description: Review code safely\n"
        "allowed-tools: [read_file, shell]\n"
        "disable-model-invocation: true\n"
        "---\n\nReview the requested code.\n",
        encoding="utf-8",
    )
    (skill.parent / "reference.txt").write_text("portable reference", encoding="utf-8")
    executable = source / "bin" / "check"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    mcp_document = json.loads((source / "mcp.json").read_text(encoding="utf-8"))
    mcp_document["mcpServers"]["local-check"].update(
        {
            "args": ["${PLUGIN_ROOT}/plugin.json", "${PLUGIN_DATA}"],
            "cwd": "${PLUGIN_DATA}",
            "env": {"MODE": "safe"},
        }
    )
    _write_json(source / "mcp.json", mcp_document)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    enabled = manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    assert enabled["effective_on"] == "next_host"
    catalog = manager.catalog()

    skill_result = manager.skill_sources(catalog)
    registry = SkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=skill_result.sources,
        plugin_diagnostics=skill_result.diagnostics,
    )
    plugin_id = catalog.plugins[0].plugin_id
    skill_id = f"plugin/{plugin_id}/review"
    record = registry.resolve(skill_id)
    assert record.requested_tools == ("read_file", "shell")
    assert record.model_invocable is False
    assert skill_id not in registry.system_prompt_fragment()
    backend = HarnessVirtualBackend(registry=registry, thread_id="thread")
    resource = backend.read(f"/.harness/skills/{skill_id}/reference.txt")
    assert resource.file_data and resource.file_data["content"] == "portable reference"
    # 模型偶尔会把多段 canonical ID 展平成单个文件名；虚拟只读层只在唯一时兼容。
    flattened_id = skill_id.replace("/", "-")
    flattened_skill = backend.read(f"/.harness/skills/{flattened_id}/SKILL.md")
    assert flattened_skill.file_data and "Review the requested code." in flattened_skill.file_data["content"]
    flattened_resource = backend.read(f"/.harness/skills/{flattened_id}/reference.txt")
    assert flattened_resource.file_data and flattened_resource.file_data["content"] == "portable reference"

    mcp_result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")
    assert mcp_result.diagnostics == ()
    assert len(mcp_result.servers) == 1
    server = mcp_result.servers[0]
    assert server.name.startswith("plugin__")
    assert server.source == f"plugin:{plugin_id}"
    assert server.source_fingerprint == catalog.plugins[0].package_digest
    assert server.inherit_environment is False
    connection = McpConnectionManager(
        build_mcp_snapshot(mcp_result.servers, "plugin-catalog")
    )._build_single_connection(server)
    assert connection is not None
    assert str(manager.store.data_path(catalog.plugins[0])) == connection["cwd"]
    assert "HOME" not in connection["env"]


def test_skill_and_mcp_consumers_require_effective_component_report(
    tmp_path: Path,
) -> None:
    """Command/Skill 与 MCP 不能仅凭 supported 或字段存在进入 runtime。"""
    source = tmp_path / "effective-gate"
    source.mkdir()
    _portable_plugin(source)
    executable = source / "bin" / "check"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    plugin = manager.store.read_registry().plugins[0]
    reports = tuple(
        replace(
            component,
            status="supported",
            effective=False,
            count=component.count,
        )
        if component.kind in {"skills", "mcp"}
        else component
        for component in plugin.components
    )
    fingerprint = capability_fingerprint(reports)
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(
                item,
                components=reports,
                capability_fingerprint=fingerprint,
                trusted_capability_fingerprint=fingerprint,
                enabled=True,
            )
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )

    catalog = manager.catalog()
    skill_result = manager.skill_sources(catalog)
    mcp_result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")

    assert skill_result.sources == ()
    assert mcp_result.servers == ()
    assert any("kind=skills" in diagnostic for diagnostic in skill_result.diagnostics)
    assert any("kind=mcp" in diagnostic for diagnostic in mcp_result.diagnostics)


def test_invalid_plugin_items_are_isolated_from_valid_skill_and_mcp(
    tmp_path: Path,
) -> None:
    """同包坏 Skill/MCP 产生诊断，但有效同类项仍可进入启动快照。"""
    source = tmp_path / "partial-plugin"
    source.mkdir()
    _portable_plugin(source)
    invalid_skill = source / "skills" / "broken" / "SKILL.md"
    _write_skill(invalid_skill)
    invalid_skill.write_text(
        invalid_skill.read_text(encoding="utf-8").replace("name: review", "name: other"),
        encoding="utf-8",
    )
    executable = source / "bin" / "check"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    mcp_document = json.loads((source / "mcp.json").read_text(encoding="utf-8"))
    mcp_document["mcpServers"]["broken"] = {"type": "stdio"}
    _write_json(source / "mcp.json", mcp_document)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    components = {item["kind"]: item for item in installed["components"]}
    assert components["skills"]["status"] == "supported"
    assert components["skills"]["count"] == 1
    assert components["mcp"]["status"] == "supported"
    assert components["mcp"]["count"] == 1
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    catalog = manager.catalog()
    skills = manager.skill_sources(catalog)
    mcp = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")
    assert [source.name for source in skills.sources] == ["review"]
    assert len(mcp.servers) == 1
    assert any("broken" in diagnostic for diagnostic in mcp.diagnostics)


def test_enabled_claude_skill_and_inline_mcp_are_adapted_without_repacking(
    tmp_path: Path,
) -> None:
    """原 Claude 目录无需改写即可进入 Harness Skill/MCP 启动来源。"""
    from harness_agent.extensions.plugin_skills import SkillRegistry

    source = tmp_path / "claude-runtime"
    source.mkdir()
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-runtime",
            "version": "1.0.0",
            "mcpServers": {
                "review": {
                    "command": "node",
                    "args": [
                        "${CLAUDE_PLUGIN_ROOT}/server.js",
                        "${CLAUDE_PLUGIN_DATA}",
                        "${CLAUDE_PROJECT_DIR}",
                    ],
                    "cwd": "${CLAUDE_PROJECT_DIR}",
                }
            },
        },
    )
    _write_skill(source / "skills" / "review" / "SKILL.md", include_name=False)
    (source / "server.js").write_text("// fixture\n", encoding="utf-8")
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    manager = PluginManager(home=home)
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    catalog = manager.catalog()
    skill_result = manager.skill_sources(catalog)
    registry = SkillRegistry(
        workspace,
        home=home,
        plugin_sources=skill_result.sources,
    )
    assert registry.resolve(
        f"plugin/{catalog.plugins[0].plugin_id}/review"
    ).source == f"plugin:{catalog.plugins[0].plugin_id}"
    mcp = manager.mcp_servers(catalog, workspace=workspace)
    assert mcp.diagnostics == ()
    assert len(mcp.servers) == 1
    assert mcp.servers[0].args[0].endswith("/server.js")
    assert mcp.servers[0].args[1] == str(manager.store.data_path(catalog.plugins[0]))
    assert mcp.servers[0].args[2] == str(workspace)
    assert mcp.servers[0].cwd == str(workspace)


def test_hybrid_keeps_portable_and_claude_mcp_semantics_separate(
    tmp_path: Path,
) -> None:
    """双 manifest 各自使用自己的 MCP schema 和运行字段。"""
    source = tmp_path / "hybrid-mcp"
    source.mkdir()
    _write_json(
        source / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "hybrid-mcp",
            "version": "1.0.0",
        },
    )
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {"name": "hybrid-mcp", "version": "1.0.0"},
    )
    _write_json(
        source / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "portable": {
                    "type": "stdio",
                    "command": "python3",
                    "args": [],
                }
            },
        },
    )
    _write_json(
        source / ".mcp.json",
        {
            "mcpServers": {
                "claude": {
                    "type": "stdio",
                    "command": "python3",
                    "args": [],
                    "timeout": 7,
                }
            }
        },
    )
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    result = manager.mcp_servers(manager.catalog(), workspace=tmp_path / "workspace")
    assert result.diagnostics == ()
    assert {server.name.rsplit("__", 1)[-1] for server in result.servers} == {
        "portable",
        "claude",
    }
    by_name = {server.name.rsplit("__", 1)[-1]: server for server in result.servers}
    assert by_name["portable"].timeout_seconds == 30
    assert by_name["claude"].timeout_seconds == 7


def test_hybrid_fatal_portable_mcp_isolated_from_claude_servers(
    tmp_path: Path,
) -> None:
    """portable mcp.json 顶层致命错误只隔离该 manifest，Claude server 继续。"""
    source = tmp_path / "hybrid-mcp-fatal-portable"
    source.mkdir()
    _write_json(
        source / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "hybrid-mcp-fatal-portable",
            "version": "1.0.0",
        },
    )
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {"name": "hybrid-mcp-fatal-portable", "version": "1.0.0"},
    )
    # unknown 顶层字段触发 portable closed schema 的致命错误。
    _write_json(
        source / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {},
            "timeout": 15,
        },
    )
    _write_json(
        source / ".mcp.json",
        {
            "mcpServers": {
                "claude": {"type": "stdio", "command": "python3", "args": []}
            }
        },
    )
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    result = manager.mcp_servers(manager.catalog(), workspace=tmp_path / "workspace")
    assert {server.name.rsplit("__", 1)[-1] for server in result.servers} == {"claude"}
    assert len(result.diagnostics) == 1
    assert "portable mcp.json" in result.diagnostics[0]
    assert "PLUGIN_MCP_INVALID" in result.diagnostics[0]


def test_hybrid_fatal_claude_mcp_isolated_from_portable_servers(
    tmp_path: Path,
) -> None:
    """Claude `.mcp.json` 根节点致命错误只隔离该 manifest，portable server 继续。"""
    source = tmp_path / "hybrid-mcp-fatal-claude"
    source.mkdir()
    _write_json(
        source / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "hybrid-mcp-fatal-claude",
            "version": "1.0.0",
        },
    )
    _write_json(
        source / ".claude-plugin" / "plugin.json",
        {"name": "hybrid-mcp-fatal-claude", "version": "1.0.0"},
    )
    _write_json(
        source / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "portable": {
                    "type": "stdio",
                    "command": "python3",
                    "args": [],
                }
            },
        },
    )
    # 根节点不是 servers mapping，Claude 侧按组件隔离处理。
    _write_json(source / ".mcp.json", {"mcpServers": "not-a-mapping"})
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    result = manager.mcp_servers(manager.catalog(), workspace=tmp_path / "workspace")
    assert {server.name.rsplit("__", 1)[-1] for server in result.servers} == {"portable"}
    assert len(result.diagnostics) == 1
    assert "claude MCP" in result.diagnostics[0]
    assert "PLUGIN_MCP_INVALID" in result.diagnostics[0]


def test_claude_and_portable_commands_become_user_only_command_records(
    tmp_path: Path,
) -> None:
    """两种 Plugin Command 都复用 Agent 端正文校验，快照不包含正文或路径。"""
    from harness_agent.extensions.plugin_skills import SkillRegistry

    claude = tmp_path / "claude-commands"
    claude.mkdir()
    _write_json(
        claude / ".claude-plugin" / "plugin.json",
        {"name": "claude-commands", "version": "1.0.0"},
    )
    command = claude / "commands" / "review.md"
    command.parent.mkdir()
    command.write_text(
        "---\ndescription: Review selected files\nargument-hint: <paths>\n---\n"
        "Review $ARGUMENTS carefully.\n",
        encoding="utf-8",
    )
    plain = claude / "commands" / "summarize.md"
    plain.write_text("Summarize the current changes.\n", encoding="utf-8")
    (claude / "commands" / "Bad Name.md").write_text(
        "This invalid command must be isolated.\n",
        encoding="utf-8",
    )

    portable = tmp_path / "portable-commands"
    portable.mkdir()
    _write_json(
        portable / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-commands",
            "extensions": {
                "com.za38.harness": {
                    "schemaVersion": "1.0.0",
                    "commands": "./commands",
                }
            },
        },
    )
    portable_command = portable / "commands" / "audit.md"
    portable_command.parent.mkdir()
    portable_command.write_text(
        "---\ndescription: Audit changes\n---\nAudit the requested change.\n",
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    for source in (claude, portable):
        installed = manager.install(source)["plugin"]
        assert isinstance(installed, dict)
        components = {item["kind"]: item for item in installed["components"]}
        assert components["commands"]["status"] == "supported"
        assert components["commands"]["effective"] is True
        manager.set_enabled(
            str(installed["id"]),
            enabled=True,
            capability_fingerprint=str(installed["capability_fingerprint"]),
        )
    catalog = manager.catalog()
    result = manager.skill_sources(catalog)
    registry = SkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
        plugin_diagnostics=result.diagnostics,
    )
    commands = registry.agent_commands()
    plugin_ids = {plugin.name: plugin.plugin_id for plugin in catalog.plugins}
    assert {item["name"] for item in commands} == {
        f"plugin:{plugin_ids['claude-commands'].replace('/', ':')}:review",
        f"plugin:{plugin_ids['claude-commands'].replace('/', ':')}:summarize",
        f"plugin:{plugin_ids['portable-commands'].replace('/', ':')}:audit",
    }
    assert all("body" not in item and "path" not in item for item in commands)
    assert any("Bad Name" in diagnostic for diagnostic in registry.diagnostics)
    review = registry.resolve(
        f"plugin/{plugin_ids['claude-commands']}/command/review"
    )
    assert review.kind == "command"
    assert review.user_invocable is True
    assert review.model_invocable is False
    assert registry.load(review.skill_id, "src").body == "Review $ARGUMENTS carefully."
    assert "command/review" not in registry.system_prompt_fragment()


def test_plugin_package_update_changes_skill_and_mcp_snapshots(tmp_path: Path) -> None:
    """同名 Plugin 内容更新后，Skill 与 MCP 身份都绑定新 package digest。"""
    from harness_agent.extensions.mcp import build_mcp_snapshot
    from harness_agent.extensions.plugin_skills import SkillRegistry

    source = tmp_path / "versioned-plugin"
    source.mkdir()
    _portable_plugin(source)
    executable = source / "bin" / "check"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    resource = source / "skills" / "review" / "reference.txt"
    resource.write_text("version one", encoding="utf-8")
    manager = PluginManager(home=tmp_path / "home")

    def runtime_ids() -> tuple[str, str, str]:
        installed = manager.install(source)["plugin"]
        assert isinstance(installed, dict)
        manager.set_enabled(
            str(installed["id"]),
            enabled=True,
            capability_fingerprint=str(installed["capability_fingerprint"]),
        )
        catalog = manager.catalog()
        skill_result = manager.skill_sources(catalog)
        registry = SkillRegistry(
            tmp_path / "workspace",
            home=tmp_path / "home",
            plugin_sources=skill_result.sources,
        )
        mcp_result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")
        mcp_snapshot = build_mcp_snapshot(mcp_result.servers, "config-revision")
        return catalog.plugins[0].package_digest, registry.snapshot_id, mcp_snapshot.digest

    first = runtime_ids()
    resource.write_text("version two", encoding="utf-8")
    second = runtime_ids()
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[2] != second[2]


def test_zip_with_parent_traversal_is_rejected_before_validation(tmp_path: Path) -> None:
    """zip 路径逃逸不能在 staging 外创建文件。"""
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escaped.txt", "bad")
        package.writestr(
            "plugin/plugin.json",
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "bad-plugin",
                }
            ),
        )

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(archive)
    assert rejected.value.code == "PLUGIN_ARCHIVE_PATH_INVALID"
    assert not (tmp_path / "escaped.txt").exists()


def test_directory_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    """本地目录来源不能通过链接绕过包边界或内容身份。"""
    target = tmp_path / "target.txt"
    target.write_text("outside", encoding="utf-8")
    symlink_plugin = tmp_path / "symlink-plugin"
    symlink_plugin.mkdir()
    _portable_plugin(symlink_plugin)
    (symlink_plugin / "linked.txt").symlink_to(target)
    with pytest.raises(PluginError) as symlink_error:
        PluginManager(home=tmp_path / "home-a").validate(symlink_plugin)
    assert symlink_error.value.code == "PLUGIN_SYMLINK_REJECTED"

    hardlink_plugin = tmp_path / "hardlink-plugin"
    hardlink_plugin.mkdir()
    _portable_plugin(hardlink_plugin)
    original = hardlink_plugin / "original.txt"
    original.write_text("same inode", encoding="utf-8")
    os.link(original, hardlink_plugin / "copy.txt")
    with pytest.raises(PluginError) as hardlink_error:
        PluginManager(home=tmp_path / "home-b").validate(hardlink_plugin)
    assert hardlink_error.value.code == "PLUGIN_HARDLINK_REJECTED"


def test_directory_staging_excludes_local_metadata_without_copying_sentinel_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地 checkout 的 VCS、OS 元数据和凭据文件不会进入 staged package。"""
    source = tmp_path / "checkout"
    source.mkdir()
    _portable_plugin(source)
    metadata = source / ".git"
    metadata.mkdir()
    (metadata / "HEAD").write_text("VCS_SENTINEL", encoding="utf-8")
    (source / ".env").write_text("ENV_ROOT_SENTINEL", encoding="utf-8")
    (source / ".env.dev").write_text("ENV_SENTINEL", encoding="utf-8")
    (source / ".npmrc").write_text("NPMRC_SENTINEL", encoding="utf-8")
    (source / ".DS_Store").write_text("OS_SENTINEL", encoding="utf-8")
    socket_path = metadata / "fsmonitor--daemon.ipc"

    # Codex sandbox 不允许创建 Unix socket inode；通过 os.walk/lstat 注入
    # 等价的 socket 目录项，仍能证明“先按 .git 剪枝、再 lstat”这一安全边界。
    real_walk = os.walk
    real_lstat = Path.lstat

    def fake_walk(*args: object, **kwargs: object):
        for root, directories, files in real_walk(*args, **kwargs):  # type: ignore[arg-type]
            if Path(root) == metadata:
                files = [*files, socket_path.name]
            yield root, directories, files

    def fake_lstat(path: Path):
        if path == socket_path:
            return os.stat_result((stat.S_IFSOCK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        return real_lstat(path)

    monkeypatch.setattr(os, "walk", fake_walk)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]

    assert isinstance(installed, dict)
    record = manager.store.read_registry().plugins[0]
    package = manager.store.package_path(record)
    assert not any(
        part in {".git", ".env", ".env.dev", ".npmrc", ".DS_Store"}
        for path in package.rglob("*")
        for part in path.relative_to(package).parts
    )
    public_outputs = json.dumps(
        {
            "install": installed,
            "list": manager.list(),
            "snapshot": manager.resource_snapshot(record.plugin_id).to_dict(),
            "preview": manager.static_preview(),
        },
        ensure_ascii=False,
    )
    assert "VCS_SENTINEL" not in public_outputs
    assert "ENV_SENTINEL" not in public_outputs
    assert "ENV_ROOT_SENTINEL" not in public_outputs
    assert "NPMRC_SENTINEL" not in public_outputs
    assert "OS_SENTINEL" not in public_outputs


def test_directory_staging_still_rejects_special_file_outside_exclusions(
    tmp_path: Path,
) -> None:
    """排除目录之外的 FIFO 仍按原有 fail-closed 规则拒绝。"""
    source = tmp_path / "special-plugin"
    source.mkdir()
    _portable_plugin(source)
    special = source / "unexpected.fifo"
    try:
        os.mkfifo(special)
    except (AttributeError, OSError):
        pytest.skip("当前宿主不支持创建 FIFO fixture")

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code == "PLUGIN_SPECIAL_FILE_REJECTED"


def test_zip_staging_excludes_metadata_but_rejects_other_special_entries(
    tmp_path: Path,
) -> None:
    """ZIP 中的本地元数据安全忽略，非排除特殊条目保持稳定拒绝。"""
    source = tmp_path / "zip-source"
    source.mkdir()
    _portable_plugin(source)
    archive = tmp_path / "metadata.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(source).as_posix())
        package.writestr(".git/HEAD", "ZIP_VCS_SENTINEL")
        package.writestr(".env", "ZIP_ENV_ROOT_SENTINEL")
        package.writestr(".env.dev", "ZIP_ENV_SENTINEL")
        package.writestr(".npmrc", "ZIP_NPMRC_SENTINEL")
        package.writestr(".DS_Store", "ZIP_OS_SENTINEL")

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(archive)["plugin"]
    assert isinstance(installed, dict)
    record = manager.store.read_registry().plugins[0]
    package = manager.store.package_path(record)
    assert not any(
        part in {".git", ".env", ".env.dev", ".npmrc", ".DS_Store"}
        for path in package.rglob("*")
        for part in path.relative_to(package).parts
    )
    assert "ZIP_VCS_SENTINEL" not in json.dumps(manager.list(), ensure_ascii=False)
    assert "ZIP_ENV_SENTINEL" not in json.dumps(manager.list(), ensure_ascii=False)
    assert "ZIP_ENV_ROOT_SENTINEL" not in json.dumps(manager.list(), ensure_ascii=False)

    special_archive = tmp_path / "special.zip"
    special_info = zipfile.ZipInfo("unexpected.fifo")
    special_info.external_attr = stat.S_IFIFO << 16
    with zipfile.ZipFile(special_archive, "w") as package:
        package.writestr(
            "plugin.json",
            json.dumps({"name": "special-plugin", "version": "1.0.0"}),
        )
        package.writestr(special_info, "not-a-real-fifo")
    with pytest.raises(PluginError) as rejected:
        manager.validate(special_archive)
    assert rejected.value.code == "PLUGIN_SPECIAL_FILE_REJECTED"


def test_package_digest_is_deterministic_and_includes_executable_bit(tmp_path: Path) -> None:
    """相同内容得到相同 digest，可执行能力变化会改变内容身份。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        path = root / "bin" / "tool"
        path.parent.mkdir()
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o600)
    assert package_digest(first) == package_digest(second)

    (second / "bin" / "tool").chmod(0o700)
    assert package_digest(first) != package_digest(second)


def test_corrupt_registry_fails_closed(tmp_path: Path) -> None:
    """损坏 registry 不会被 list/install 当作空文件覆盖。"""
    home = tmp_path / "home"
    registry = home / ".harness" / "plugins" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{broken", encoding="utf-8")
    manager = PluginManager(home=home)
    with pytest.raises(PluginError) as rejected:
        manager.list()
    assert rejected.value.code == "PLUGIN_REGISTRY_INVALID"


def test_legacy_registry_version_fails_closed(tmp_path: Path) -> None:
    """旧 registry 版本不能被当作当前 registry 读取或覆盖。"""
    home = tmp_path / "home"
    registry = home / ".harness" / "plugins" / "registry.json"
    registry.parent.mkdir(parents=True)
    legacy = json.dumps({"version": 1, "revision": 0, "plugins": []}, sort_keys=True)
    registry.write_text(legacy, encoding="utf-8")
    manager = PluginManager(home=home)

    with pytest.raises(PluginError) as rejected:
        manager.list()

    assert rejected.value.code == "PLUGIN_REGISTRY_INVALID"
    assert registry.read_text(encoding="utf-8") == legacy
