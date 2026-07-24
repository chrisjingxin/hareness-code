"""Runtime Profile 身份、脱敏和 ThreadStore 迁移回归测试。"""

from __future__ import annotations

from dataclasses import replace
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from harness_agent.approval_mode import DEFAULT_APPROVAL_MODE
from harness_agent.config import ExecutionSettings, ModelSettings
from harness_agent.runtime_profile import (
    ModelRoleBinding,
    RuntimeProfile,
    component_fingerprint,
    default_runtime_profile,
)
from harness_agent.thread_store import ContextState, ThreadStore, ThreadStoreError


def _profile(project_fingerprint: str) -> RuntimeProfile:
    """构造一份不含真实配置的固定 Profile，供身份和存储测试复用。"""
    return RuntimeProfile(
        project_fingerprint=project_fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=(
            ModelRoleBinding("reviewer", component_fingerprint({"model": "review"})),
            ModelRoleBinding("primary", component_fingerprint({"model": "primary"})),
        ),
        tool_catalog_fingerprint=component_fingerprint({"tools": ["read", "write"]}),
        skill_catalog_fingerprint=component_fingerprint({"skills": []}),
        mcp_config_fingerprint=component_fingerprint({"mcp": "disabled"}),
        sandbox_config_fingerprint=component_fingerprint({"sandbox": "local"}),
        policy_fingerprint=component_fingerprint({"approval": "default"}),
        middleware_fingerprint=component_fingerprint({"middleware": 1}),
        prompt_template_fingerprint=component_fingerprint({"prompt": 2}),
    )


def test_runtime_profile_key_is_stable_and_excludes_thread_run_state() -> None:
    """相同图结构必须共享 Key，动态消息和 run 状态不能成为 Profile 字段。"""
    project_fingerprint = component_fingerprint({"project": "a"})
    first = _profile(project_fingerprint)
    second = RuntimeProfile(
        project_fingerprint=project_fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=tuple(reversed(first.model_roles)),
        tool_catalog_fingerprint=first.tool_catalog_fingerprint,
        skill_catalog_fingerprint=first.skill_catalog_fingerprint,
        mcp_config_fingerprint=first.mcp_config_fingerprint,
        sandbox_config_fingerprint=first.sandbox_config_fingerprint,
        policy_fingerprint=first.policy_fingerprint,
        middleware_fingerprint=first.middleware_fingerprint,
        prompt_template_fingerprint=first.prompt_template_fingerprint,
    )

    assert first.profile_key == second.profile_key
    assert first.record() == second.record()
    assert "thread_id" not in first.record()
    assert "run_id" not in first.record()
    assert "messages" not in first.record()
    for field_name in (
        "project_fingerprint",
        "tool_catalog_fingerprint",
        "skill_catalog_fingerprint",
        "mcp_config_fingerprint",
        "sandbox_config_fingerprint",
        "policy_fingerprint",
        "middleware_fingerprint",
        "prompt_template_fingerprint",
    ):
        assert replace(first, **{field_name: component_fingerprint({field_name: "changed"})}).profile_key != first.profile_key
    assert replace(first, topology_version=2).profile_key != first.profile_key
    assert replace(
        first,
        model_roles=(ModelRoleBinding("primary", component_fingerprint({"model": "changed"})),),
    ).profile_key != first.profile_key
    assert RuntimeProfile.from_record(first.record()) == first


def test_default_runtime_profile_hashes_model_and_execution_without_leaking_secrets() -> None:
    """模型 endpoint、固定 Header 与 API Key 只能参与哈希，不能进入 Profile 记录。"""
    profile = default_runtime_profile(
        project_fingerprint=component_fingerprint({"project": "a"}),
        model_profile="enterprise",
        model=ModelSettings(
            name="fast-model",
            base_url="https://gateway.example/v1",
            api_key="toml-secret",
            headers={"X-Trace": "trace-secret"},
        ),
        tool_catalog_fingerprint=component_fingerprint({"tools": ["read"]}),
        skill_catalog_fingerprint=component_fingerprint({"skills": "snapshot"}),
        execution=ExecutionSettings(approval_mode=DEFAULT_APPROVAL_MODE),
        middleware_fingerprint=component_fingerprint({"middleware": "v1"}),
        prompt_template_fingerprint=component_fingerprint({"prompt": "v2"}),
    )

    encoded = str(profile.record())
    assert profile.model_roles[0].role == "primary"
    assert "toml-secret" not in encoded
    assert "trace-secret" not in encoded
    assert "gateway.example" not in encoded


