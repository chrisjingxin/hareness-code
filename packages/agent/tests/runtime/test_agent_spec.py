"""角色级 ResolvedAgentSpec 与 AgentEngineProfile 身份测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.runtime.agent_engine import AgentEngineError, AgentEnginePool
from harness_agent.runtime.agent_engine_profile import (
    AGENT_ENGINE_PROFILE_VERSION,
    AgentEngineProfile,
    ModelRoleBinding,
    component_fingerprint,
)
from harness_agent.runtime.agent_spec import (
    RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION,
    resolve_builtin_main_agent_spec,
)
from harness_agent.policy.approval_mode import DEFAULT_APPROVAL_MODE
from harness_agent.config.config import ExecutionSettings, ModelProfile, ModelSettings
from harness_agent.runtime.execution_binding import (
    ResolvedExecutionBinding,
    SafeModelProfile,
    SelectionOrigin,
    ThreadExecutionSelection,
)
from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot
from harness_agent.extensions.skills import SkillRegistry
from harness_agent.threads.prompting import sha256_text


def _binding(model_name: str = "fast-model", *, api_key: str | None = "secret") -> ResolvedExecutionBinding:
    """构造最小的根模型解析结果。"""
    profile = ModelProfile(
        profile_id="fast",
        settings=ModelSettings(model_name, "https://gateway.example/v1", api_key=api_key),
        source="test",
    )
    return ResolvedExecutionBinding(
        selection=ThreadExecutionSelection("fast"),
        primary_profile=profile,
        safe_primary=SafeModelProfile.from_profile(profile),
        selection_origin=SelectionOrigin.CONFIG_DEFAULT,
    )


def _spec(tmp_path: Path, *, model_name: str = "fast-model", tools: tuple[object, ...] = ()):
    """构造内置 main 的一次完整角色解析。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return resolve_builtin_main_agent_spec(
        project_fingerprint=component_fingerprint({"project": "test"}),
        workspace=workspace,
        binding=_binding(model_name),
        execution=ExecutionSettings(approval_mode=DEFAULT_APPROVAL_MODE),
        skill_registry=SkillRegistry(workspace, home=tmp_path / "home"),
        mcp_snapshot=build_mcp_snapshot([], revision="test"),
        mcp_tools=tools,
        interactive=True,
        pinned=False,
    )


def test_same_resolved_agent_spec_reuses_key_but_static_behavior_changes_do_not(tmp_path: Path) -> None:
    """同一角色同一快照共享 Key，模型/Policy/Prompt/能力/沙箱变化都会换 Key。"""
    first = _spec(tmp_path)
    second = _spec(tmp_path)
    assert first.runtime_profile.profile_key == second.runtime_profile.profile_key

    changed_model = _spec(tmp_path, model_name="pro-model")
    assert changed_model.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_policy = replace(
        first,
        effective_policy=replace(first.effective_policy, approval_mode="always"),
    )
    assert changed_policy.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_prompt = replace(first, prompt="另一份系统提示词")
    assert changed_prompt.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_tools = _spec(tmp_path, tools=({"name": "mcp_read", "parameters": {"path": "string"}},))
    assert changed_tools.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_skill_view = replace(
        first,
        skill_view_fingerprint=component_fingerprint({"skills": ["review"]}),
    )
    assert changed_skill_view.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_mcp = replace(
        first,
        mcp_snapshot=build_mcp_snapshot(
            (McpServerConfig(name="review", transport="stdio", command="review-mcp"),),
            revision="changed",
        ),
    )
    assert changed_mcp.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_sandbox = replace(
        first,
        execution=ExecutionSettings(sandbox_enabled=True, approval_mode=DEFAULT_APPROVAL_MODE),
    )
    assert changed_sandbox.runtime_profile.profile_key != first.runtime_profile.profile_key

    changed_workspace = replace(first, workspace=tmp_path / "other-workspace")
    assert changed_workspace.runtime_profile.profile_key != first.runtime_profile.profile_key


