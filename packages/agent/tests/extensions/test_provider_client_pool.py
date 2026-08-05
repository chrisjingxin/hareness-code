"""Provider HTTP transport 复用测试。"""

from __future__ import annotations

from harness_agent.config.config import ModelSettings
from harness_agent.extensions.providers.harness_gateway import ProviderClientPool


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
        first_lease = await pool.acquire(first)
        same_lease = await pool.acquire(same_transport)
        isolated_lease = await pool.acquire(isolated)
        try:
            assert first_lease.value is same_lease.value
            assert first_lease.value is not isolated_lease.value
        finally:
            await first_lease.release()
            await same_lease.release()
            await isolated_lease.release()
    finally:
        await pool.aclose()
