"""Agent/Policy catalog 的来源、引用与不提权交集测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.agent_catalog import (
    AgentCatalog,
    AgentCatalogError,
    DelegationPolicy,
    ExecutionPolicyDefinition,
    NetworkPolicy,
    PluginAgentSource,
    ShellPolicy,
    StringRule,
    intersect_execution_policies,
)
from harness_agent.config import ModelCatalog, ModelProfile, ModelSettings


def _models() -> ModelCatalog:
    """构造不含真实凭据的最小模型目录。"""
    return ModelCatalog(
        default_profile="fast",
        profiles={
            "fast": ModelProfile("fast", ModelSettings("fast-model", "https://example.test"), "test"),
            "pro": ModelProfile("pro", ModelSettings("pro-model", "https://example.test"), "test"),
        },
        role_profiles={},
    )


def _write_json(path: Path, value: object) -> None:
    """写入测试 catalog 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_plugin_agent(root: Path, *, model: str = "fast") -> None:
    """写入包含指令、fragment 和输出 Schema 的合法 Plugin Agent。"""
    _write_json(
        root / "policies" / "review.json",
        {
            "id": "review",
            "tools": {"allow": ["read_file", "write_file"]},
            "filesystem": {"read": ["**/*"], "write": ["**/*"]},
            "shell": {"enabled": True, "allowedCommands": ["git", "pytest"]},
            "network": {"enabled": True, "allowedHosts": ["api.example.test"]},
            "approval": {"mode": "never"},
            "delegation": {"enabled": True, "allowedAgents": ["reviewer"], "maxDepth": 3, "maxParallelism": 4},
        },
    )
    _write_json(
        root / "agents" / "reviewer.json",
        {
            "id": "reviewer",
            "purpose": "审查代码变更",
            "instructionsRef": "reviewer.md",
            "instructionFragments": ["evidence-first.md"],
            "outputContractRef": "review-result.json",
            "modelProfileId": model,
            "executionPolicyId": "review",
        },
    )
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "reviewer.md").write_text("只报告有证据的问题。", encoding="utf-8")
    (root / "instructions").mkdir(parents=True, exist_ok=True)
    (root / "instructions" / "evidence-first.md").write_text("结论必须包含证据。", encoding="utf-8")
    _write_json(root / "schemas" / "review-result.json", {"type": "object", "properties": {"issues": {"type": "array"}}})


def test_catalog_loads_plugin_assets_and_fingerprints_content(tmp_path: Path) -> None:
    """合法 Plugin Agent 只能从已传入根目录读取，资产改变必须形成新快照。"""
    root = tmp_path / "plugin"
    _write_plugin_agent(root)
    source = PluginAgentSource("review-plugin", root)

    first = AgentCatalog(model_catalog=_models(), sources=(source,))
    reviewer = first.require_agent("reviewer")
    assert reviewer.model_profile_id == "fast"
    assert reviewer.source == "plugin:review-plugin"
    with pytest.raises(AgentCatalogError, match="AGENT_NOT_FOUND"):
        first.require_agent("main")
    assert str(tmp_path) not in str(first.list_agents())

    fragment = root / "instructions" / "evidence-first.md"
    fragment.write_text("每条结论都必须包含可复核证据。", encoding="utf-8")
    second = AgentCatalog(model_catalog=_models(), sources=(source,))
    assert second.snapshot_id != first.snapshot_id


def test_invalid_or_unpassed_sources_do_not_enter_plugin_catalog(tmp_path: Path) -> None:
    """损坏 Plugin 项被忽略，项目目录未显式作为 Plugin Source 时根本不会读取。"""
    valid_root = tmp_path / "valid-plugin"
    _write_plugin_agent(valid_root)
    invalid_root = tmp_path / "invalid-plugin"
    _write_json(
        invalid_root / "agents" / "broken.json",
        {"id": "broken", "purpose": "bad", "instructionsRef": "../../secret.md"},
    )
    project = tmp_path / "project" / ".harness" / "agents"
    _write_json(
        project / "project-agent.json",
        {
            "id": "project-agent",
            "purpose": "不可信项目定义",
            "instructionsRef": "project-agent.md",
            "modelProfileId": "fast",
            "executionPolicyId": "main",
        },
    )

    catalog = AgentCatalog(
        model_catalog=_models(),
        sources=(
            PluginAgentSource("valid-plugin", valid_root),
            PluginAgentSource("invalid-plugin", invalid_root),
        ),
    )
    assert catalog.require_agent("reviewer").source == "plugin:valid-plugin"
    with pytest.raises(AgentCatalogError, match="AGENT_NOT_FOUND"):
        catalog.require_agent("project-agent")
    assert all(str(tmp_path) not in diagnostic for diagnostic in catalog.diagnostics)


def test_policy_intersection_cannot_relax_parent_envelope(tmp_path: Path) -> None:
    """目标 Agent 请求写入、Shell、网络、免审批或 delegation 都不能超过父边界。"""
    root = tmp_path / "plugin"
    _write_plugin_agent(root)
    catalog = AgentCatalog(
        model_catalog=_models(),
        sources=(PluginAgentSource("review-plugin", root),),
    )
    parent = ExecutionPolicyDefinition(
        policy_id="parent",
        source="test",
        tools=StringRule(allow=("read_file",)),
        filesystem_read=("**/*",),
        filesystem_write=(),
        shell=ShellPolicy(enabled=False),
        network=NetworkPolicy(enabled=False),
        approval_mode="always",
        delegation=DelegationPolicy(enabled=False),
    )
    effective = catalog.effective_policy("reviewer", envelope=parent)

    assert effective.tools is not None and effective.tools.allow == ("read_file",)
    assert effective.filesystem_write == ()
    assert effective.shell is not None and effective.shell.enabled is False
    assert effective.network is not None and effective.network.enabled is False
    assert effective.approval_mode == "always"
    assert effective.delegation is not None and effective.delegation.enabled is False


def test_policy_intersection_rejects_incompatible_isolation() -> None:
    """未知安全偏序不能猜测哪个隔离环境更严格。"""
    parent = ExecutionPolicyDefinition(policy_id="parent", source="test", isolation="container")
    target = ExecutionPolicyDefinition(policy_id="target", source="test", isolation="worktree")
    with pytest.raises(AgentCatalogError, match="ISOLATION_CONFLICT"):
        intersect_execution_policies(parent, target)
