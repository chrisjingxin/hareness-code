"""Qwen/DevAgent Extension 分阶段 Adapter 与 canonical 接入的离线回归测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.config.config import ExecutionSettings, ModelCatalog, ModelProfile, ModelSettings
from harness_agent.extensions.mcp import build_mcp_snapshot
from harness_agent.extensions.plugin_skills import (
    SkillError as PluginSkillError,
    SkillRegistry as PluginSkillRegistry,
)
from harness_agent.extensions.skills import SkillRegistry
from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.model import PluginError, capability_fingerprint
from harness_agent.protocol.generated import EventEnvelope
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
REAL_ZA38_EXTENSION = Path("/Users/beichen/Desktop/大模型/za38-cli-extension")


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
    assert components["commands"]["status"] == "adapted"
    assert components["commands"]["effective"] is True
    assert components["skills"]["status"] == "adapted"
    assert components["skills"]["effective"] is True
    assert components["mcp"]["status"] == "adapted"
    assert components["mcp"]["effective"] is True
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


async def test_qwen_unsupported_lsp_and_monitor_reports_cannot_enter_runtime(
    tmp_path: Path,
) -> None:
    """有效 Agent/Context/Hook 不能让 Qwen unsupported LSP/Monitor 借道运行。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    spawn_counter = tmp_path / "fake-spawn-count.txt"
    spawn_counter.write_text("0", encoding="utf-8")
    fake_spawn = source / "scripts" / "fake-spawn.py"
    fake_spawn.parent.mkdir(parents=True, exist_ok=True)
    fake_spawn.write_text(
        "from pathlib import Path\n"
        f"counter = Path({str(spawn_counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1), encoding='utf-8')\n",
        encoding="utf-8",
    )
    manifest["lspServers"] = {
        "malicious": {
            "transport": "socket",
            "command": sys.executable,
            "args": ["${extensionPath}/scripts/fake-spawn.py"],
            "extensionToLanguage": {".evil": "evil"},
        }
    }
    manifest["monitors"] = "monitors/monitors.json"
    (source / "monitors").mkdir()
    (source / "monitors" / "monitors.json").write_text(
        json.dumps(
            [
                {
                    "name": "malicious",
                    "command": (
                        f"{json.dumps(sys.executable)} "
                        f"{json.dumps('${extensionPath}/scripts/fake-spawn.py')}"
                    ),
                }
            ],
        ),
        encoding="utf-8",
    )
    _write_manifest(source, "devagent-extension.json", manifest)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    catalog = manager.catalog()
    qwen = catalog.plugins[0]
    assert any(
        component.kind == "agents" and component.effective
        for component in qwen.components
    )
    assert any(
        component.kind == "contexts" and component.effective
        for component in qwen.components
    )
    assert any(
        component.kind == "hooks" and component.effective
        for component in qwen.components
    )
    assert qwen.components and {
        component.kind
        for component in qwen.components
        if component.kind in {"lsp", "monitors"}
    } == {"lsp", "monitors"}
    assert all(
        (
            component.kind == "monitors"
            and component.status == "unsupported"
            and not component.effective
        )
        or (
            component.kind == "lsp"
            and component.status == "unsupported"
            and not component.effective
        )
        for component in qwen.components
        if component.kind in {"lsp", "monitors"}
    )

    runtime_catalog = manager.runtime_catalog(
        catalog,
        workspace=tmp_path / "workspace",
    )

    assert len(runtime_catalog.hooks) == 1
    assert runtime_catalog.lsp_servers == ()
    assert runtime_catalog.monitors == ()
    assert any(
        "PLUGIN_RUNTIME_COMPONENT_BLOCKED" in diagnostic
        and "kind=lsp" in diagnostic
        for diagnostic in runtime_catalog.diagnostics
    )
    assert any(
        "PLUGIN_RUNTIME_COMPONENT_BLOCKED" in diagnostic
        and "kind=monitors" in diagnostic
        for diagnostic in runtime_catalog.diagnostics
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "malicious.evil").write_text("x", encoding="utf-8")
    from harness_agent.plugins.runtime import PluginRuntimeManager

    runtime = PluginRuntimeManager(runtime_catalog)
    try:
        await runtime.start()
        lsp_result = await runtime.lsp.query(
            "definition",
            "malicious.evil",
            1,
            1,
            str(workspace),
        )
        assert lsp_result["results"] == []
        await asyncio.sleep(0.05)
    finally:
        await runtime.aclose()
    assert spawn_counter.read_text(encoding="utf-8") == "0"


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
    if "PostToolUse" in hooks:
        assert component["status"] == "adapted"
        assert component["effective"] is True
    elif any(
        isinstance(group, dict)
        and any(
            isinstance(handler, dict) and handler.get("command") == 123
            for handler in group.get("hooks", [])
        )
        for group in hooks["SubagentStop"]
    ):
        assert component["status"] == "adapted"
        assert component["effective"] is True
    else:
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

    if component["effective"] is True:
        assert len(runtime_catalog.hooks) == 1
        if "PostToolUse" in hooks:
            assert runtime_catalog.hooks[0].event == "PostToolUse"
        else:
            assert runtime_catalog.hook_failures
    else:
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
        ("timeout", 600_001, "timeout"),
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

    def reauthorized_hook_report(current: object) -> tuple[object, ...]:
        assert hasattr(current, "plugins")
        updated: list[object] = []
        for plugin in current.plugins:  # type: ignore[attr-defined]
            if plugin.plugin_id != plugin_id:
                updated.append(plugin)
                continue
            components = tuple(
                replace(
                    item,
                    status="adapted",
                    effective=True,
                    capabilities=("process:hook",),
                    diagnostics=("re-authorized report",),
                )
                if item.kind == "hooks"
                else item
                for item in plugin.components
            )
            fingerprint = capability_fingerprint(components)
            updated.append(
                replace(
                    plugin,
                    components=components,
                    capability_fingerprint=fingerprint,
                    trusted_capability_fingerprint=fingerprint,
                )
            )
        return tuple(updated)

    manager.store.mutate_registry(reauthorized_hook_report)  # type: ignore[arg-type]
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


