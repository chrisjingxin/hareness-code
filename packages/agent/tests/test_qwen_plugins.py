"""Qwen/DevAgent Extension 分阶段 Adapter 与 canonical 接入的离线回归测试。"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.config.config import ExecutionSettings, ModelCatalog, ModelProfile, ModelSettings
from harness_agent.extensions.mcp import build_mcp_snapshot
from harness_agent.extensions.skills import SkillRegistry
from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.model import PluginComponentReport, PluginError
from harness_agent.runtime.agent_catalog import (
    AgentCatalog,
    DelegationPolicy,
    EffectiveExecutionPolicy,
    ExecutionPolicyDefinition,
    NetworkPolicy,
    ShellPolicy,
    StringRule,
)
from harness_agent.runtime.agent_spec import resolve_plugin_agent_spec
from harness_agent.threads.context_lifecycle import (
    ContextAuthority,
    ContextLifecycle,
    ContextStability,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"


def _copy_fixture(tmp_path: Path) -> Path:
    """复制清洁 fixture 到临时目录，测试不会读取开发期扩展目录。"""
    target = tmp_path / f"za38-devagent-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _write_manifest(root: Path, name: str, value: dict[str, object]) -> Path:
    """写入单一 Qwen 家族清单并返回路径。"""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _manifest(root: Path) -> dict[str, object]:
    """读取 fixture 清单供边界测试复制。"""
    return json.loads((root / "devagent-extension.json").read_text(encoding="utf-8"))


def _components(result: dict[str, object]) -> dict[str, dict[str, object]]:
    """按组件 kind 索引 JSON-RPC 风格的兼容报告。"""
    raw = result["components"]
    assert isinstance(raw, list)
    return {str(item["kind"]): item for item in raw if isinstance(item, dict)}


def _model_catalog() -> ModelCatalog:
    """构造不含真实凭据的 Qwen Agent 解析目录。"""
    return ModelCatalog(
        default_profile="fast",
        profiles={
            "fast": ModelProfile(
                "fast",
                ModelSettings("offline-model", "https://example.test"),
                "test",
            )
        },
        role_profiles={},
    )


def test_clean_za38_fixture_is_detected_as_qwen_with_static_inventory(
    tmp_path: Path,
) -> None:
    """DevAgent 清洁包应报告真实字段和组件数量，Agent 可进入 canonical 运行时。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]

    assert isinstance(summary, dict)
    assert summary["format"] == "qwen-code"
    assert summary["manifest"] == "devagent-extension.json"
    assert summary["name"] == "ZA38.03_CLI_EXTENSION"
    assert summary["version"] == "0.2.0"
    assert summary["can_enable"] is True

    components = _components(summary)
    assert {kind: components[kind]["count"] for kind in components} == {
        "agents": 3,
        "commands": 3,
        "contexts": 1,
        "hooks": 1,
        "mcp": 1,
        "skills": 1,
    }
    assert components["agents"]["status"] == "adapted"
    assert components["agents"]["effective"] is True
    assert components["contexts"]["status"] == "adapted"
    assert components["contexts"]["effective"] is True
    assert components["hooks"]["status"] == "adapted"
    assert components["hooks"]["effective"] is True
    assert components["commands"]["status"] == "unsupported"
    assert components["commands"]["effective"] is False
    assert components["skills"]["status"] == "unsupported"
    assert components["skills"]["effective"] is False
    assert components["mcp"]["status"] == "unsupported"
    assert components["mcp"]["effective"] is False
    assert components["contexts"]["sources"] == ["DEVAGENT.md"]
    assert components["hooks"]["capabilities"] == ["process:hook"]
    assert isinstance(summary["capability_fingerprint"], str)
    assert len(summary["capability_fingerprint"]) == 64


