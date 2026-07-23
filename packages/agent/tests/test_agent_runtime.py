"""RuntimePool 的共享构建、租约状态与资源关闭回归测试。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest


def _profile(name: str = "default"):
    """创建一个字段完整但不含真实路径或模型凭据的测试 Profile。"""
    from harness_agent.runtime_profile import ModelRoleBinding, RuntimeProfile, component_fingerprint

    def fingerprint(component: str) -> str:
        return component_fingerprint({"test": name, "component": component})

    return RuntimeProfile(
        project_fingerprint=fingerprint("project"),
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding(role="primary", model_config_fingerprint=fingerprint("model")),),
        tool_catalog_fingerprint=fingerprint("tools"),
        skill_catalog_fingerprint=fingerprint("skills"),
        mcp_config_fingerprint=fingerprint("mcp"),
        sandbox_config_fingerprint=fingerprint("sandbox"),
        policy_fingerprint=fingerprint("policy"),
        middleware_fingerprint=fingerprint("middleware"),
        prompt_template_fingerprint=fingerprint("prompt"),
    )


async def test_runtime_pool_single_flight_builds_one_runtime_for_concurrent_acquires():
    """100 个同 Profile acquire 必须共享一项 BUILDING task 与同一图实例。"""
    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState, RuntimePool

    profile = _profile()
    release_builder = asyncio.Event()
    builds = 0

    async def build(requested: Any) -> AgentRuntime:
        nonlocal builds
        builds += 1
        await release_builder.wait()
        return AgentRuntime(profile=requested, graph=object())

    pool = RuntimePool(build)
    acquires = [asyncio.create_task(pool.acquire(profile)) for _ in range(100)]
    for _ in range(10):
        await asyncio.sleep(0)
        if builds:
            break
    assert builds == 1
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.BUILDING

    release_builder.set()
    leases = await asyncio.gather(*acquires)
    runtime = leases[0].runtime
    assert all(lease.runtime is runtime for lease in leases)
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.ACTIVE

    await asyncio.gather(*(lease.release() for lease in leases))
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.IDLE
    await pool.aclose()


async def test_runtime_pool_discards_failed_build_and_allows_retry():
    """失败的构建不能残留为不可用缓存项，下一次 acquire 必须重新调用工厂。"""
    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState, RuntimePool

    profile = _profile()
    builds = 0

    async def build(requested: Any) -> AgentRuntime:
        nonlocal builds
        builds += 1
        if builds == 1:
            raise RuntimeError("first build failed")
        return AgentRuntime(profile=requested, graph=object())

    pool = RuntimePool(build)
    with pytest.raises(RuntimeError, match="first build failed"):
        await pool.acquire(profile)
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.MISSING

    lease = await pool.acquire(profile)
    assert builds == 2
    await lease.release()
    await pool.aclose()


async def test_runtime_pool_evicts_lru_idle_runtime_before_building_new_profile():
    """达到容量时只淘汰最久未使用且空闲的 Runtime，随后才允许新 Profile 构建。"""
    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState, RuntimePool

    first_profile = _profile("first")
    second_profile = _profile("second")
    built: list[str] = []

    def build(profile: Any) -> AgentRuntime:
        built.append(profile.profile_key)
        return AgentRuntime(profile=profile, graph=object())

    pool = RuntimePool(build, max_profiles=1)
    first_lease = await pool.acquire(first_profile)
    first_runtime = first_lease.runtime
    await first_lease.release()

    second_lease = await pool.acquire(second_profile)
    assert built == [first_profile.profile_key, second_profile.profile_key]
    assert first_runtime.state == AgentRuntimeState.CLOSED
    assert await pool.state_for(first_profile.profile_key) == AgentRuntimeState.MISSING
    assert await pool.state_for(second_profile.profile_key) == AgentRuntimeState.ACTIVE

    await second_lease.release()
    await pool.aclose()


async def test_runtime_pool_rejects_capacity_when_all_profiles_are_active():
    """容量耗尽但没有安全候选时必须失败，不能淘汰正在运行的图。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        RuntimePool,
        RuntimePoolCapacityError,
    )

    pool = RuntimePool(
        lambda profile: AgentRuntime(profile=profile, graph=object()),
        max_profiles=1,
    )
    first_lease = await pool.acquire(_profile("active"))

    with pytest.raises(RuntimePoolCapacityError, match="RUNTIME_POOL_CAPACITY_EXHAUSTED"):
        await pool.acquire(_profile("blocked"))

    assert first_lease.runtime.graph is not None
    await first_lease.release()
    await pool.aclose()