@pytest.mark.parametrize(
    ("timeout", "expected_status", "expected_seconds"),
    (
        (10_000, "adapted", 10.0),
        (600_000, "adapted", 600.0),
        (None, "adapted", 60.0),
        (0, "invalid", None),
        (-1, "invalid", None),
        (True, "invalid", None),
        ("10000", "invalid", None),
        (600_001, "invalid", None),
    ),
)
def test_qwen_hook_timeout_is_milliseconds_at_adapter_and_runtime_boundary(
    tmp_path: Path,
    timeout: object,
    expected_status: str,
    expected_seconds: float | None,
) -> None:
    """Qwen command Hook 的 timeout 按毫秒适配，canonical HookDefinition 按秒运行。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    handler: dict[str, object] = {
        "type": "command",
        "command": "node scripts/za38-git-commit-gate.mjs",
    }
    if timeout is not None:
        handler["timeout"] = timeout
    manifest["hooks"] = {
        "SubagentStop": [
            {"matcher": "^za38-frontend-executor$", "hooks": [handler]},
        ]
    }
    (source / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["hooks"]
    assert component["status"] == expected_status
    assert component["effective"] is (expected_status == "adapted")

    if expected_seconds is None:
        return

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    runtime_catalog = manager.runtime_catalog(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )
    assert len(runtime_catalog.hooks) == 1
    assert runtime_catalog.hooks[0].timeout_seconds == expected_seconds


@pytest.mark.parametrize(
    ("qwen", "timeout", "valid"),
    (
        (True, 10_000, True),
        (False, 10_000, False),
        (False, 600, True),
        (False, 601, False),
    ),
)
def test_command_hook_timeout_validation_remains_format_specific(
    qwen: bool,
    timeout: object,
    valid: bool,
) -> None:
    """Qwen 使用毫秒边界，Claude/portable 继续使用既有秒边界。"""
    from harness_agent.plugins.common import validate_command_hook_handler

    error = validate_command_hook_handler(
        {"type": "command", "command": "node gate.mjs", "timeout": timeout},
        event="SubagentStop",
        qwen=qwen,
    )
    assert (error is None) is valid


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
        "COMPONENT_FINGERPRINT_DRIFT" in diagnostic
        for diagnostic in runtime_catalog.diagnostics
    ), runtime_catalog.diagnostics


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
        "${extensionPath}/scripts/za38-index.mjs",
        "${extensionPath}/scripts/za38-git-commit-gate.mjs",
        "${extensionPath}${/}mcp${/}context-server.mjs",
        "${extensionPath}${pathSeparator}scripts${pathSeparator}za38-index.mjs",
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
        ("--file=${extensionPath}/../outside.mjs", "PLUGIN_MCP_PATH_INVALID"),
        ("--file=prefix/${extensionPath}/scripts/za38-index.mjs", "PLUGIN_MCP_PATH_INVALID"),
        ("--file=${UNKNOWN_ROOT}/scripts/za38-index.mjs", "PLUGIN_MCP_PLACEHOLDER_INVALID"),
        ("--file=<extensionPath>/scripts/missing.mjs", "PLUGIN_MCP_PLACEHOLDER_INVALID"),
        ("--file=/tmp/outside.mjs", "PLUGIN_MCP_PATH_INVALID"),
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

    summary = PluginManager(home=tmp_path / "home").install(source)["plugin"]
    assert isinstance(summary, dict)
    component = _components(summary)["mcp"]
    assert component["effective"] is False
    assert component["status"] == "invalid"
    assert any(error_code in diagnostic for diagnostic in component["diagnostics"])


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


def test_qwen_commands_and_skills_use_canonical_dialects_and_dedupe_preview(
    tmp_path: Path,
) -> None:
    """trusted Qwen Commands/Skills 进入 canonical registry，effective 条目从 preview 去重。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    catalog = manager.catalog()
    result = manager.skill_sources(catalog)
    assert result.diagnostics == ()
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
        plugin_diagnostics=result.diagnostics,
    )

    commands = registry.agent_commands()
    assert {item["name"] for item in commands} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }
    assert all(item["name"] != "plugin:local:ZA38.03_CLI_EXTENSION:za38-sdd" for item in commands)
    command_records = [
        record for record in registry.records if record.kind == "command"
    ]
    assert {record.dialect for record in command_records} == {"qwen-command"}
    assert all(record.name in {"za38-index", "za38-init", "za38-sdd"} for record in command_records)
    skill = registry.resolve("za38-framework")
    assert skill.kind == "skill"
    assert skill.dialect == "qwen-skill"
    assert registry.read_resource(skill.skill_id, "references/root-guide.md").startswith(
        "ZA38 offline root reference"
    )

    preview = manager.static_preview()
    assert preview["commands"] == []
    assert preview["skills"] == []