def test_trusted_qwen_subagent_stop_enters_canonical_runtime_catalog(
    tmp_path: Path,
) -> None:
    """trust+enable 后 Qwen Hook 进入统一 runtime catalog，但不启动进程。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    runtime_catalog = manager.runtime_catalog(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )

    assert len(runtime_catalog.hooks) == 1
    assert runtime_catalog.hooks[0].event == "SubagentStop"
    assert runtime_catalog.hooks[0].matcher == "^za38-(frontend|backend|java)-executor$"
    assert runtime_catalog.hooks[0].source_id
    assert runtime_catalog.diagnostics == ()


@pytest.mark.parametrize(
    "hooks",
    (
        {
            "SubagentStop": [
                {
                    "matcher": "^za38-frontend-executor$",
                    "hooks": [
                        {"type": "command", "command": "node scripts/gate.mjs"}
                    ],
                },
                {"hooks": [{"type": "command", "command": 123}]},
            ]
        },
        {
            "PostToolUse": [
                {
                    "matcher": "^za38-frontend-executor$",
                    "hooks": [
                        {"type": "command", "command": "node scripts/gate.mjs"}
                    ],
                }
            ]
        },
        {
            "SubagentStop": [
                {
                    "matcher": "^za38-frontend-executor$",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "node scripts/gate.mjs",
                            "async": True,
                        }
                    ],
                }
            ]
        },
    ),
)
def test_qwen_invalid_or_out_of_scope_hooks_never_enter_runtime(
    tmp_path: Path,
    hooks: dict[str, object],
) -> None:
    """Hook 组件 invalid/async/范围外时，即使 Plugin 可启用也不进入 runtime。"""
    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value["hooks"] = hooks
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["hooks"]
    assert component["status"] == "invalid"
    assert component["effective"] is False
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["can_enable"] is True
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    runtime_catalog = manager.runtime_catalog(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )

    assert runtime_catalog.hooks == ()
    assert any(
        "PLUGIN_QWEN_HOOK_COMPONENT_DISABLED" in diagnostic
        for diagnostic in runtime_catalog.diagnostics
    )


def test_qwen_runtime_hook_definition_rejects_async_subagent_stop(
    tmp_path: Path,
) -> None:
    """runtime definition seam 也拒绝 Qwen SubagentStop async handler。"""
    from harness_agent.plugins.runtime import PluginRuntimeError, _hook_definition

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    plugin = manager.store.read_registry().plugins[-1]
    root = manager.store.package_path(plugin)
    data = manager.store.data_path(plugin)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(PluginRuntimeError) as error:
        _hook_definition(
            plugin,
            root,
            data,
            workspace,
            "SubagentStop",
            "*",
            {"type": "command", "command": "node gate.mjs", "async": True},
            {},
        )

    assert error.value.code == "PLUGIN_HOOK_SUBAGENT_STOP_ASYNC_UNSUPPORTED"


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    (
        ("matcher", "x" * 513, "matcher"),
        ("command", "x" * 32_769, "command"),
        ("timeout", True, "timeout"),
        ("timeout", 601, "timeout"),
        ("shell", "fish", "shell"),
        ("args", 123, "args"),
        ("env", {"TOKEN": "must-not-run"}, "env"),
    ),
)
def test_qwen_hook_report_matches_canonical_runtime_validation(
    tmp_path: Path,
    field: str,
    value: object,
    diagnostic: str,
) -> None:
    """静态 adapted 报告必须保证 canonical runtime 一定能构造。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    group: dict[str, object] = {
        "matcher": "^za38-frontend-executor$",
        "hooks": [
            {
                "type": "command",
                "command": "node scripts/za38-git-commit-gate.mjs",
            }
        ],
    }
    if field == "matcher":
        group[field] = value
    else:
        handler = group["hooks"][0]
        assert isinstance(handler, dict)
        handler[field] = value
    manifest["hooks"] = {"SubagentStop": [group]}
    (source / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["hooks"]
    assert component["status"] == "invalid"
    assert component["effective"] is False
    assert any(
        diagnostic.lower() in str(item).lower()
        for item in component["diagnostics"]
    )

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    def stale_hook_report(current: object) -> tuple[object, ...]:
        assert hasattr(current, "plugins")
        return tuple(
            replace(
                plugin,
                components=tuple(
                    replace(
                        item,
                        status="adapted",
                        effective=True,
                        capabilities=("process:hook",),
                        diagnostics=("stale report",),
                    )
                    if plugin.plugin_id == plugin_id and item.kind == "hooks"
                    else item
                    for item in plugin.components
                ),
            )
            if plugin.plugin_id == plugin_id
            else plugin
            for plugin in current.plugins  # type: ignore[attr-defined]
        )

    manager.store.mutate_registry(stale_hook_report)  # type: ignore[arg-type]
    runtime_catalog = manager.runtime_catalog(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )
    assert runtime_catalog.hooks == ()
    assert runtime_catalog.hook_failures
    assert any(
        diagnostic.upper() in item.upper()
        for item in runtime_catalog.diagnostics
    )


def test_qwen_stale_adapted_hook_runtime_failure_is_observable_and_fail_closed(
    tmp_path: Path,
) -> None:
    """损坏的安装报告不能因 runtime 转换失败而退化为无 gate 放行。"""
    from harness_agent.plugins.runtime import PluginRuntimeCatalog

    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    manifest["hooks"] = {
        "SubagentStop": [
            {
                "matcher": "x" * 513,
                "hooks": [
                    {
                        "type": "command",
                        "command": "node scripts/za38-git-commit-gate.mjs",
                    }
                ],
            }
        ]
    }
    (source / "devagent-extension.json").write_text(
        json.dumps(manifest),
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

    def corrupt_report(current: object) -> tuple[object, ...]:
        assert hasattr(current, "plugins")
        plugins = []
        for plugin in current.plugins:  # type: ignore[attr-defined]
            if plugin.plugin_id != plugin_id:
                plugins.append(plugin)
                continue
            components = tuple(
                replace(
                    component,
                    status="adapted",
                    effective=True,
                    capabilities=("process:hook",),
                    diagnostics=("stale report",),
                )
                if component.kind == "hooks"
                else component
                for component in plugin.components
            )
            plugins.append(replace(plugin, components=components))
        return tuple(plugins)

    manager.store.mutate_registry(corrupt_report)  # type: ignore[arg-type]
    runtime_catalog = manager.runtime_catalog(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )

    assert isinstance(runtime_catalog, PluginRuntimeCatalog)
    assert runtime_catalog.hooks == ()
    assert runtime_catalog.hook_failures
    assert any(
        "PLUGIN_HOOK_MATCHER_INVALID" in diagnostic
        for diagnostic in runtime_catalog.diagnostics
    )


def test_qwen_context_uses_default_qwen_md_for_missing_or_empty_array(
    tmp_path: Path,
) -> None:
    """标准 Qwen 清单缺省或空数组时只报告实际存在的根 QWEN.md。"""
    source = tmp_path / "qwen-default-context"
    source.mkdir()
    (source / "QWEN.md").write_text("# context\n", encoding="utf-8")
    _write_manifest(source, "qwen-extension.json", {"name": "context-default"})

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["contexts"]
    assert component["count"] == 1
    assert component["sources"] == ["QWEN.md"]

    empty_array = tmp_path / "qwen-empty-context"
    empty_array.mkdir()
    (empty_array / "QWEN.md").write_text("# context\n", encoding="utf-8")
    _write_manifest(
        empty_array,
        "qwen-extension.json",
        {"name": "context-empty", "contextFileName": []},
    )
    empty_summary = PluginManager(home=tmp_path / "empty-home").validate(empty_array)["plugin"]
    assert isinstance(empty_summary, dict)
    assert _components(empty_summary)["contexts"]["sources"] == ["QWEN.md"]

    missing_default = tmp_path / "qwen-missing-default"
    missing_default.mkdir()
    _write_manifest(missing_default, "qwen-extension.json", {"name": "no-context"})
    missing_summary = PluginManager(home=tmp_path / "missing-home").validate(missing_default)["plugin"]
    assert isinstance(missing_summary, dict)
    assert "contexts" not in _components(missing_summary)


def test_qwen_context_accepts_explicit_string_array(tmp_path: Path) -> None:
    """Qwen contextFileName string[] 只报告数组中的包内普通文件。"""
    source = tmp_path / "qwen-context-array"
    source.mkdir()
    (source / "ONE.md").write_text("one\n", encoding="utf-8")
    (source / "TWO.md").write_text("two\n", encoding="utf-8")
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "context-array", "contextFileName": ["ONE.md", "TWO.md"]},
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]

    assert isinstance(summary, dict)
    component = _components(summary)["contexts"]
    assert component["count"] == 2
    assert component["sources"] == ["ONE.md", "TWO.md"]


@pytest.mark.parametrize(
    ("context_value", "error_code"),
    (
        ("MISSING.md", "PLUGIN_COMPONENT_MISSING"),
        (["ONE.md", "../outside.md"], "PLUGIN_COMPONENT_PATH_INVALID"),
        ("/tmp/outside.md", "PLUGIN_COMPONENT_PATH_INVALID"),
    ),
)
def test_qwen_context_explicit_paths_keep_fail_closed_boundaries(
    tmp_path: Path,
    context_value: object,
    error_code: str,
) -> None:
    """显式 Context 路径缺失、越界或绝对路径必须稳定失败。"""
    source = tmp_path / "qwen-explicit-context"
    source.mkdir()
    if isinstance(context_value, list):
        (source / "ONE.md").write_text("one\n", encoding="utf-8")
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "context-boundary", "contextFileName": context_value},
    )

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code == error_code


def test_devagent_context_keeps_explicit_string_contract(tmp_path: Path) -> None:
    """ZA38 DevAgent 字符串 contextFileName 保持兼容，数组语义暂不猜测。"""
    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value["contextFileName"] = ["DEVAGENT.md"]
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code == "PLUGIN_MANIFEST_FIELD_INVALID"


