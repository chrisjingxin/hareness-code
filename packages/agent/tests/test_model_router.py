"""ModelRouter 与 ProviderClientPool 的角色绑定、能力与复用测试。"""

from __future__ import annotations

import pytest

from harness_agent.config import ConfigError, ModelCatalog, ModelProfile, ModelSettings
from harness_agent.model_router import ModelRouter
from harness_agent.providers.harness_gateway import ProviderClientPool


def _profile(profile_id: str, *, capabilities: frozenset[str] | None = None) -> ModelProfile:
    """构造不依赖真实环境变量的测试 Profile。"""
    return ModelProfile(
        profile_id=profile_id,
        settings=ModelSettings(
            name=f"{profile_id}-model",
            base_url=f"https://{profile_id}.example/v1",
            api_key="test-key",
            capabilities=capabilities or frozenset({"tool-calling", "streaming"}),
        ),
        source="test",
    )


def test_model_router_resolves_roles_and_overrides_only_executor() -> None:
    """单 Agent 只将 executor 映射到 primary，其余角色仍冻结给未来 topology。"""
    catalog = ModelCatalog(
        default_profile="fast",
        profiles={"fast": _profile("fast"), "pro": _profile("pro")},
        role_profiles={"planner": "pro", "executor": "fast"},
    )

    bindings = ModelRouter(catalog).bind_thread("pro")

    assert bindings.for_role("planner").profile_id == "pro"
    assert bindings.for_role("executor").profile_id == "pro"
    assert bindings.for_role("reviewer").profile_id == "fast"
    assert bindings.runtime_primary().profile_id == "pro"
    record = bindings.record()
    assert record["roles"]["executor"]["id"] == "pro"  # type: ignore[index]
    assert "https://pro.example" not in str(record)
    assert "test-key" not in str(record)


def test_model_router_rejects_executor_without_required_capabilities() -> None:
    """当前 Single Agent 的 executor 缺少工具或流式能力时不能启动。"""
    catalog = ModelCatalog(
        default_profile="limited",
        profiles={"limited": _profile("limited", capabilities=frozenset({"streaming"}))},
        role_profiles={},
    )

    with pytest.raises(ConfigError, match="MODEL_PROFILE_CAPABILITY_MISSING: tool-calling"):
        ModelRouter(catalog).bind_thread()


async def test_provider_client_pool_reuses_uncredentialed_transport() -> None:
    """相同 endpoint/超时的 Profile 共享 transport，凭据和 Header 不进入复用键。"""
    pool = ProviderClientPool()
    first = ModelSettings(
        name="fast",
        base_url="https://gateway.example/v1",
        api_key="first-key",
        headers={"X-Tenant": "one"},
    )
    same_transport = ModelSettings(
        name="pro",
        base_url="https://gateway.example/v1",
        api_key="second-key",
        headers={"X-Tenant": "two"},
    )
    isolated = ModelSettings(
        name="other",
        base_url="https://other.example/v1",
        api_key="third-key",
    )
    try:
        assert await pool.get_async_client(first) is await pool.get_async_client(same_transport)
        assert await pool.get_async_client(first) is not await pool.get_async_client(isolated)
    finally:
        await pool.aclose()