def test_qwen_skill_resource_closure_excludes_origin_material_without_diagnostics(
    tmp_path: Path,
) -> None:
    """Skill 只捕获顶层运行时 references，开发原始素材不可见且不产生诊断。"""
    source = _copy_fixture(tmp_path)
    origin = source / "references" / "origin"
    origin.mkdir(parents=True)
    (origin / "sentinel.png").write_bytes(b"ORIGIN_SENTINEL")
    (origin / "oversized.png").write_bytes(b"ORIGIN_OVERSIZED_SENTINEL")

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    installed_record = manager.store.read_registry().plugins[0]
    package = manager.store.package_path(installed_record)
    # Store 保留净化后的完整安装源；runtime snapshot 再按组件声明收紧闭包。
    assert (package / "references/origin/sentinel.png").read_bytes() == b"ORIGIN_SENTINEL"
    assert (package / "references/origin/oversized.png").read_bytes() == (
        b"ORIGIN_OVERSIZED_SENTINEL"
    )

    result = manager.skill_sources(manager.catalog())
    assert result.diagnostics == ()
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
        plugin_diagnostics=result.diagnostics,
    )
    skill = registry.resolve("za38-framework")
    assert registry.read_resource(skill.skill_id, "references/root-guide.md").startswith(
        "ZA38 offline root reference"
    )
    with pytest.raises(PluginSkillError, match="not captured"):
        registry.read_resource(skill.skill_id, "references/origin/sentinel.png")

    snapshot = manager.resource_snapshot(str(installed["id"]))
    serialized = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    assert "ORIGIN_SENTINEL" not in serialized
    assert "ORIGIN_OVERSIZED_SENTINEL" not in serialized
    assert all("references/origin/" not in asset.source for asset in snapshot.resources)


@pytest.mark.skipif(
    not REAL_ZA38_EXTENSION.is_dir(),
    reason="本机没有只读 ZA38 extension fixture",
)
def test_real_za38_extension_install_ignores_checkout_metadata_and_exposes_four_items(
    tmp_path: Path,
) -> None:
    """真实 checkout 只读安装后提供三 Command 和一个 Skill，不触碰本地秘密。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(REAL_ZA38_EXTENSION)["plugin"]
    assert isinstance(installed, dict)
    installed_components = _components(installed)
    assert installed_components["hooks"]["status"] == "adapted"
    assert installed_components["hooks"]["effective"] is True
    assert installed.get("compatibility") == "recognized"
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    catalog = manager.catalog()
    result = manager.skill_sources(catalog)
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
        plugin_diagnostics=result.diagnostics,
    )
    assert result.diagnostics == ()
    assert {item["name"] for item in registry.agent_commands()} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }
    assert {
        record.name
        for record in registry.records
        if record.kind == "skill" and record.source != "builtin"
    } == {
        "za38-framework"
    }
    assert manager.static_preview()["commands"] == []
    assert manager.static_preview()["skills"] == []

    record = manager.store.read_registry().plugins[0]
    package = manager.store.package_path(record)
    assert not any(
        part in {".git", ".env.dev", ".env.prd", ".env.st", ".env.uat", ".npmrc"}
        for path in package.rglob("*")
        for part in path.relative_to(package).parts
    )
    public_outputs = json.dumps(
        {
            "list": manager.list(),
            "snapshot": manager.resource_snapshot(record.plugin_id).to_dict(),
            "diagnostics": result.diagnostics,
            "catalog": catalog.to_dict(),
        },
        ensure_ascii=False,
    )
    assert str(REAL_ZA38_EXTENSION) not in public_outputs
    assert ".env.dev" not in public_outputs
    assert ".env.prd" not in public_outputs
    assert ".env.st" not in public_outputs
    assert ".env.uat" not in public_outputs
    assert ".npmrc" not in public_outputs
    assert all(
        "references/origin/" not in asset.source
        for asset in manager.resource_snapshot(record.plugin_id).resources
    )
    origin_store_entries = tuple(
        path
        for path in package.rglob("*")
        if "references/origin" in path.relative_to(package).as_posix()
    )
    assert origin_store_entries
    # PluginStore 故意把 package 设为只读；恢复临时 home 的权限只为让
    # pytest 在测试结束时清理 fixture，不改变被测 store 的运行语义。
    for path in sorted(
        (tmp_path / "home").rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(0o700 if path.is_dir() else 0o600)
    (tmp_path / "home").chmod(0o700)


async def test_qwen_command_host_run_keeps_raw_invocation_and_projects_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host 离线 Run 只把 snapshot 展开正文交给 fake Agent，并保留原调用。"""
    from harness_agent.host.agent_host import AgentHost

    class RecordingAgent:
        """不访问模型或网络，只记录 ManagedAgentExecutor 的一次输入。"""

        def __init__(self) -> None:
            self.inputs: list[object] = []

        async def astream(self, stream_input: object, **_kwargs: Any):
            self.inputs.append(stream_input)
            yield (
                "messages",
                (SimpleNamespace(content="done", usage_metadata=None, tool_call_chunks=[]), {}),
            )

    home = tmp_path / "home"
    config_path = home / ".harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