def test_qwen_install_exposes_static_resource_snapshot_with_virtual_paths(
    tmp_path: Path,
) -> None:
    """安装后的 ZA38 资产进入只读快照，摘要只使用虚拟路径。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")

    installed = manager.install(source)
    assert isinstance(installed, dict)
    snapshot = installed["resource_snapshot"]
    assert isinstance(snapshot, dict)
    plugin_id = str(installed["plugin"]["id"])
    assert snapshot["plugin_id"] == plugin_id
    assert snapshot["virtual_root"] == f"/.harness/plugins/{plugin_id}"
    assert snapshot["read_only"] is True
    assert snapshot["counts"] == {
        "agents": 3,
        "commands": 3,
        "contexts": 1,
        "hooks": 1,
        "mcp": 1,
        "skills": 1,
    }

    resources = snapshot["resources"]
    assert isinstance(resources, list)
    assert resources
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert all(
        isinstance(item, dict)
        and str(item["virtual_path"]).startswith(f"/.harness/plugins/{plugin_id}/")
        and item["read_only"] is True
        for item in resources
    )
    mcp = [item for item in resources if item["kind"] == "mcp"]
    assert len(mcp) == 1
    assert mcp[0]["runnable"] is False
    assert mcp[0]["args"] == [
        f"/.harness/plugins/{plugin_id}/mcp/context-server.mjs"
    ]
    expected_assets = {
        "references/root-guide.md",
        "scripts/za38-index.mjs",
        "scripts/za38-git-commit-gate.mjs",
        "mcp/context-server.mjs",
    }
    assert expected_assets <= {
        str(item["source"])
        for item in resources
    }
    assert all(
        not str(item["source"]).startswith("skills/za38-framework/references/")
        for item in resources
    )
    context_server = f"/.harness/plugins/{plugin_id}/mcp/context-server.mjs"
    assert manager.resource_snapshot(plugin_id).read_text(context_server).startswith(
        "#!/usr/bin/env node"
    )
    object_snapshot = manager.resource_snapshot(plugin_id)
    object_mcp = next(asset for asset in object_snapshot.resources if asset.kind == "mcp")
    with pytest.raises(TypeError):
        object_mcp.metadata["args"][0] = "mutated"  # type: ignore[index]
    hook = next(item for item in resources if item["kind"] == "hooks")
    assert hook["events"] == [
        {
            "event": "SubagentStop",
            "matcher": "^za38-(frontend|backend|java)-executor$",
            "handlers": [
                {
                    "type": "command",
                    "name": "za38-git-commit-gate",
                    "command": f'node "{snapshot["virtual_root"]}/scripts/za38-git-commit-gate.mjs"',
                }
            ],
        }
    ]
    command = next(
        item
        for item in resources
        if item["kind"] == "commands" and item["source"] == "commands/za38-index.md"
    )
    assert command["placeholder_targets"] == [
        f"{snapshot['virtual_root']}/scripts/za38-index.mjs"
    ]
    assert (
        f"{snapshot['virtual_root']}/scripts/za38-index.mjs"
        in manager.resource_snapshot(plugin_id).read_text(command["virtual_path"])
    )
    listed = manager.list()["resource_snapshots"]
    assert listed == [snapshot]


def test_qwen_snapshot_excludes_hidden_and_credential_named_assets(
    tmp_path: Path,
) -> None:
    """静态资源闭包排除 .env、凭据命名文件和隐藏开发文件。"""
    source = _copy_fixture(tmp_path)
    (source / "scripts" / ".env.local").write_text("SECRET=not-read\n", encoding="utf-8")
    (source / "scripts" / "credentials.json").write_text("{}\n", encoding="utf-8")
    (source / "references" / ".dev-note.md").write_text("dev-only\n", encoding="utf-8")

    snapshot = PluginManager(home=tmp_path / "home").install(source)["resource_snapshot"]
    assert all(
        not any(
            part.startswith(".")
            or part in {"credentials.json", ".env.local"}
            for part in Path(str(item["source"])).parts
        )
        for item in snapshot["resources"]
    )


def test_qwen_resource_snapshot_reads_skill_references_and_rejects_escape(
    tmp_path: Path,
) -> None:
    """Skill 的相对 references 进入快照，越界路径仍被拒绝。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    snapshot = manager.resource_snapshot(str(installed["id"]))

    skill = next(
        asset
        for asset in snapshot.resources
        if asset.kind == "skills" and asset.source.endswith("SKILL.md")
    )
    assert snapshot.read_relative_text(
        skill.virtual_path,
        "../../references/root-guide.md",
    ).startswith("ZA38 offline root reference")

    with pytest.raises(PluginError) as escaped:
        snapshot.read_text(f"{snapshot.virtual_root}/skills/../agents/za38-backend-executor.md")
    assert escaped.value.code == "PLUGIN_RESOURCE_PATH_INVALID"


def test_qwen_resource_snapshot_resolves_known_placeholders_only(
    tmp_path: Path,
) -> None:
    """MCP 静态摘要只在已知字段内将四种根 placeholder 转成虚拟路径。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    server = manifest["mcpServers"]["za38.03_code_index"]
    server["args"] = [
        "<extensionPath>/scripts/za38-index.mjs",
        "${extensionPath}/scripts/za38-git-commit-gate.mjs",
        "${extensionPath}${/}mcp${/}context-server.mjs",
        "${CLAUDE_PLUGIN_ROOT}/scripts/za38-index.mjs",
    ]
    (source / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    mcp = [
        asset
        for asset in manager.resource_snapshot(plugin_id).resources
        if asset.kind == "mcp"
    ]
    assert len(mcp) == 1
    assert list(mcp[0].metadata["args"]) == [
        f"/.harness/plugins/{plugin_id}/scripts/za38-index.mjs",
        f"/.harness/plugins/{plugin_id}/scripts/za38-git-commit-gate.mjs",
        f"/.harness/plugins/{plugin_id}/mcp/context-server.mjs",
        f"/.harness/plugins/{plugin_id}/scripts/za38-index.mjs",
    ]
    assert str(tmp_path) not in json.dumps(mcp[0].to_dict(), ensure_ascii=False)


def test_qwen_untrusted_process_assets_are_static_only(tmp_path: Path) -> None:
    """未 trust 的安装记录不进入 enabled catalog，也不会产生可启动 MCP。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["enabled"] is False
    assert manager.catalog().plugins == ()
    assert manager.mcp_servers(
        manager.catalog(), workspace=tmp_path / "workspace"
    ).servers == ()
    snapshot = manager.resource_snapshot(str(installed["id"]))
    assert all(
        not asset.metadata.get("runnable", False)
        for asset in snapshot.resources
        if asset.kind == "mcp"
    )


@pytest.mark.parametrize(
    ("argument", "error_code"),
    (
        ("--file=${extensionPath}/../outside.mjs", "PLUGIN_RESOURCE_PATH_INVALID"),
        ("--file=prefix/${extensionPath}/scripts/za38-index.mjs", "PLUGIN_RESOURCE_PATH_INVALID"),
        ("--file=${UNKNOWN_ROOT}/scripts/za38-index.mjs", "PLUGIN_RESOURCE_PLACEHOLDER_INVALID"),
        ("--file=<extensionPath>/scripts/missing.mjs", "PLUGIN_RESOURCE_TARGET_MISSING"),
        ("--file=/tmp/outside.mjs", "PLUGIN_RESOURCE_PATH_INVALID"),
    ),
)
def test_qwen_embedded_placeholder_paths_fail_closed(
    tmp_path: Path,
    argument: str,
    error_code: str,
) -> None:
    """MCP 参数中的嵌入 token、未知根、缺失目标和宿主路径都稳定失败。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    server = manifest["mcpServers"]["za38.03_code_index"]
    server["args"] = [argument]
    (source / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").install(source)
    assert rejected.value.code == error_code


def test_qwen_static_preview_lists_are_disabled_and_non_runnable(
    tmp_path: Path,
) -> None:
    """现有 Plugin 列表链路分别暴露四类静态 preview，不进入可执行 catalog。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)

    preview = manager.static_preview()
    assert {kind: len(items) for kind, items in preview.items()} == {
        "commands": 3,
        "skills": 1,
        "agents": 3,
        "mcp": 1,
    }
    for kind, items in preview.items():
        assert all(
            item["kind"] == kind
            and item["enabled"] is False
            and item["static"] is True
            and item["runnable"] is False
            and item["read_only"] is True
            and str(item["virtual_path"]).startswith("/.harness/plugins/")
            for item in items
        )
    assert {item["name"] for item in preview["commands"]} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }
    assert {item["name"] for item in preview["skills"]} == {"za38-framework"}
    assert {item["name"] for item in preview["agents"]} == {
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    }
    assert {item["name"] for item in preview["mcp"]} == {"za38.03_code_index"}
    listed = manager.list()
    assert listed["static_preview"] == preview
    assert str(tmp_path) not in json.dumps(listed, ensure_ascii=False)