async def test_runtime_pool_sweep_evicts_only_expired_idle_runtime():
    """TTL sweep 不碰活动 Runtime，空闲到期项关闭后可从持久状态重建。"""
    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState, RuntimePool

    profile = _profile("ttl")
    pool = RuntimePool(
        lambda requested: AgentRuntime(profile=requested, graph=object()),
        idle_ttl_seconds=10,
    )
    lease = await pool.acquire(profile)
    runtime = lease.runtime
    await lease.release()
    snapshot = await runtime.snapshot()

    assert await pool.sweep(now=snapshot.last_used_at + 9.9) == ()
    assert runtime.state == AgentRuntimeState.IDLE
    assert await pool.sweep(now=snapshot.last_used_at + 10) == (profile.profile_key,)
    assert runtime.state == AgentRuntimeState.CLOSED
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.MISSING


async def test_runtime_pool_shutdown_records_close_timeout_and_keeps_closing_task_isolated():
    """Sidecar shutdown 超时只记录失败，不会阻止 Pool 清空其缓存条目。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        RuntimeCloseAdapter,
        RuntimePool,
        RuntimeResourceBundle,
    )

    release = asyncio.Event()

    async def slow_close() -> None:
        await release.wait()

    resources = RuntimeResourceBundle.from_sequences(
        flushers=(RuntimeCloseAdapter("slow", slow_close),),
    )
    profile = _profile("close-timeout")
    pool = RuntimePool(
        lambda requested: AgentRuntime(profile=requested, graph=object(), resources=resources),
        close_timeout_seconds=0.01,
    )
    lease = await pool.acquire(profile)
    await lease.release()

    reports = await pool.aclose()
    assert reports[0].failures[0].resource_name == "runtime_close_timeout"
    assert await pool.size() == 0

    release.set()
    await asyncio.sleep(0)


async def test_draining_runtime_rejects_new_lease_until_old_run_and_lease_release():
    """淘汰中的 Runtime 保留既有 run，但拒绝新租约，空闲后才能真正关闭。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        AgentRuntimeState,
        RuntimePool,
        RuntimeUnavailableError,
    )

    profile = _profile()
    pool = RuntimePool(lambda requested: AgentRuntime(profile=requested, graph=object()))
    lease = await pool.acquire(profile)
    run = await lease.run()

    assert await pool.evict(profile.profile_key, reason="test") is False
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.DRAINING
    with pytest.raises(RuntimeUnavailableError, match="RUNTIME_DRAINING"):
        await pool.acquire(profile)

    await run.release()
    await lease.release()
    assert await pool.finalize_draining(profile.profile_key) is True
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.MISSING


async def test_runtime_close_continues_after_resource_failure_and_is_idempotent():
    """一个资源关闭失败时仍按顺序关闭其余资源，并最终清空图引用。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        AgentRuntimeState,
        RuntimeCloseAdapter,
        RuntimeResourceBundle,
    )

    events: list[str] = []

    async def close(name: str, *, fail: bool = False) -> None:
        events.append(name)
        if fail:
            raise RuntimeError(f"{name} failed")

    resources = RuntimeResourceBundle.from_sequences(
        flushers=(RuntimeCloseAdapter("checkpoint", lambda: close("flush")),),
        tool_resources=(RuntimeCloseAdapter("scheduler", lambda: close("tool", fail=True)),),
        mcp_resources=(RuntimeCloseAdapter("manager", lambda: close("mcp")),),
        sandbox_resources=(RuntimeCloseAdapter("sandbox", lambda: close("sandbox")),),
        model_resources=(RuntimeCloseAdapter("owned-client", lambda: close("model")),),
    )
    runtime = AgentRuntime(profile=_profile(), graph=object(), resources=resources)

    first = await runtime.aclose()
    second = await runtime.aclose()

    assert events == ["flush", "tool", "mcp", "sandbox", "model"]
    assert first == second
    assert first.closed_cleanly is False
    assert first.failures[0].resource_name == "tool:scheduler"
    assert runtime.graph is None
    assert runtime.state == AgentRuntimeState.CLOSED


async def test_runtime_close_cancels_registered_background_tasks():
    """Runtime 关闭必须取消并等待自身创建的后台任务，不能把它们留给进程退出。"""
    from harness_agent.agent_runtime import AgentRuntime

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime = AgentRuntime(profile=_profile(), graph=object())
    task = asyncio.create_task(worker())
    await runtime.register_background_task(task)
    await started.wait()

    await runtime.aclose()

    assert task.cancelled() is True
    assert cancelled.is_set() is True


async def test_pool_removes_runtime_after_close_failure():
    """关闭报告包含失败时，Pool 仍必须删除已 CLOSED 的条目，避免缓存毒化。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        AgentRuntimeState,
        RuntimeCloseAdapter,
        RuntimePool,
        RuntimeResourceBundle,
    )

    profile = _profile()

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    resources = RuntimeResourceBundle.from_sequences(
        tool_resources=(RuntimeCloseAdapter("broken", fail_close),),
    )
    built: list[AgentRuntime] = []

    def build(requested: Any) -> AgentRuntime:
        runtime = AgentRuntime(profile=requested, graph=object(), resources=resources)
        built.append(runtime)
        return runtime

    pool = RuntimePool(build)
    lease = await pool.acquire(profile)
    await lease.release()

    assert await pool.evict(profile.profile_key) is True
    assert built[0].state == AgentRuntimeState.CLOSED
    assert built[0].close_report is not None
    assert await pool.state_for(profile.profile_key) == AgentRuntimeState.MISSING