model = "offline-model"
base_url = "https://offline.invalid/v1"
api_key_env = "HARNESS_PHASE1_FAKE_KEY"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_PHASE1_FAKE_KEY", "offline-test-key")
    workspace = tmp_path / "workspace"
    fake = RecordingAgent()
    server = AgentHost(agent=fake, config_home=home, workspace=workspace)
    server._thread_persistence_enabled = lambda: True  # type: ignore[method-assign]

    async def offline_mcp() -> None:
        """测试不得连接 fixture MCP 或创建外部进程。"""

    server._ensure_mcp_connected = offline_mcp  # type: ignore[method-assign]
    server._connect_mcp_servers = offline_mcp  # type: ignore[method-assign]
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    installed = server._plugin_manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    server._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    try:
        initialize_params = {
                "protocol": {"major": 3, "min_minor": 0, "max_minor": 7},
            "client": {"name": "test", "version": "0.1.0", "kind": "test"},
            "capabilities": {
                "requests": ["run.multithread", "skills.read", "threads.read"],
                "handles": [],
            },
        }
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": initialize_params,
                "id": "phase1-init",
            }
        )
        initialized = next(frame["result"] for frame in frames if frame.get("id") == "phase1-init")
        command = next(
            item for item in initialized["agent_commands"] if item["name"] == "za38-sdd"
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "commands.bind",
                "params": {
                    "snapshot_id": initialized["skills_snapshot"]["id"],
                    "bindings": [
                        {"id": item["id"], "name": item["name"]}
                        for item in initialized["agent_commands"]
                    ],
                },
                "id": "phase1-command-bind",
            }
        )
        binding = next(frame for frame in frames if frame.get("id") == "phase1-command-bind")
        assert binding["result"] == {
            "snapshot_id": initialized["skills_snapshot"]["id"],
            "accepted": True,
        }
        raw_invocation = "/ZA38-SDD   创建登录功能  "
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "run.start",
                "params": {
                    "mode": "build",
                    "message": raw_invocation,
                    "thread_id": "phase1-thread",
                    "run_id": "phase1-run",
                    "requested_skill": {
                        "id": command["requested_skill_id"],
                        "args": "创建登录功能",
                        "raw_invocation": raw_invocation,
                        "command_name": "za38-sdd",
                    },
                },
                "id": "phase1-run-start",
            }
        )
        for _ in range(200):
            if any(
                frame.get("params", {}).get("type") == "run.completed"
                for frame in frames
            ):
                break
            await asyncio.sleep(0.01)
        assert any(
            frame.get("params", {}).get("type") == "run.completed"
            for frame in frames
        ), frames
        # 这里直接校验 Host 真实 BuildRunAdapter 发出的每一帧，而不是手工
        # 构造一个相似 payload；schema 漏声明 provenance 时应在此处先失败。
        for frame in frames:
            if frame.get("method") == "event":
                EventEnvelope.model_validate(frame["params"])
        assert len(fake.inputs) == 1
        assert fake.inputs[0]["messages"][0].content == (
            "# /za38-sdd\n\nSDD fixture.\n\n创建登录功能"
        )
        loaded = next(
            frame["params"]
            for frame in frames
            if frame.get("method") == "event"
            and frame["params"]["type"] == "skill.loaded"
        )
        started = next(
            frame["params"]
            for frame in frames
            if frame.get("method") == "event"
            and frame["params"]["type"] == "run.started"
        )
        expected_provenance = {
            "plugin_id": str(installed["id"]),
            "package_digest": installed["package_digest"],
            "command_id": command["id"],
            "snapshot_id": initialized["skills_snapshot"]["id"],
        }
        assert started["payload"]["command_provenance"] == expected_provenance
        assert loaded["payload"]["provenance"] == expected_provenance
        assert set(started["payload"]["command_provenance"]) == {
            "plugin_id",
            "package_digest",
            "command_id",
            "snapshot_id",
        }
        assert set(loaded["payload"]["provenance"]) == {
            "plugin_id",
            "package_digest",
            "command_id",
            "snapshot_id",
        }
        serialized_events = json.dumps(
            [frame["params"] for frame in frames if frame.get("method") == "event"],
            ensure_ascii=False,
        )
        assert str(home) not in serialized_events
        assert str(workspace) not in serialized_events
        assert "SDD fixture." not in json.dumps(expected_provenance)
        assert loaded["payload"]["provenance"]["package_digest"] == installed["package_digest"]
        assert loaded["payload"]["provenance"]["command_id"] == command["id"]
        records = await server._thread_persistence.load_transcript("phase1-thread")
        assert records[0].kind == "user"
        assert records[0].payload["content"] == raw_invocation

        fake.inputs.clear()
        frames.clear()
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "run.start",
                "params": {
                    "mode": "build",
                    "message": "/help dangerous-goal",
                    "thread_id": "phase1-mismatch-thread",
                    "run_id": "phase1-mismatch-run",
                    "requested_skill": {
                        "id": command["requested_skill_id"],
                        "args": "dangerous-goal",
                        "raw_invocation": "/help dangerous-goal",
                        "command_name": "help",
                    },
                },
                "id": "phase1-mismatch-start",
            }
        )
        error = next(frame for frame in frames if frame.get("id") == "phase1-mismatch-start")
        assert error["error"]["message"] == "COMMAND_INVOCATION_IDENTITY_MISMATCH"
        assert error["error"]["data"] == {
            "code": "COMMAND_INVOCATION_IDENTITY_MISMATCH",
            "retryable": False,
        }
        assert len(fake.inputs) == 0
        assert await server._thread_persistence.load_transcript("phase1-mismatch-thread") == ()
    finally:
        await server.close()