async def test_qwen_static_preview_is_exposed_by_host_component_lists(
    tmp_path: Path,
) -> None:
    """Host 的 Skills/Agents/MCP 列表分别暴露静态预览而不改变运行列表。"""
    from types import SimpleNamespace
    from harness_agent.host.agent_host import AgentHost

    source = _copy_fixture(tmp_path)
    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    installed = server._plugin_manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    server._agent_catalog_for_control_plane = lambda: SimpleNamespace(
        snapshot_id="empty",
        diagnostics=(),
        list_agents=lambda: [],
    )

    initialized = await server._handle_initialize(
        {
            "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
            "client": {"name": "test", "version": "0.1.0", "kind": "test"},
            "capabilities": {"requests": [], "handles": []},
        },
        "initialize",
    )
    skills = await server._handle_skills_list({"include_disabled": True}, "skills")
    agents = await server._handle_agents_list({}, "agents")
    mcp = await server._handle_mcp_status({}, "mcp")

    assert len(initialized["static_command_preview"]) == 3
    assert len(skills["skills"]) == 0
    assert len(skills["static_preview"]) == 1
    assert skills["static_preview"][0]["name"] == "za38-framework"
    assert len(agents["agents"]) == 0
    assert {item["name"] for item in agents["static_preview"]} == {
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    }
    assert len(mcp["servers"]) == 0
    assert [item["name"] for item in mcp["static_preview"]] == [
        "za38.03_code_index"
    ]
    assert all(
        item["disabled"] is True
        and item["static"] is True
        and item["runnable"] is False
        for result in (skills, agents, mcp)
        for item in result["static_preview"]
    )


async def test_enabled_qwen_host_lists_effective_agents_and_keeps_other_previews(
    tmp_path: Path,
) -> None:
    """启用后 Host 四条列表按组件去重，且测试不连接 fixture MCP。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    installed = server._plugin_manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    server._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    source_result = server._plugin_manager.agent_sources(server._plugin_manager.catalog())
    server._agent_catalog_for_control_plane = lambda: AgentCatalog(
        model_catalog=_model_catalog(),
        sources=source_result.sources,
    )

    async def _offline_mcp_connect() -> None:
        """阻止管理列表测试启动任何 fixture MCP 进程。"""

    server._connect_mcp_servers = _offline_mcp_connect
    server._ensure_mcp_connected = _offline_mcp_connect

    initialized = await server._handle_initialize(
        {
            "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
            "client": {"name": "test", "version": "0.1.0", "kind": "test"},
            "capabilities": {"requests": [], "handles": []},
        },
        "initialize",
    )
    skills = await server._handle_skills_list({"include_disabled": True}, "skills")
    agents = await server._handle_agents_list({}, "agents")
    mcp = await server._handle_mcp_status({}, "mcp")

    assert len(initialized["static_command_preview"]) == 3
    assert len(skills["static_preview"]) == 1
    assert len(agents["agents"]) == 3
    assert agents["static_preview"] == []
    assert mcp["servers"] == []
    assert len(mcp["static_preview"]) == 1
    assert all(
        item["disabled"] is True
        and item["static"] is True
        and item["runnable"] is False
        for result in (initialized, skills, mcp)
        for item in result.get("static_command_preview", result.get("static_preview", []))
    )


async def test_host_static_preview_filters_non_qwen_plugins_from_all_lists(
    tmp_path: Path,
) -> None:
    """Host 四个 preview 分区只保留未接入运行时的 Qwen 资源。"""
    from types import SimpleNamespace

    from harness_agent.host.agent_host import AgentHost

    portable = Path(__file__).parent / "fixtures" / "agent_plugins" / "partial-components"
    qwen = _copy_fixture(tmp_path)
    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    server._plugin_manager.install(portable)
    server._plugin_manager.install(qwen)
    server._agent_catalog_for_control_plane = lambda: SimpleNamespace(
        snapshot_id="empty",
        diagnostics=(),
        list_agents=lambda: [],
    )

    initialized = await server._handle_initialize(
        {
            "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
            "client": {"name": "test", "version": "0.1.0", "kind": "test"},
            "capabilities": {"requests": [], "handles": []},
        },
        "initialize",
    )
    skills = await server._handle_skills_list({"include_disabled": True}, "skills")
    agents = await server._handle_agents_list({}, "agents")
    mcp = await server._handle_mcp_status({}, "mcp")

    assert {item["name"] for item in initialized["static_command_preview"]} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }
    assert [item["name"] for item in skills["static_preview"]] == [
        "za38-framework"
    ]
    assert {item["name"] for item in agents["static_preview"]} == {
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    }
    assert [item["name"] for item in mcp["static_preview"]] == [
        "za38.03_code_index"
    ]
    assert all(
        item["disabled"] is True
        and item["static"] is True
        and item["runnable"] is False
        for result in (skills, agents, mcp)
        for item in result["static_preview"]
    )


def test_qwen_installed_snapshot_is_independent_from_source_changes(
    tmp_path: Path,
) -> None:
    """安装后删除或篡改源目录不会改变已复制的只读资源快照。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    snapshot = manager.resource_snapshot(plugin_id)
    reference = next(
        asset
        for asset in snapshot.resources
        if asset.kind == "resources" and asset.source == "references/root-guide.md"
    )
    original = snapshot.read_text(reference.virtual_path)

    (source / "references" / "root-guide.md").write_text(
        "tampered source\n",
        encoding="utf-8",
    )
    shutil.rmtree(source)

    retained = manager.resource_snapshot(plugin_id)
    assert retained.read_text(reference.virtual_path) == original


def test_qwen_directory_and_zip_share_resource_snapshot_chain(
    tmp_path: Path,
) -> None:
    """目录与 ZIP 都经同一 staging/store 链后生成相同的静态库存。"""
    source = _copy_fixture(tmp_path)
    archive_path = tmp_path / "za38-devagent.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(
                    path,
                    (Path(source.name) / path.relative_to(source)).as_posix(),
                )

    def install_assets(
        package: Path,
        home_name: str,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = PluginManager(home=tmp_path / home_name)
        installation = manager.install(package)
        snapshot = installation["resource_snapshot"]
        assert isinstance(snapshot, dict)
        assert snapshot["counts"] == {
            "agents": 3,
            "commands": 3,
            "contexts": 1,
            "hooks": 1,
            "mcp": 1,
            "skills": 1,
        }
        virtual_root = str(snapshot["virtual_root"])
        assets = []
        for item in snapshot["resources"]:
            metadata = {
                key: value
                for key, value in item.items()
                if key in {"args", "events", "placeholder_targets", "target_paths"}
            }
            # 两种来源的 source_id 不同；只归一化虚拟根，保留完整资源形状。
            normalized_metadata = json.loads(
                json.dumps(metadata).replace(virtual_root, "/.harness/plugins/<id>")
            )
            assets.append(
                {
                    "kind": item["kind"],
                    "source": item["source"],
                    "relative_virtual_path": str(item["virtual_path"]).split(
                        f"{virtual_root}/", 1
                    )[1],
                    "digest": (
                        item["digest"]
                        if item["kind"] not in {"commands", "mcp", "hooks"}
                        else None
                    ),
                    "size": item["size"],
                    "metadata": normalized_metadata,
                }
            )
        return snapshot, assets

    directory_snapshot, directory_assets = install_assets(source, "home-directory")
    zip_snapshot, zip_assets = install_assets(archive_path, "home-zip")
    assert directory_snapshot["counts"] == zip_snapshot["counts"]
    assert directory_assets == zip_assets


def test_qwen_manifest_uses_default_component_directories(tmp_path: Path) -> None:
    """只有身份字段的标准 Qwen 清单也应扫描根目录默认组件目录。"""
    source = tmp_path / "qwen-default"
    (source / "commands").mkdir(parents=True)
    (source / "skills" / "default-skill").mkdir(parents=True)
    (source / "agents").mkdir()
    (source / "commands" / "default.md").write_text("# default\n", encoding="utf-8")
    (source / "skills" / "default-skill" / "SKILL.md").write_text(
        "---\nname: default-skill\ndescription: Default skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (source / "agents" / "default.md").write_text(
        "---\n"
        "name: default-agent\n"
        "description: Default agent.\n"
        "approvalMode: default\n"
        "---\n\n"
        "Read-only default agent.\n",
        encoding="utf-8",
    )
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "default-qwen", "version": "1.0.0"},
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]

    assert isinstance(summary, dict)
    components = _components(summary)
    assert {kind: components[kind]["count"] for kind in components} == {
        "agents": 1,
        "commands": 1,
        "skills": 1,
    }

    no_manifest = tmp_path / "ordinary-directories"
    (no_manifest / "commands").mkdir(parents=True)
    (no_manifest / "skills").mkdir()
    (no_manifest / "agents").mkdir()
    no_manifest_summary = PluginManager(home=tmp_path / "other-home").validate(no_manifest)["plugin"]
    assert isinstance(no_manifest_summary, dict)
    assert no_manifest_summary["format"] == "claude-code"

    skills_only = tmp_path / "skills-only"
    (skills_only / "skills").mkdir(parents=True)
    with pytest.raises(PluginError) as ambiguous:
        PluginManager(home=tmp_path / "third-home").validate(skills_only)
    assert ambiguous.value.code == "PLUGIN_FORMAT_AMBIGUOUS"