async def test_runtime_state_transitions_cover_ready_active_idle_draining_and_closed():
    """Runtime 记录完整状态序列，MISSING/BUILDING 由 Pool 条目另行表示。"""
    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState

    runtime = AgentRuntime(profile=_profile(), graph=object())
    lease = await runtime.acquire_lease()
    await lease.release()
    await runtime.begin_draining(reason="test")
    await runtime.aclose()

    assert [transition.state for transition in runtime.transitions] == [
        AgentRuntimeState.READY,
        AgentRuntimeState.ACTIVE,
        AgentRuntimeState.IDLE,
        AgentRuntimeState.DRAINING,
        AgentRuntimeState.CLOSED,
    ]


async def test_runtime_pool_diagnostics_are_bounded_and_redacted():
    """诊断记录命中、构建、淘汰与关闭失败，但不返回完整 Profile Key。"""
    from harness_agent.agent_runtime import (
        AgentRuntime,
        RuntimeCloseAdapter,
        RuntimePool,
        RuntimeResourceBundle,
    )

    first = _profile("diagnostic-first")
    second = _profile("diagnostic-second")

    async def fail_close() -> None:
        raise RuntimeError("expected close failure")

    resources = RuntimeResourceBundle.from_sequences(
        tool_resources=(RuntimeCloseAdapter("broken", fail_close),),
    )
    pool = RuntimePool(
        lambda profile: AgentRuntime(profile=profile, graph=object(), resources=resources),
        max_profiles=1,
    )
    first_lease = await pool.acquire(first)
    await first_lease.release()
    second_lease = await pool.acquire(second)
    await second_lease.release()

    diagnostics = await pool.diagnostics()
    payload = diagnostics.payload()
    assert payload["pool_size"] == 1
    assert payload["metrics"] == {
        "hits": 0,
        "misses": 2,
        "build_successes": 2,
        "build_failures": 0,
        "build_duration_ms_total": pytest.approx(diagnostics.build_duration_ms_total, abs=0.001),
        "capacity_rejections": 0,
        "eviction_reasons": {"lru_capacity": 1},
        "close_reports": 1,
        "close_failures": 1,
        "close_duration_ms_total": pytest.approx(diagnostics.close_duration_ms_total, abs=0.001),
    }
    assert payload["runtimes"][0]["profile_id"] == second.profile_key[:12]
    assert first.profile_key not in str(payload)
    assert second.profile_key not in str(payload)
    assert payload["memory"] == {
        "estimated_bytes": None,
        "rss_bytes": None,
        "status": "not_collected",
    }
    assert payload["recent_events"][-1]["event"] == "build_completed"

    await pool.aclose()