async def test_old_minor_rejects_plugin_command_before_emitting_v37_provenance(
    tmp_path: Path,
) -> None:
    """v3.6 连接不能收到 v3.7 Plugin Command provenance 事件。"""
    from harness_agent.host.agent_host import AgentHost

    class FakeAgent:
        async def astream(self, _stream_input: object, **_kwargs: Any):
            yield ("messages", (SimpleNamespace(content="unused"), {}))

    server = AgentHost(
        agent=FakeAgent(),
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    installed = server._plugin_manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    server._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    try:
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocol": {"major": 3, "min_minor": 0, "max_minor": 6},
                    "client": {"name": "old-cli", "version": "0.1.0", "kind": "test"},
                    "capabilities": {"requests": ["run.multithread", "skills.read"], "handles": []},
                },
                "id": "old-init",
            }
        )
        initialized = next(frame["result"] for frame in frames if frame.get("id") == "old-init")
        command = next(item for item in initialized["agent_commands"] if item["name"] == "za38-sdd")
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "commands.bind",
                "params": {
                    "snapshot_id": initialized["skills_snapshot"]["id"],
                    "bindings": [
                        {"id": item["id"], "name": item["name"]}
                        for item in initialized["agent_commands"]
                    ],
                },
                "id": "old-bind",
            }
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "run.start",
                "params": {
                    "mode": "build",
                    "message": "/za38-sdd",
                    "thread_id": "old-thread",
                    "run_id": "old-run",
                    "requested_skill": {
                        "id": command["requested_skill_id"],
                        "args": "",
                        "raw_invocation": "/za38-sdd",
                        "command_name": "za38-sdd",
                    },
                },
                "id": "old-run-start",
            }
        )
        response = next(frame for frame in frames if frame.get("id") == "old-run-start")
        assert response["error"]["message"] == "PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED"
        assert response["error"]["data"] == {
            "code": "PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED",
            "retryable": False,
        }
        assert not any(frame.get("method") == "event" for frame in frames)
    finally:
        await server.close()


def test_host_accepts_cli_resolved_command_name_without_builtin_table() -> None:
    """Host 只验证 CLI 的单一解析结果，不复制未来 builtin/alias 规则。"""
    from harness_agent.host.agent_host import _validate_command_invocation
    from harness_agent.host.run_coordinator import RequestedSkill, StartRun

    command_id = "plugin/local/future/command/preview"

    class SnapshotRegistry:
        """只提供 Host snapshot 的 stable command record，不提供 UI 命令表。"""

        def resolve(self, skill_id: str) -> SimpleNamespace:
            assert skill_id == command_id
            return SimpleNamespace(
                skill_id=skill_id,
                kind="command",
                name="preview",
                source="plugin:local/future",
                dialect="qwen-command",
            )

    raw = "/future.preview   args  "
    requested = RequestedSkill(
        command_id,
        "args",
        raw,
        "future.preview",
    )
    command = StartRun(
        thread_id="future-command-thread",
        run_id="future-command-run",
        message=raw,
        mode="build",
        requested_skill=requested,
    )

    record = SnapshotRegistry().resolve(command_id)
    _validate_command_invocation(
        command,
        requested,
        record,
        resolved_command_name="future.preview",
    )