def test_qwen_install_persists_record_and_requires_explicit_runtime_enable(
    tmp_path: Path,
) -> None:
    """显式 install 可保存大写/下划线身份，启用仍需确认当前 capability。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source, format="qwen-code")["plugin"]

    assert isinstance(installed, dict)
    assert installed["format"] == "qwen-code"
    assert installed["name"] == "ZA38.03_CLI_EXTENSION"
    assert installed["enabled"] is False
    enabled = manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    assert enabled["plugin"]["enabled"] is True


@pytest.mark.parametrize("manifest_name", ("qwen-extension.json", "devagent-extension.json"))
def test_both_qwen_manifest_names_use_one_adapter(
    tmp_path: Path,
    manifest_name: str,
) -> None:
    """Qwen 和 DevAgent 清单名称只改变 manifest 来源，不改变格式。"""
    source = _copy_fixture(tmp_path)
    if manifest_name == "qwen-extension.json":
        source.joinpath("devagent-extension.json").rename(source / manifest_name)
    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]

    assert isinstance(summary, dict)
    assert summary["format"] == "qwen-code"
    assert summary["manifest"] == manifest_name


def test_explicit_qwen_requires_one_manifest_and_reports_conflicts(tmp_path: Path) -> None:
    """显式 qwen-code 不猜目录，家族或跨格式清单冲突必须失败关闭。"""
    manager = PluginManager(home=tmp_path / "home")
    source = tmp_path / "no-manifest"
    source.mkdir()
    (source / "commands").mkdir()
    (source / "commands" / "looks-like-qwen.md").write_text("# command\n", encoding="utf-8")
    with pytest.raises(PluginError) as missing:
        manager.validate(source, format="qwen-code")
    assert missing.value.code == "PLUGIN_FORMAT_MISMATCH"

    both = _copy_fixture(tmp_path)
    _write_manifest(both, "qwen-extension.json", _manifest(both))
    with pytest.raises(PluginError) as family_conflict:
        manager.validate(both, format="qwen-code")
    assert family_conflict.value.code == "PLUGIN_FORMAT_CONFLICT"

    cross = _copy_fixture(tmp_path)
    _write_manifest(
        cross,
        "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-cross-format",
        },
    )
    with pytest.raises(PluginError) as cross_conflict:
        manager.validate(cross, format="qwen-code")
    assert cross_conflict.value.code == "PLUGIN_FORMAT_CONFLICT"

    qwen_only = _copy_fixture(tmp_path)
    for requested in ("agent-plugins-1.0", "claude-code"):
        with pytest.raises(PluginError) as explicit_mismatch:
            manager.validate(qwen_only, format=requested)
        assert explicit_mismatch.value.code == "PLUGIN_FORMAT_MISMATCH"


@pytest.mark.parametrize("other_manifest", ("plugin.json", ".claude-plugin/plugin.json"))
def test_auto_rejects_qwen_cross_format_manifest_conflict(
    tmp_path: Path,
    other_manifest: str,
) -> None:
    """auto 也必须拒绝 Qwen 与 portable/Claude manifest 的混合歧义。"""
    source = _copy_fixture(tmp_path)
    path = source / other_manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    if other_manifest == "plugin.json":
        value = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-cross-format",
        }
    else:
        value = {"name": "claude-cross-format"}
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code == "PLUGIN_FORMAT_CONFLICT"


def test_qwen_static_validation_has_stable_json_identity_and_path_errors(
    tmp_path: Path,
) -> None:
    """坏清单、身份类型和包外路径不能被静态 Adapter 吞掉。"""
    manager = PluginManager(home=tmp_path / "home")
    source = _copy_fixture(tmp_path)
    manifest = source / "devagent-extension.json"
    manifest.write_text("{broken", encoding="utf-8")
    with pytest.raises(PluginError) as bad_json:
        manager.validate(source)
    assert bad_json.value.code == "PLUGIN_JSON_INVALID"

    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value["name"] = {"not": "a string"}
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PluginError) as bad_identity:
        manager.validate(source)
    assert bad_identity.value.code == "PLUGIN_NAME_INVALID"

    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value["contextFileName"] = "../outside.md"
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PluginError) as escaped:
        manager.validate(source)
    assert escaped.value.code == "PLUGIN_COMPONENT_PATH_INVALID"


def test_qwen_static_validation_rejects_malformed_mcp_and_hooks(
    tmp_path: Path,
) -> None:
    """字段存在但形状畸形的 MCP/Hook 不得被计入有效静态库存。"""
    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value["mcpServers"] = {"bad-server": {"command": 123}}
    value["hooks"] = {
        "SubagentStop": [
            {},
            {"hooks": [{}]},
            {"hooks": [{"type": "command"}]},
            {"hooks": [{"type": "command", "command": 123}]},
        ]
    }
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]

    assert isinstance(summary, dict)
    components = _components(summary)
    assert components["mcp"]["status"] == "invalid"
    assert components["mcp"]["count"] == 0
    assert components["mcp"]["capabilities"] == []
    assert any("command" in item for item in components["mcp"]["diagnostics"])
    assert components["hooks"]["status"] == "invalid"
    assert components["hooks"]["count"] == 0
    assert components["hooks"]["capabilities"] == []
    assert any("Hook" in item for item in components["hooks"]["diagnostics"])


def test_qwen_direct_skill_file_uses_frontmatter_validation(tmp_path: Path) -> None:
    """manifest 直指单个 SKILL.md 时仍必须校验 frontmatter 和正文。"""
    source = tmp_path / "qwen-direct-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# missing frontmatter\n", encoding="utf-8")
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "direct-skill", "skills": "SKILL.md"},
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]

    assert isinstance(summary, dict)
    component = _components(summary)["skills"]
    assert component["status"] == "invalid"
    assert component["count"] == 0
    assert any("front matter" in item for item in component["diagnostics"])


def test_qwen_nonempty_settings_and_out_of_scope_fields_are_unsupported(
    tmp_path: Path,
) -> None:
    """settings、channels、themes、workflows 等字段必须显式进入报告。"""
    source = _copy_fixture(tmp_path)
    value = _manifest(source)
    value.update(
        {
            "settings": [{"name": "example", "type": "string"}],
            "channels": ["alerts"],
            "themes": "themes",
            "workflows": "workflows",
            "futureField": True,
        }
    )
    (source / "devagent-extension.json").write_text(json.dumps(value), encoding="utf-8")

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    components = _components(summary)
    assert components["settings"]["status"] == "unsupported"
    for field in ("channels", "themes", "workflows", "unsupported"):
        assert components[field]["status"] == "unsupported"
    assert any("futureField" in item for item in summary["diagnostics"])


def test_za38_fixture_preserves_agent_modes_hook_matcher_and_mcp_placeholder() -> None:
    """fixture 锁定真实字段形状，运行时解析留给后续阶段。"""
    manifest = json.loads(
        (FIXTURE_ROOT / "devagent-extension.json").read_text(encoding="utf-8")
    )
    server = manifest["mcpServers"]["za38.03_code_index"]
    hook = manifest["hooks"]["SubagentStop"][0]

    assert server["args"] == ["${extensionPath}${/}mcp${/}context-server.mjs"]
    assert hook["matcher"] == "^za38-(frontend|backend|java)-executor$"
    assert hook["hooks"][0]["type"] == "command"
    assert "${CLAUDE_PLUGIN_ROOT}" in hook["hooks"][0]["command"]
    expected_agents = {
        "za38-backend-executor.md": "green",
        "za38-frontend-executor.md": "cyan",
        "za38-java-executor.md": "orange",
    }
    for filename, color in expected_agents.items():
        content = (FIXTURE_ROOT / "agents" / filename).read_text(encoding="utf-8")
        assert f"color: {color}" in content
        assert "approvalMode: auto-edit" in content


def test_qwen_agents_enter_canonical_catalog_after_trust_and_enable(
    tmp_path: Path,
) -> None:
    """信任后 Qwen Agent 进入 canonical catalog，静态 preview 只保留未接入组件。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)

    agent_component = _components(installed)["agents"]
    assert agent_component["status"] == "adapted"
    assert agent_component["effective"] is True
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    source_result = manager.agent_sources(manager.catalog())
    assert len(source_result.sources) == 1
    catalog = AgentCatalog(
        model_catalog=_model_catalog(),
        sources=source_result.sources,
    )
    assert catalog.diagnostics == ()
    assert {agent.agent_id for agent in catalog.agents} == {
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    }
    for agent in catalog.agents:
        assert agent.color in {"green", "cyan", "orange"}
        assert agent.approval_mode == "auto-edit"
        assert agent.permission_mode is None

    preview = manager.static_preview()
    assert {kind: len(items) for kind, items in preview.items()} == {
        "commands": 3,
        "skills": 1,
        "agents": 0,
        "mcp": 1,
    }