def test_snapshot_middleware_profile_key_is_not_legacy_prompt_epoch(tmp_path: Path) -> None:
    """RunContextSnapshot middleware 与旧 PromptEpoch 语义不能复用 Profile。"""
    current = _spec(tmp_path)
    legacy_marker = sha256_text(
        str(
            (
                "prompt-epoch-v1",
                "context-window-v1",
                "workspace-boundary-v1",
                "interactive-question",
                "memory-on",
                "skills-on",
            )
        )
    )
    legacy = replace(current, middleware_fingerprint=legacy_marker)

    expected_marker = sha256_text(
        str(
            (
                RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION,
                "context-window-v1",
                "workspace-boundary-v1",
                "interactive-question",
                "memory-on",
                "skills-on",
            )
        )
    )
    assert current.middleware_fingerprint == expected_marker
    assert current.runtime_profile.profile_key != legacy.runtime_profile.profile_key


def test_dynamic_run_context_and_credentials_do_not_enter_profile_identity(tmp_path: Path) -> None:
    """Thread、Run、Team、任务文本和凭据改变时，静态 AgentEngine 身份保持不变。"""
    first = _spec(tmp_path)
    with_other_credentials = replace(
        first,
        model_settings=ModelSettings(
            "fast-model",
            "https://gateway.example/v1",
            api_key="another-secret",
        ),
    )
    assert with_other_credentials.runtime_profile.profile_key == first.runtime_profile.profile_key
    assert "another-secret" not in str(with_other_credentials.runtime_profile.record())
    assert "thread_id" not in str(first.runtime_profile.record())
    assert "run_id" not in str(first.runtime_profile.record())
    assert "task" not in str(first.runtime_profile.record())
    assert "team" not in str(first.runtime_profile.record())


def test_v1_profile_is_readable_but_not_a_new_role_identity() -> None:
    """旧记录仍可校验读取，但不能写回或进入新 Pool。"""
    fingerprint = component_fingerprint({"legacy": True})
    legacy = AgentEngineProfile(
        project_fingerprint=fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding("primary", fingerprint),),
        tool_catalog_fingerprint=fingerprint,
        skill_catalog_fingerprint=fingerprint,
        mcp_config_fingerprint=fingerprint,
        sandbox_config_fingerprint=fingerprint,
        policy_fingerprint=fingerprint,
        middleware_fingerprint=fingerprint,
        prompt_template_fingerprint=fingerprint,
        profile_version=1,
    )
    restored = AgentEngineProfile.from_record(legacy.record())
    assert restored.is_legacy is True
    assert restored.profile_version == 1
    assert "agent" not in restored.record()
    assert restored.profile_key != replace(legacy, profile_version=AGENT_ENGINE_PROFILE_VERSION).profile_key


async def test_legacy_profile_cannot_be_acquired() -> None:
    """Pool 不应把旧 Profile 当作当前角色图复用。"""
    fingerprint = component_fingerprint({"legacy-pool": True})
    legacy = AgentEngineProfile(
        project_fingerprint=fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding("primary", fingerprint),),
        tool_catalog_fingerprint=fingerprint,
        skill_catalog_fingerprint=fingerprint,
        mcp_config_fingerprint=fingerprint,
        sandbox_config_fingerprint=fingerprint,
        policy_fingerprint=fingerprint,
        middleware_fingerprint=fingerprint,
        prompt_template_fingerprint=fingerprint,
        profile_version=1,
    )
    pool = AgentEnginePool(lambda profile: pytest.fail(f"unexpected build: {profile}"))
    with pytest.raises(AgentEngineError, match="LEGACY_PROFILE"):
        await pool.acquire(legacy)
    await pool.aclose()


async def test_legacy_profile_cannot_be_persisted(tmp_path: Path) -> None:
    """旧 Profile 可读但只能作为历史诊断，不能写入当前 Profile 表。"""
    from harness_agent.threads.thread_persistence import ThreadPersistence, ThreadPersistenceError

    fingerprint = component_fingerprint({"legacy-persistence": True})
    legacy = AgentEngineProfile(
        project_fingerprint=fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding("primary", fingerprint),),
        tool_catalog_fingerprint=fingerprint,
        skill_catalog_fingerprint=fingerprint,
        mcp_config_fingerprint=fingerprint,
        sandbox_config_fingerprint=fingerprint,
        policy_fingerprint=fingerprint,
        middleware_fingerprint=fingerprint,
        prompt_template_fingerprint=fingerprint,
        profile_version=1,
    )
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    try:
        with pytest.raises(ThreadPersistenceError, match="LEGACY_READ_ONLY"):
            await store.persist_agent_engine_profile(legacy)
    finally:
        await store.close()