@pytest.mark.parametrize(
    ("record_name", "source", "raw_name", "resolved_name", "expected"),
    (
        ("za38-sdd", "plugin:local/ZA38", "za38-sdd", "za38-sdd", True),
        ("tools:check", "plugin:local/tools", "tools:check", "tools:check", True),
        ("za38-sdd", "plugin:local/ZA38", "ZA38.za38-sdd", "ZA38.za38-sdd", True),
        ("za38-sdd", "plugin:local/ZA38", "ZA38.za38-sdd.1", "ZA38.za38-sdd.1", True),
        ("za38-sdd", "plugin:local/ZA38", "help", "za38-sdd", False),
        ("za38-sdd", "plugin:local/ZA38", "other.za38-sdd", "ZA38.za38-sdd", False),
    ),
)
def test_host_exact_command_binding_matches_cli_resolution(
    record_name: str,
    source: str,
    raw_name: str,
    resolved_name: str,
    expected: bool,
) -> None:
    """Host 只接受 CLI 已选中的 exact name，不根据 record 形状推断候选。"""
    from harness_agent.host.agent_host import _validate_command_invocation
    from harness_agent.host.run_coordinator import RequestedSkill, StartRun

    command_id = "plugin/local/ZA38/command/za38-sdd"
    raw = f"/{raw_name} dangerous-goal"
    requested = RequestedSkill(command_id, "dangerous-goal", raw, resolved_name)
    command = StartRun(
        thread_id="record-binding-thread",
        run_id="record-binding-run",
        message=raw,
        mode="build",
        requested_skill=requested,
    )
    record = SimpleNamespace(
        skill_id=command_id,
        kind="command",
        name=record_name,
        source=source,
        dialect="qwen-command",
    )

    if expected:
        _validate_command_invocation(
            command,
            requested,
            record,
            resolved_command_name=resolved_name,
        )
    else:
        with pytest.raises(PluginSkillError, match="COMMAND_INVOCATION_IDENTITY_MISMATCH"):
            _validate_command_invocation(
                command,
                requested,
                record,
                resolved_command_name=resolved_name,
            )


def test_host_rejects_unselected_plugin_natural_name_when_cli_binding_uses_fallback() -> None:
    """Host 必须使用 CLI 的 exact binding，不能把未选中的自然名当作已解析命令。"""
    from harness_agent.host.agent_host import _validate_command_invocation
    from harness_agent.host.run_coordinator import RequestedSkill, StartRun

    command_id = "plugin/local/bad/command/help"
    record = SimpleNamespace(
        skill_id=command_id,
        kind="command",
        name="help",
        source="plugin:local/bad",
        dialect="qwen-command",
    )
    rejected_raw = "/help dangerous-goal"
    rejected = StartRun(
        thread_id="exact-command-binding-thread",
        run_id="exact-command-binding-rejected",
        message=rejected_raw,
        mode="build",
        requested_skill=RequestedSkill(
            command_id,
            "dangerous-goal",
            rejected_raw,
            "help",
        ),
    )

    with pytest.raises(PluginSkillError, match="COMMAND_INVOCATION_IDENTITY_MISMATCH"):
        _validate_command_invocation(
            rejected,
            rejected.requested_skill,
            record,
            resolved_command_name="bad.help",
        )

    accepted_raw = "/bad.help dangerous-goal"
    accepted = replace(
        rejected,
        run_id="exact-command-binding-accepted",
        message=accepted_raw,
        requested_skill=RequestedSkill(
            command_id,
            "dangerous-goal",
            accepted_raw,
            "bad.help",
        ),
    )
    _validate_command_invocation(
        accepted,
        accepted.requested_skill,
        record,
        resolved_command_name="bad.help",
    )


@pytest.mark.asyncio
async def test_host_uses_immutable_cli_fallback_binding_for_plugin_help() -> None:
    """同一 snapshot 的 /bad.help 可运行，未被 CLI 选中的 /help 必须拒绝。"""
    from harness_agent.host.agent_host import AgentHost, _validate_command_invocation
    from harness_agent.host.run_coordinator import RequestedSkill, StartRun

    command_id = "plugin/local/bad/command/help"

    class SnapshotRegistry:
        snapshot_id = "snapshot-command-binding"

        @staticmethod
        def agent_commands() -> list[dict[str, object]]:
            return [
                {
                    "id": command_id,
                    "name": "help",
                    "description": "bad",
                    "argument_hint": None,
                    "requested_skill_id": command_id,
                    "plugin_id": "local/bad",
                }
            ]

    server = AgentHost(allow_echo=True)
    server._skill_registry = SnapshotRegistry()  # type: ignore[assignment]
    record = SimpleNamespace(
        skill_id=command_id,
        kind="command",
        name="help",
        source="plugin:local/bad",
        dialect="qwen-command",
    )
    try:
        result = await server._handle_commands_bind(
            {
                "snapshot_id": "snapshot-command-binding",
                "bindings": [{"id": command_id, "name": "bad.help"}],
            },
            "bind-help",
        )
        assert result == {
            "snapshot_id": "snapshot-command-binding",
            "accepted": True,
        }
        exact_name = server._owner_connection.command_bindings[command_id]

        accepted_raw = "/bad.help dangerous-goal"
        accepted = StartRun(
            thread_id="binding-help-thread",
            run_id="binding-help-run",
            message=accepted_raw,
            mode="build",
            requested_skill=RequestedSkill(
                command_id,
                "dangerous-goal",
                accepted_raw,
                "bad.help",
            ),
        )
        _validate_command_invocation(
            accepted,
            accepted.requested_skill,
            record,
            resolved_command_name=exact_name,
        )

        rejected_raw = "/help dangerous-goal"
        rejected = replace(
            accepted,
            run_id="binding-help-rejected",
            message=rejected_raw,
            requested_skill=RequestedSkill(
                command_id,
                "dangerous-goal",
                rejected_raw,
                "help",
            ),
        )
        with pytest.raises(PluginSkillError, match="COMMAND_INVOCATION_IDENTITY_MISMATCH"):
            _validate_command_invocation(
                rejected,
                rejected.requested_skill,
                record,
                resolved_command_name=exact_name,
            )
    finally:
        await server.close()


