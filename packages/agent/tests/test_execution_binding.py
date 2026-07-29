"""执行绑定 deep module 的选择优先级、兼容与脱敏测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.config import (
    ConfigError,
    ExecutionSettings,
    ModelCatalog,
    ModelProfile,
    ModelSettings,
    AgentEnginePoolSettings,
    Za38Config,
)
from harness_agent.execution_binding import (
    ExecutionBindingError,
    LegacyModelBindings,
    PersistedBindingState,
    RunExecutionBinding,
    SafeModelProfile,
    SelectionOrigin,
    ThreadExecutionSelection,
    describe_thread_binding,
    resolve_execution_binding,
)


def _profile(
    profile_id: str,
    *,
    capabilities: frozenset[str] = frozenset({"tool-calling", "streaming"}),
    api_key: str | None = "test-key",
) -> ModelProfile:
    """构造不依赖真实环境变量的模型 Profile。"""
    return ModelProfile(
        profile_id=profile_id,
        settings=ModelSettings(
            name=f"{profile_id}-model",
            base_url=f"https://{profile_id}.example/v1",
            api_key=api_key,
            headers={"Authorization": "secret-header"},
            capabilities=capabilities,
        ),
        source="test",
    )


def _config() -> Za38Config:
    """构造 fast 默认、pro 可选的类型化配置。"""
    catalog = ModelCatalog(
        default_profile="fast",
        profiles={"fast": _profile("fast"), "pro": _profile("pro")},
        role_profiles={"executor": "pro"},
    )
    return Za38Config(
        model=catalog.require_profile().settings,
        model_profile=catalog.default_profile,
        execution=ExecutionSettings(),
        agent_engine_pool=AgentEnginePoolSettings(),
        paths=(),
        workspace=Path("/workspace"),
        sources={},
        model_catalog=catalog,
    )


def _persisted_run(profile_id: str = "pro") -> RunExecutionBinding:
    """构造最近一次 Run 的恢复事实。"""
    profile = replace(_profile(profile_id), is_default=profile_id == "fast")
    return RunExecutionBinding(
        thread_id="thread",
        run_id="previous",
        requested_selection=ThreadExecutionSelection(profile_id),
        actual_primary=SafeModelProfile.from_profile(profile),
        selection_origin=SelectionOrigin.REQUEST,
        runtime_profile_id="123456789abc",
        created_at_ms=1,
    )


def _legacy(profile_id: str = "pro") -> LegacyModelBindings:
    """构造 v5 executor 快照。"""
    return LegacyModelBindings(
        roles=(("executor", SafeModelProfile.from_profile(_profile(profile_id))),)
    )


@pytest.mark.parametrize(
    ("requested", "persisted", "expected_profile", "expected_origin"),
    [
        (
            ThreadExecutionSelection("fast"),
            PersistedBindingState(latest_run=_persisted_run("pro"), legacy_models=_legacy("pro")),
            "fast",
            SelectionOrigin.REQUEST,
        ),
        (
            None,
            PersistedBindingState(latest_run=_persisted_run("pro"), legacy_models=_legacy("fast")),
            "pro",
            SelectionOrigin.RECOVERED,
        ),
        (
            None,
            PersistedBindingState(legacy_models=_legacy("pro")),
            "pro",
            SelectionOrigin.LEGACY,
        ),
        (
            None,
            PersistedBindingState(),
            "fast",
            SelectionOrigin.CONFIG_DEFAULT,
        ),
    ],
)
def test_resolve_execution_binding_uses_one_precedence_chain(
    requested: ThreadExecutionSelection | None,
    persisted: PersistedBindingState,
    expected_profile: str,
    expected_origin: SelectionOrigin,
) -> None:
    """请求、最近 Run、legacy 和默认值必须只在一个 interface 中排序。"""
    resolved = resolve_execution_binding(_config(), requested, persisted)

    assert resolved.selection.root_model_profile_id == expected_profile
    assert resolved.primary_profile.profile_id == expected_profile
    assert resolved.selection_origin is expected_origin


def test_resolved_binding_builds_safe_immutable_run_fact() -> None:
    """AgentEngine 输入与持久化事实必须来自同一个解析结果且保持脱敏。"""
    resolved = resolve_execution_binding(
        _config(),
        ThreadExecutionSelection("pro"),
        PersistedBindingState(),
    )

    binding = resolved.bind_run(
        thread_id="thread",
        run_id="run",
        runtime_profile_id="123456789abc",
        created_at_ms=42,
    )

    assert resolved.primary_profile.settings.name == "pro-model"
    assert binding.requested_selection_record() == {"primary_profile": "pro"}
    assert binding.actual_primary_record()["source"] == "thread-primary"
    serialized = str(binding.actual_primary_record())
    assert "https://pro.example" not in serialized
    assert "test-key" not in serialized
    assert "secret-header" not in serialized


def test_compatibility_config_rejects_explicit_profile() -> None:
    """旧单模型配置没有 catalog 时不能接受命名 Profile 选择。"""
    profile = _profile("default")
    config = Za38Config(
        model=profile.settings,
        model_profile="default",
        execution=ExecutionSettings(),
        agent_engine_pool=AgentEnginePoolSettings(),
        paths=(),
        workspace=Path("/workspace"),
        sources={},
    )

    with pytest.raises(ConfigError, match="MODEL_CATALOG_UNAVAILABLE"):
        resolve_execution_binding(
            config,
            ThreadExecutionSelection("pro"),
            PersistedBindingState(),
        )

    resolved = resolve_execution_binding(config, None, PersistedBindingState())
    assert resolved.primary_profile.profile_id == "default"
    assert resolved.safe_primary.source == "compatibility"


def test_resolve_execution_binding_rejects_unknown_profile() -> None:
    """显式或恢复出的未知 Profile 不得静默回退到默认模型。"""
    with pytest.raises(ConfigError, match="MODEL_PROFILE_NOT_FOUND: missing"):
        resolve_execution_binding(
            _config(),
            ThreadExecutionSelection("missing"),
            PersistedBindingState(),
        )


@pytest.mark.parametrize(
    ("profile", "error"),
    [
        (_profile("missing", api_key=None), "MODEL_PROFILE_UNAVAILABLE: API_KEY_MISSING"),
        (
            _profile("limited", capabilities=frozenset({"streaming"})),
            "MODEL_PROFILE_CAPABILITY_MISSING: tool-calling",
        ),
    ],
)
def test_resolve_execution_binding_rejects_unusable_model(
    profile: ModelProfile, error: str
) -> None:
    """根模型缺少凭据或必要能力时必须 fail closed。"""
    catalog = ModelCatalog(
        default_profile=profile.profile_id,
        profiles={profile.profile_id: profile},
        role_profiles={},
    )
    config = replace(
        _config(),
        model=profile.settings,
        model_profile=profile.profile_id,
        model_catalog=catalog,
    )

    with pytest.raises(ConfigError, match=error):
        resolve_execution_binding(config, None, PersistedBindingState())


def test_binding_decoders_reject_extra_sensitive_fields() -> None:
    """旧 JSON 中出现未审计字段时不得进入类型化当前路径。"""
    record = SafeModelProfile.from_profile(_profile("fast")).to_record()
    record["base_url"] = "https://secret.example"

    with pytest.raises(ExecutionBindingError, match="RUN_EXECUTION_BINDING_INVALID"):
        SafeModelProfile.from_record(record)


def test_run_binding_decoder_rejects_unknown_source() -> None:
    """损坏的来源字符串不能进入当前选择恢复路径。"""
    profile = SafeModelProfile.from_profile(_profile("fast")).to_record()

    with pytest.raises(ExecutionBindingError, match="RUN_EXECUTION_BINDING_INVALID"):
        RunExecutionBinding.from_records(
            thread_id="thread",
            run_id="run",
            requested_selection={"primary_profile": "fast"},
            actual_primary_binding={"profile": profile, "source": "unknown"},
            runtime_profile_id="123456789abc",
            created_at_ms=1,
        )


def test_describe_thread_binding_preserves_legacy_unknown() -> None:
    """只有旧 AgentEngine 指纹时不能伪造当前默认模型。"""
    view = describe_thread_binding(PersistedBindingState(has_legacy_runtime=True))

    assert view.to_record() == {"state": "legacy", "roles": {}}