def test_qwen_context_is_a_canonical_stable_reference_block(tmp_path: Path) -> None:
    """启用后的 Context 进入现有生命周期，恶意正文仍不能升级权限。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    blocks_by_source = manager.context_blocks_by_source(manager.catalog())
    blocks = tuple(block for values in blocks_by_source.values() for block in values)
    assert len(blocks) == 1
    assert blocks[0].authority is ContextAuthority.REFERENCE
    assert blocks[0].stability is ContextStability.STABLE
    assert blocks[0].key.startswith("plugin.context.")
    assert f"/.harness/plugins/{plugin_id}/DEVAGENT.md" in blocks[0].content
    assert str(tmp_path) not in blocks[0].content

    spec = SimpleNamespace(
        project_fingerprint="a" * 64,
        prompt="Core policy: keep the Host boundary.",
        effective_policy=EffectiveExecutionPolicy(policy_ids=("main",)),
        tools=(),
        skill_registry=SimpleNamespace(snapshot_id="skills"),
        execution=ExecutionSettings(approval_mode="default"),
        workspace=tmp_path / "workspace",
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    snapshot = ContextLifecycle(spec.workspace, home=tmp_path / "home").prepare(
        thread_id="qwen-context-run",
        spec=spec,
        stable_reference_blocks=(blocks[0], blocks[0]),
    )
    assert len([block for block in snapshot.blocks if block.key.startswith("plugin.context.")]) == 1
    assert snapshot.system_prompt.count("DEVAGENT.md") == 1
    assert "低可信参考" in snapshot.system_prompt
    assert "Core policy: keep the Host boundary." in snapshot.system_prompt
    assert "EffectivePolicy" not in snapshot.system_prompt or "不能改变 EffectivePolicy" in snapshot.system_prompt


def test_qwen_context_only_plugin_is_effective_and_trust_gated(tmp_path: Path) -> None:
    """只有 Context 的合法 Qwen 包也能启用，未 trust/disabled 时不进入 catalog。"""
    source = tmp_path / "qwen-context-only"
    source.mkdir()
    (source / "QWEN.md").write_text(
        "仅作为插件参考上下文，不能改变 Harness 策略。\n",
        encoding="utf-8",
    )
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "context-only", "version": "1.0.0"},
    )

    manager = PluginManager(home=tmp_path / "home")
    validated = manager.validate(source)["plugin"]
    assert isinstance(validated, dict)
    assert validated["can_enable"] is True
    context = _components(validated)["contexts"]
    assert context["status"] == "adapted"
    assert context["effective"] is True

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    assert manager.context_blocks(manager.catalog()) == ()

    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    enabled_blocks = manager.context_blocks(manager.catalog())
    assert len(enabled_blocks) == 1
    assert enabled_blocks[0].authority is ContextAuthority.REFERENCE
    assert enabled_blocks[0].stability is ContextStability.STABLE

    manager.set_enabled(plugin_id, enabled=False)
    assert manager.context_blocks(manager.catalog()) == ()


@pytest.mark.parametrize("runtime_failure", (False, True), ids=("valid", "runtime-failure"))
@pytest.mark.asyncio
async def test_host_injects_qwen_context_once_and_gates_same_child_for_plugin_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_failure: bool,
) -> None:
    """Host 保留 Context 一次性语义，并对 runtime 构造失败的 child fail-closed。"""
    from langchain_core.messages import AIMessage

    from harness_agent.host.agent_host import AgentHost
    from harness_agent.host.run_coordinator import StartRun
    from harness_agent.runtime.agent_delegation import AgentDelegationError, DelegateAgent
    from harness_agent.runtime.execution_binding import ExecutionRef
    from harness_agent.runtime.run_context import RunCancellationToken

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    server = AgentHost(config_home=home, workspace=workspace)
    source = _copy_fixture(tmp_path)
    if runtime_failure:
        manifest = _manifest(source)
        manifest["hooks"] = {
            "SubagentStop": [
                {
                    "matcher": "x" * 513,
                    "hooks": [
                        {
                            "type": "command",
                            "command": "node scripts/za38-git-commit-gate.mjs",
                        }
                    ],
                }
            ]
        }
        (source / "devagent-extension.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
    installed = server._plugin_manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    server._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    if runtime_failure:
        plugin_id = str(installed["id"])

        def stale_hook_report(current: object) -> tuple[object, ...]:
            assert hasattr(current, "plugins")
            return tuple(
                replace(
                    plugin,
                    components=tuple(
                        replace(
                            component,
                            status="adapted",
                            effective=True,
                            capabilities=("process:hook",),
                            diagnostics=("stale report",),
                        )
                        if plugin.plugin_id == plugin_id and component.kind == "hooks"
                        else component
                        for component in plugin.components
                    ),
                )
                if plugin.plugin_id == plugin_id
                else plugin
                for plugin in current.plugins  # type: ignore[attr-defined]
            )

        server._plugin_manager.store.mutate_registry(stale_hook_report)  # type: ignore[arg-type]
    registry = server._build_skill_registry()
    from harness_agent.plugins.runtime import HookResult
    from harness_agent.runtime.interactions import InteractionResult

    hook_results = [
        HookResult(
            0,
            document={
                "decision": "block",
                "reason": "请完成离线提交检查。",
                "additionalContext": "只读测试反馈。",
            },
        ),
        HookResult(0, document={"decision": "allow"}),
    ]
    captured_interactions: list[tuple[object, object]] = []
    hook_calls = 0

    async def fake_hook_runner(
        _event: str,
        *,
        tool_name: str,
        payload: object,
        plugin_id: str | None = None,
    ) -> tuple[HookResult, ...]:
        """Host 集成使用离线 fake Hook，验证终态门禁不启动用户脚本。"""
        nonlocal hook_calls
        hook_calls += 1
        assert tool_name.startswith("za38-")
        assert isinstance(payload, dict)
        assert plugin_id
        return (hook_results.pop(0),)

    async def fake_child_interaction(
        run: object,
        interaction: object,
    ) -> InteractionResult:
        """用 fake owner channel 保留 child question 的 provenance。"""
        captured_interactions.append((run, interaction))
        return InteractionResult({"answers": {"question-1": ["submit"]}})

    assert server._plugin_runtime_manager is not None
    monkeypatch.setattr(server._plugin_runtime_manager.hooks, "run", fake_hook_runner)
    monkeypatch.setattr(
        server._run_coordinator,
        "request_child_interaction",
        fake_child_interaction,
    )

    captured_blocks: list[tuple[object, ...]] = []
    original_prepare = ContextLifecycle.prepare

    def capture_prepare(
        lifecycle: ContextLifecycle,
        *,
        thread_id: str,
        spec: Any,
        stable_reference_blocks: tuple[object, ...] = (),
        dynamic_blocks: tuple[object, ...] = (),
        now_ms: int | None = None,
    ):
        """捕获 Host 两条 canonical ContextLifecycle 调用链的输入。"""
        captured_blocks.append(tuple(stable_reference_blocks))
        return original_prepare(
            lifecycle,
            thread_id=thread_id,
            spec=spec,
            stable_reference_blocks=stable_reference_blocks,
            dynamic_blocks=dynamic_blocks,
            now_ms=now_ms,
        )

    monkeypatch.setattr(ContextLifecycle, "prepare", capture_prepare)

    class Store:
        """提供离线 Host 测试所需的最小持久化接口。"""

        project_fingerprint = "a" * 64

        async def load_thread_activity_ms(self, _thread_id: str) -> int | None:
            return None

        async def persist_agent_engine_profile(self, _profile: object) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeGraph:
        """用 fake graph 验证 Managed child 收到的 RunContext。"""

        def __init__(self) -> None:
            self.calls = 0
            self.inputs: list[object] = []

        async def astream(self, stream_input: object, *, context: Any, **_kwargs: object):
            self.calls += 1
            self.inputs.append(stream_input)
            child_context["value"] = context
            yield ("messages", (AIMessage(content="OFFLINE_OK"), {}))

    class FakeRunLease:
        async def release(self) -> None:
            return None

    class FakeLease:
        def __init__(self, engine: Any) -> None:
            self.engine = engine

        async def run(self) -> FakeRunLease:
            return FakeRunLease()

        async def release(self) -> None:
            return None

    class FakePool:
        def __init__(self) -> None:
            self.engine = SimpleNamespace(graph=FakeGraph())

        async def acquire(self, _profile: object) -> FakeLease:
            return FakeLease(self.engine)

        async def finalize_draining(self, _profile_key: str) -> None:
            return None

        async def aclose(self) -> tuple[object, ...]:
            return ()

    child_context: dict[str, Any] = {}
    server._thread_persistence = Store()  # type: ignore[assignment]
    server._config = SimpleNamespace(
        model_catalog=_model_catalog(),
        execution=ExecutionSettings(),
        agent_engine_pool=SimpleNamespace(
            max_profiles=2,
            idle_ttl_seconds=600,
            close_timeout_seconds=15,
        ),
    )
    server._load_config = lambda: None  # type: ignore[method-assign]
    server._agent_engine_pool = FakePool()

    main_policy = EffectiveExecutionPolicy(policy_ids=("main",))
    main_spec = SimpleNamespace(
        project_fingerprint="b" * 64,
        prompt="Main policy boundary.",
        effective_policy=main_policy,
        tools=(),
        skill_registry=registry,
        execution=ExecutionSettings(),
        workspace=workspace,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
        runtime_profile=SimpleNamespace(profile_key="main-profile"),
    )
    resolved_binding = SimpleNamespace(bind_run=lambda **_kwargs: object())

    async def resolve_binding(*_args: object, **_kwargs: object) -> object:
        return resolved_binding

    async def resolve_spec(*_args: object, **_kwargs: object) -> object:
        return main_spec

    async def return_registry() -> SkillRegistry:
        return registry

    server._resolve_execution_binding = resolve_binding  # type: ignore[method-assign]
    server._resolve_agent_engine_spec = resolve_spec  # type: ignore[method-assign]
    server._refresh_skill_catalog_locked = return_registry  # type: ignore[method-assign]

    try:
        main_preparation = await server._prepare_run(
            StartRun(
                mode="build",
                thread_id="host-context-main",
                run_id="host-context-main-run",
                message="只读检查",
            ),
            server._thread_persistence,
        )
        assert main_preparation.context_snapshot is not None
        assert len(captured_blocks) == 1
        assert len(captured_blocks[0]) == 1
        assert main_preparation.context_snapshot.system_prompt.count("DEVAGENT.md") == 1
        if main_preparation.snapshot_reservation is not None:
            await main_preparation.snapshot_reservation.release()

        parent_policy = EffectiveExecutionPolicy(
            policy_ids=("parent",),
            delegation=DelegationPolicy(
                enabled=True,
                allowed_agents=None,
                max_depth=1,
                max_parallelism=1,
            ),
        )
        parent_spec = SimpleNamespace(
            agent_id="main",
            effective_policy=parent_policy,
            model_profile_id="fast",
        )
        targets = await server._plugin_delegation_targets(parent_spec)
        assert targets
        child_command = DelegateAgent(
            parent_ref=ExecutionRef.root("host-context-child", "child-run"),
            target_agent_id=targets[0].agent_id,
            task="只读检查",
            idempotency_key="host-context-child-once",
            delegation_policy=parent_policy.delegation,
            cancellation_token=RunCancellationToken(),
        )
        if runtime_failure:
            with pytest.raises(AgentDelegationError) as runtime_error:
                await targets[0].runner(child_command)
            assert runtime_error.value.code == "PLUGIN_HOOK_MATCHER_INVALID"
            assert hook_calls == 0
            assert captured_interactions == []
            assert server._agent_engine_pool.engine.graph.calls == 1
        else:
            result = await targets[0].runner(child_command)
            assert result == {"final": "OFFLINE_OK"}
        assert len(captured_blocks) == 2
        assert len(captured_blocks[1]) == 1
        assert captured_blocks[0][0].key == captured_blocks[1][0].key
        assert child_context["value"].context_snapshot.system_prompt.count("DEVAGENT.md") == 1
        if runtime_failure:
            assert len(captured_interactions) == 0
        else:
            assert len(captured_interactions) == 1
            interaction = captured_interactions[0][1]
            assert interaction.execution_id.startswith("child-")
            assert interaction.parent_execution_id == "root-child-run"
            assert interaction.agent_id == targets[0].agent_id
            assert interaction.serial_context["kind"] == "subagent_stop"
            assert interaction.serial_context["checkpoint_namespace"].endswith(
                ":host-context-child:child-run:"
                + interaction.execution_id
            )
            assert server._agent_engine_pool.engine.graph.calls == 2
            assert "用户已选择" in repr(server._agent_engine_pool.engine.graph.inputs[1])
            assert hook_results == []
    finally:
        await server.close()


def test_qwen_context_bad_utf8_fails_closed_after_enable(tmp_path: Path) -> None:
    """已安装 Context 的坏 UTF-8 不能降级为可注入的替换文本。"""
    source = _copy_fixture(tmp_path)
    (source / "DEVAGENT.md").write_bytes(b"bad utf8: \xff\n")
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    with pytest.raises(PluginError) as rejected:
        manager.context_blocks(manager.catalog())
    assert rejected.value.code == "PLUGIN_RESOURCE_ENCODING_INVALID"


@pytest.mark.parametrize(
    ("permission_mode", "expected"),
    (
        ("default", "default"),
        ("plan", "plan"),
        ("acceptEdits", "auto-edit"),
        ("auto", "auto-edit"),
        ("dontAsk", "default"),
    ),
)
def test_qwen_permission_mode_uses_source_enum_mapping(
    tmp_path: Path,
    permission_mode: str,
    expected: str,
) -> None:
    """Qwen permissionMode 使用独立源枚举，并只映射为 canonical 权限请求。"""
    agent_path = tmp_path / f"{permission_mode}.md"
    agent_path.write_text(
        "---\n"
        "name: permission-agent\n"
        "description: Offline permission mapping\n"
        f"permissionMode: {permission_mode}\n"
        "---\n\n"
        "只读任务。\n",
        encoding="utf-8",
    )
    catalog = AgentCatalog(
        model_catalog=_model_catalog(),
        sources=(
            SimpleNamespace(
                plugin_id=f"qwen-{permission_mode.lower()}",
                root=tmp_path,
                format="qwen-code",
                agent_files=(agent_path,),
                policy_files=(),
                package_digest=None,
            ),
        ),
    )

    assert catalog.diagnostics == ()
    agent = catalog.require_agent("permission-agent")
    assert agent.permission_mode == expected
    assert catalog.require_policy(agent.execution_policy_id).approval_mode == expected


@pytest.mark.parametrize(
    "field,value",
    (
        ("permissionMode", "bypassPermissions"),
        ("permissionMode", "unknown"),
        ("permissionMode", 123),
        ("approvalMode", "acceptEdits"),
        ("approvalMode", {"mode": "auto-edit"}),
    ),
)
def test_qwen_permission_and_approval_modes_fail_closed_before_enable(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """未知、bypass 或类型错误模式在 Plugin validate 阶段稳定失败。"""
    source = tmp_path / "qwen-invalid-mode"
    (source / "agents").mkdir(parents=True)
    (source / "agents" / "invalid.md").write_text(
        "---\n"
        "name: invalid-mode\n"
        "description: Invalid mode\n"
        f"{field}: {json.dumps(value)}\n"
        "---\n\n"
        "正文。\n",
        encoding="utf-8",
    )
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "invalid-mode", "agents": ["agents"]},
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    assert summary["can_enable"] is False
    component = _components(summary)["agents"]
    assert component["status"] == "invalid"
    assert component["effective"] is False
    assert any(item.startswith("PLUGIN_COMPONENT_INVALID:") for item in component["diagnostics"])


def test_qwen_approval_and_permission_mode_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    """两个模式字段同时存在且请求不一致时不猜测更宽松的语义。"""
    source = tmp_path / "qwen-conflicting-mode"
    (source / "agents").mkdir(parents=True)
    (source / "agents" / "conflict.md").write_text(
        "---\n"
        "name: conflict-mode\n"
        "description: Conflicting mode\n"
        "approvalMode: plan\n"
        "permissionMode: auto\n"
        "---\n\n"
        "正文。\n",
        encoding="utf-8",
    )
    _write_manifest(
        source,
        "qwen-extension.json",
        {"name": "conflicting-mode", "agents": ["agents"]},
    )

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    assert summary["can_enable"] is False
    component = _components(summary)["agents"]
    assert component["status"] == "invalid"
    assert component["effective"] is False
    assert any("approvalMode and permissionMode conflict" in item for item in component["diagnostics"])


@pytest.mark.parametrize("field", ("approvalMode", "permissionMode"))
def test_qwen_agent_unknown_permission_modes_fail_before_enable(
    tmp_path: Path,
    field: str,
) -> None:
    """未知或 bypass 类权限模式在安装报告阶段失败关闭。"""
    source = _copy_fixture(tmp_path)
    agent = source / "agents" / "za38-backend-executor.md"
    content = agent.read_text(encoding="utf-8")
    content = content.replace("approvalMode: auto-edit", f"{field}: bypassPermissions")
    agent.write_text(content, encoding="utf-8")

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["agents"]
    assert component["status"] == "invalid"
    assert component["effective"] is False
    assert any("approval" in item.lower() or "permission" in item.lower() for item in component["diagnostics"])


def test_qwen_auto_edit_is_only_a_request_under_parent_and_host_policy(
    tmp_path: Path,
) -> None:
    """ZA38 auto-edit 只保留为请求，父/Host 收紧后不能获得写、Shell 或网络。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    sources = manager.agent_sources(manager.catalog()).sources
    catalog = AgentCatalog(model_catalog=_model_catalog(), sources=sources)
    parent = ExecutionPolicyDefinition(
        policy_id="restricted-parent",
        source="test",
        tools=StringRule(allow=("read_file", "glob", "grep", "write_file", "execute", "task")),
        filesystem_read=("**/*",),
        filesystem_write=("**/*",),
        shell=ShellPolicy(enabled=True),
        network=NetworkPolicy(enabled=True),
        delegation=DelegationPolicy(enabled=True),
        approval_mode="auto-edit",
    )

    for agent in catalog.agents:
        effective = catalog.effective_policy(agent.agent_id, envelope=parent)
        assert effective.approval_mode == "auto-edit"
        assert effective.filesystem_write == ()
        assert effective.shell is not None and effective.shell.enabled is False
        assert effective.network is not None and effective.network.enabled is False
        assert effective.delegation is not None and effective.delegation.enabled is False