def test_qwen_bad_markdown_entries_are_isolated_and_remain_non_runnable_preview(
    tmp_path: Path,
) -> None:
    """坏 UTF-8、unsupported expansion 和坏 front matter 不扩大有效条目。"""
    source = _copy_fixture(tmp_path)
    (source / "commands" / "bad-encoding.md").write_bytes(b"\xff\xfe")
    (source / "commands" / "bad-expansion.md").write_text(
        "---\ndescription: bad\n---\n\nRun !{rm -rf /} and @{secret}.\n",
        encoding="utf-8",
    )
    bad_skill = source / "skills" / "bad-skill"
    bad_skill.mkdir(parents=True)
    (bad_skill / "SKILL.md").write_text(
        "---\nname: bad-skill\ndescription: bad\n---\n\n!{shell}\n",
        encoding="utf-8",
    )

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    components = _components(summary)
    assert components["commands"]["status"] == "adapted"
    assert components["commands"]["effective"] is True
    assert components["commands"]["count"] == 3
    assert components["skills"]["status"] == "adapted"
    assert components["skills"]["effective"] is True
    assert components["skills"]["count"] == 1
    assert any("bad-encoding.md" in item for item in components["commands"]["diagnostics"])
    assert any("bad-expansion.md" in item for item in components["commands"]["diagnostics"])
    assert any("bad-skill/SKILL.md" in item for item in components["skills"]["diagnostics"])

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.skill_sources(manager.catalog())
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
        plugin_diagnostics=result.diagnostics,
    )
    assert {record.name for record in registry.records if record.kind == "command"} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }
    preview = manager.static_preview()
    assert {item["name"] for item in preview["commands"]} == {
        "bad-encoding",
        "bad-expansion",
    }
    assert all(item["runnable"] is False for item in preview["commands"])
    assert [item["name"] for item in preview["skills"]] == ["bad-skill"]
    assert preview["skills"][0]["runnable"] is False


def test_qwen_markdown_path_symlink_and_size_boundaries_fail_closed_per_entry(
    tmp_path: Path,
) -> None:
    """路径越界、symlink 和超大 Markdown 不升级有效 component。"""
    source = _copy_fixture(tmp_path)
    (source / "commands" / "oversized.md").write_text(
        "---\nname: oversized\ndescription: too large\n---\n\n"
        + ("x" * (64 * 1024)),
        encoding="utf-8",
    )
    try:
        (source / "commands" / "linked.md").symlink_to(
            source / "commands" / "za38-sdd.md"
        )
    except OSError:
        pytest.skip("当前测试宿主不允许创建 symlink")
    manager = PluginManager(home=tmp_path / "symlink-home")
    with pytest.raises(PluginError) as symlink_error:
        manager.validate(source)
    assert symlink_error.value.code == "PLUGIN_SYMLINK_REJECTED"
    (source / "commands" / "linked.md").unlink()
    manifest = _manifest(source)
    manifest["commands"] = ["commands", "../outside"]
    _write_manifest(source, "devagent-extension.json", manifest)

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    commands = _components(summary)["commands"]
    assert commands["status"] == "adapted"
    assert commands["effective"] is True
    assert commands["count"] == 3
    diagnostics = "\n".join(commands["diagnostics"])
    assert "QWEN_MARKDOWN_TOO_LARGE" in diagnostics
    assert "PLUGIN_COMPONENT_PATH_INVALID" in diagnostics

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.skill_sources(manager.catalog())
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
    )
    assert {record.name for record in registry.records if record.kind == "command"} == {
        "za38-index",
        "za38-init",
        "za38-sdd",
    }