async def test_runtime_pool_pressure_reuses_one_graph_for_1000_threads_without_state_crosstalk():
    """1000 个 mock thread 共享一张图，且消息/epoch/artifact 始终按 thread 独立保存。"""
    from harness_agent.agent_runtime import AgentRuntime, RuntimePool

    profile = _profile("pressure-shared")
    builds = 0
    persisted: dict[str, dict[str, str]] = {}

    def build(requested: Any) -> AgentRuntime:
        nonlocal builds
        builds += 1
        return AgentRuntime(profile=requested, graph=object())

    pool = RuntimePool(build, max_profiles=4)

    async def run_thread(index: int) -> None:
        thread_id = f"thread-{index:04d}"
        # 这些字段模拟 ThreadStore/Checkpointer 的持久状态，而不是 Runtime 图的成员。
        persisted[thread_id] = {
            "message": f"message:{thread_id}",
            "prompt_epoch": f"epoch:{thread_id}",
            "artifact": f"artifact:{thread_id}",
        }
        lease = await pool.acquire(profile)
        run = await lease.run()
        await run.release()
        await lease.release()

    await asyncio.gather(*(run_thread(index) for index in range(1_000)))

    diagnostics = await pool.diagnostics()
    assert builds == 1
    assert diagnostics.pool_size == 1
    assert diagnostics.hits == 999
    assert diagnostics.misses == 1
    assert diagnostics.active_leases == diagnostics.active_runs == diagnostics.queued_runs == 0
    assert len(persisted) == 1_000
    for index in (0, 499, 999):
        thread_id = f"thread-{index:04d}"
        assert persisted[thread_id] == {
            "message": f"message:{thread_id}",
            "prompt_epoch": f"epoch:{thread_id}",
            "artifact": f"artifact:{thread_id}",
        }

    await pool.aclose()


async def test_runtime_pool_pressure_keeps_runtime_count_bounded_across_multiple_profiles():
    """历史 thread 即使覆盖多个 Profile，Pool 也不会超过容量并保留每次淘汰原因。"""
    from harness_agent.agent_runtime import AgentRuntime, RuntimePool

    profiles = tuple(_profile(f"pressure-{index}") for index in range(10))
    builds = 0

    def build(requested: Any) -> AgentRuntime:
        nonlocal builds
        builds += 1
        return AgentRuntime(profile=requested, graph=object())

    pool = RuntimePool(build, max_profiles=3)
    for index in range(1_000):
        lease = await pool.acquire(profiles[index % len(profiles)])
        await lease.release()
        assert await pool.size() <= 3

    diagnostics = await pool.diagnostics()
    assert diagnostics.pool_size <= 3
    assert diagnostics.eviction_reasons["lru_capacity"] == builds - diagnostics.pool_size
    assert diagnostics.close_reports == diagnostics.eviction_reasons["lru_capacity"]
    assert len(diagnostics.recent_events) == 64
    await pool.aclose()


async def test_runtime_pool_restart_diagnostics_do_not_reference_closed_pool():
    """Sidecar 重启后新 Pool 从零开始，不携带旧 Runtime 的事件或短标识。"""
    from harness_agent.agent_runtime import AgentRuntime, RuntimePool

    profile = _profile("restart")
    first = RuntimePool(lambda requested: AgentRuntime(profile=requested, graph=object()))
    lease = await first.acquire(profile)
    await lease.release()
    first_diagnostics = await first.diagnostics()
    await first.aclose()

    second = RuntimePool(lambda requested: AgentRuntime(profile=requested, graph=object()))
    second_diagnostics = await second.diagnostics()
    assert first_diagnostics.runtimes[0].profile_id == profile.profile_key[:12]
    assert second_diagnostics.pool_size == 0
    assert second_diagnostics.recent_events == ()
    assert second_diagnostics.hits == second_diagnostics.misses == 0
    await second.aclose()


async def test_runtime_pool_structured_logs_use_only_short_profile_id(caplog: pytest.LogCaptureFixture):
    """生命周期日志应包含事件、原因、耗时和关闭失败字段，但不能输出完整 Profile Key。"""
    from harness_agent.agent_runtime import AgentRuntime, RuntimePool

    profile = _profile("log-fields")
    caplog.set_level(logging.INFO, logger="harness_agent.agent_runtime")
    pool = RuntimePool(lambda requested: AgentRuntime(profile=requested, graph=object()))
    lease = await pool.acquire(profile)
    await lease.release()

    messages = [record.getMessage() for record in caplog.records if "RuntimePool event=" in record.getMessage()]
    assert any(
        "event=build_completed" in message
        and f"profile={profile.profile_key[:12]}" in message
        and "reason=-" in message
        and "duration_ms=" in message
        and "close_failures=0" in message
        for message in messages
    )
    assert all(profile.profile_key not in message for message in messages)
    await pool.aclose()