def test_qwen_executors_are_selectable_by_an_offline_resolved_spec(
    tmp_path: Path,
) -> None:
    """fake runner 只选择 canonical spec 并完成读取能力检查，不调用模型或 MCP。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    catalog = AgentCatalog(
        model_catalog=_model_catalog(),
        sources=manager.agent_sources(manager.catalog()).sources,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = EffectiveExecutionPolicy(
        policy_ids=("parent",),
        tools=StringRule(allow=("read_file", "glob", "grep")),
        filesystem_read=("**/*",),
        filesystem_write=(),
        shell=ShellPolicy(enabled=False),
        network=NetworkPolicy(enabled=False),
        isolation="local",
        approval_mode="default",
        delegation=DelegationPolicy(enabled=False),
    )
    registry = SkillRegistry(workspace, home=tmp_path / "skill-home")
    selected: list[str] = []
    for definition in catalog.agents:
        resolved = resolve_plugin_agent_spec(
            definition=definition,
            catalog=catalog,
            parent_policy=parent,
            model_catalog=_model_catalog(),
            project_fingerprint="b" * 64,
            workspace=workspace,
            execution=ExecutionSettings(approval_mode="default"),
            skill_registry=registry,
            mcp_snapshot=build_mcp_snapshot([], revision="offline"),
            mcp_tools=(),
            interactive=False,
            inherited_model_profile_id="fast",
        )
        assert resolved.capability_view.tool_names == (
            "glob",
            "grep",
            "read_file",
        )
        assert resolved.effective_policy.filesystem_write == ()
        selected.append(resolved.agent_id)

    assert selected == [
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    ]