def test_qwen_command_expands_args_once_from_immutable_skill_snapshot(
    tmp_path: Path,
) -> None:
    """Command 正文只做一次纯文本 {{args}} 替换，参数过大 fail closed。"""
    source = _copy_fixture(tmp_path)
    (source / "commands" / "za38-sdd.md").write_text(
        "---\ndescription: SDD\nargument-hint: <goal>\n---\n\nCreate: {{args}}\n",
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
    result = manager.skill_sources(manager.catalog())
    registry = PluginSkillRegistry(
        tmp_path / "workspace",
        home=tmp_path / "home",
        plugin_sources=result.sources,
    )
    command = registry.resolve("za38-sdd")
    loaded = registry.load(command.skill_id, "创建登录功能")
    assert loaded.rendered_body() == "Create: 创建登录功能"

    # 模拟运行期间重新安装：旧 Registry 仍只读本次 Run 已捕获的正文，
    # 下一次 Registry 才能看到新的 package digest 和正文。
    (source / "commands" / "za38-sdd.md").write_text(
        "---\ndescription: replaced\nargument-hint: <goal>\n---\n\nReplaced: {{args}}\n",
        encoding="utf-8",
    )
    replacement = manager.install(source)["plugin"]
    assert isinstance(replacement, dict)
    assert replacement["package_digest"] != installed["package_digest"]
    assert registry.load(command.skill_id, "创建登录功能").rendered_body() == (
        "Create: 创建登录功能"
    )
    with pytest.raises(PluginSkillError, match="COMMAND_ARGUMENT_TOO_LARGE"):
        registry.load(command.skill_id, "x" * 100_000)


@pytest.mark.parametrize("manifest_name", ("qwen-extension.json", "devagent-extension.json"))
def test_qwen_default_commands_and_skills_are_effective_for_both_manifest_dialects(
    tmp_path: Path,
    manifest_name: str,
) -> None:
    """两种 Qwen family 清单省略目录字段时都扫描实际根目录并接入 runtime。"""
    source = _copy_fixture(tmp_path)
    if manifest_name == "qwen-extension.json":
        (source / "qwen-extension.json").write_text(
            (source / "devagent-extension.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (source / "devagent-extension.json").unlink()
    value = json.loads((source / manifest_name).read_text(encoding="utf-8"))
    value.pop("commands", None)
    value.pop("skills", None)
    (source / manifest_name).write_text(json.dumps(value), encoding="utf-8")

    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(summary, dict)
    components = _components(summary)
    assert components["commands"]["status"] == "adapted"
    assert components["commands"]["count"] == 3
    assert components["skills"]["status"] == "adapted"
    assert components["skills"]["count"] == 1


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

    assert len(initialized["static_command_preview"]) == 0
    assert len(skills["static_preview"]) == 0
    assert {
        agent["id"]
        for agent in agents["agents"]
        if agent["id"].startswith("za38-")
    } == {
        "za38-backend-executor",
        "za38-frontend-executor",
        "za38-java-executor",
    }
    assert agents["static_preview"] == []
    assert mcp["servers"] == []
    assert len(mcp["static_preview"]) == 0
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
    (source / "commands" / "default.md").write_text(
        "---\nname: default\ndescription: Default command.\n---\n\n# default\n",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize("manifest_name", ("qwen-extension.json", "devagent-extension.json"))
def test_qwen_default_component_report_missing_has_stable_diagnostics(
    tmp_path: Path,
    manifest_name: str,
) -> None:
    """两个 Qwen manifest 名称都必须识别默认根目录的缺报告声明。"""
    source = _copy_fixture(tmp_path)
    if manifest_name == "qwen-extension.json":
        (source / "devagent-extension.json").rename(source / manifest_name)
    manifest_path = source / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("commands", None)
    manifest.pop("skills", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source, format="qwen-code")["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    plugin = manager.store.read_registry().plugins[0]
    reports = tuple(
        component
        for component in plugin.components
        if component.kind not in {"commands", "skills"}
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

    result = manager.skill_sources(manager.catalog())

    assert result.sources == ()
    for kind in ("commands", "skills"):
        assert any(
            f"kind={kind}; reason=COMPONENT_REPORT_MISSING" in diagnostic
            for diagnostic in result.diagnostics
        ), result.diagnostics


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


def test_qwen_invalid_settings_and_out_of_scope_fields_are_reported(
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
    assert components["settings"]["status"] == "invalid"
    assert components["settings"]["effective"] is False
    assert any("SETTINGS_DECLARATION_INVALID" in item for item in components["settings"]["diagnostics"])
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
        "commands": 0,
        "skills": 0,
        "agents": 0,
        "mcp": 0,
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

        def reauthorized_hook_report(current: object) -> tuple[object, ...]:
            assert hasattr(current, "plugins")
            updated: list[object] = []
            for plugin in current.plugins:  # type: ignore[attr-defined]
                if plugin.plugin_id != plugin_id:
                    updated.append(plugin)
                    continue
                components = tuple(
                    replace(
                        component,
                        status="adapted",
                        effective=True,
                        capabilities=("process:hook",),
                        diagnostics=("re-authorized report",),
                    )
                    if component.kind == "hooks"
                    else component
                    for component in plugin.components
                )
                fingerprint = capability_fingerprint(components)
                updated.append(
                    replace(
                        plugin,
                        components=components,
                        capability_fingerprint=fingerprint,
                        trusted_capability_fingerprint=fingerprint,
                    )
                )
            return tuple(updated)

        server._plugin_manager.store.mutate_registry(reauthorized_hook_report)  # type: ignore[arg-type]
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
        mcp_snapshot=build_mcp_snapshot([], "missing"),
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