async def test_thread_store_persists_immutable_runtime_profile_without_raw_values(tmp_path: Path) -> None:
    """Thread 绑定 Profile 后可重开读取，变更绑定或跨项目写入必须拒绝。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadStore.open(project=project, home=home)
    await store.record_message("thread-1", "开始实现")
    profile = _profile(store.project_fingerprint)
    await store.save_runtime_profile("thread-1", profile)
    await store.save_runtime_profile("thread-1", profile)
    assert await store.get_runtime_profile("thread-1") == profile
    with pytest.raises(ThreadStoreError, match="RUNTIME_PROFILE_IMMUTABLE"):
        await store.save_runtime_profile(
            "thread-1",
            replace(profile, policy_fingerprint=component_fingerprint({"approval": "plan"})),
        )
    with pytest.raises(ThreadStoreError, match="RUNTIME_PROFILE_PROJECT_MISMATCH"):
        await store.save_runtime_profile("thread-2", _profile(component_fingerprint({"project": "other"})))
    database = store.database_path
    await store.close()

    connection = sqlite3.connect(database)
    try:
        stored = connection.execute("SELECT profile_record FROM harness_runtime_profiles").fetchone()[0]
    finally:
        connection.close()
    assert str(project) not in stored
    assert "toml-secret" not in stored

    reopened = await ThreadStore.open(project=project, home=home)
    assert await reopened.get_runtime_profile("thread-1") == profile
    await reopened.close()


async def test_thread_store_upgrades_v3_runtime_profile_schema_without_losing_epoch(tmp_path: Path) -> None:
    """v3 数据库升级到 v5 时，旧 thread 继续读取且保持未绑定兼容状态。"""
    from harness_agent.prompting import PromptComposer

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadStore.open(project=project, home=home)
    await store.record_message("legacy-thread", "旧请求")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="旧请求")]}
    await store.checkpointer.aput(store.graph_config("legacy-thread"), checkpoint, {}, {})
    await store.refresh_thread("legacy-thread")
    artifact = await store.archive_context("legacy-thread", kind="tool-result", content="旧工具原文")
    await store.set_context_state("legacy-thread", ContextState(failures=2, circuit_open=False, last_action="archive"))
    epoch = PromptComposer("core").create_epoch(
        thread_id="legacy-thread",
        execution_boundary="execution",
        environment={"workspace": "logical-workspace"},
        readonly_memory="",
        skill_index="<skills />",
        tool_fingerprint="schema",
        now_ms=1,
    )
    await store.save_prompt_epoch(epoch)
    database = store.database_path
    await store.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_thread_model_bindings")
        connection.execute("DROP TABLE harness_thread_runtime_profiles")
        connection.execute("DROP TABLE harness_runtime_profiles")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()

    upgraded = await ThreadStore.open(project=project, home=home)
    assert await upgraded.get_runtime_profile("legacy-thread") is None
    assert await upgraded.get_prompt_epoch("legacy-thread") == epoch
    assert (await upgraded.open_thread("legacy-thread")).summary.first_message == "旧请求"
    assert await upgraded.read_context_artifact("legacy-thread", artifact.artifact_id) == artifact
    assert await upgraded.context_state("legacy-thread") == ContextState(
        failures=2,
        circuit_open=False,
        last_action="archive",
    )
    await upgraded.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'harness_runtime_profiles'"
        ).fetchone()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'harness_thread_model_bindings'"
        ).fetchone()
    finally:
        connection.close()
