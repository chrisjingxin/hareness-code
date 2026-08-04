"""AgentEnginePool 的共享构建、租约状态与资源关闭回归测试。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest


def _profile(name: str = "default"):
    """创建一个字段完整但不含真实路径或模型凭据的测试 Profile。"""
    from harness_agent.runtime.agent_engine_profile import ModelRoleBinding, AgentEngineProfile, component_fingerprint

    def fingerprint(component: str) -> str:
        return component_fingerprint({"test": name, "component": component})

    return AgentEngineProfile(
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


async def test_agent_engine_pool_single_flight_builds_one_engine_for_concurrent_acquires():
    """100 个同 Profile acquire 必须共享一项 BUILDING task 与同一图实例。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState, AgentEnginePool

    profile = _profile()
    release_builder = asyncio.Event()
    builds = 0

    async def build(requested: Any) -> AgentEngine:
        nonlocal builds
        builds += 1
        await release_builder.wait()
        return AgentEngine(profile=requested, graph=object())

    pool = AgentEnginePool(build)
    acquires = [asyncio.create_task(pool.acquire(profile)) for _ in range(100)]
    for _ in range(10):
        await asyncio.sleep(0)
        if builds:
            break
    assert builds == 1
    assert await pool.state_for(profile.profile_key) == AgentEngineState.BUILDING

    release_builder.set()
    leases = await asyncio.gather(*acquires)
    engine = leases[0].engine
    assert all(lease.engine is engine for lease in leases)
    assert await pool.state_for(profile.profile_key) == AgentEngineState.ACTIVE

    await asyncio.gather(*(lease.release() for lease in leases))
    assert await pool.state_for(profile.profile_key) == AgentEngineState.IDLE
    await pool.aclose()


async def test_pool_invalidate_reserves_building_generation_until_stale_build_is_discarded():
    """失效发生在首建期间时不能发布旧图或启动第二个旧 key 构建。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEnginePool,
        AgentEngineState,
        AgentEngineUnavailableError,
    )

    profile = _profile("building-invalidation")
    builder_started = asyncio.Event()
    release_builder = asyncio.Event()
    built: list[AgentEngine] = []

    async def build(requested: Any) -> AgentEngine:
        builder_started.set()
        await release_builder.wait()
        engine = AgentEngine(profile=requested, graph=object())
        built.append(engine)
        return engine

    pool = AgentEnginePool(build)
    acquire_task = asyncio.create_task(pool.acquire(profile))
    await builder_started.wait()

    assert await pool.invalidate(
        lambda candidate: candidate.profile_key == profile.profile_key,
        reason="mcp_snapshot_changed",
    ) == (profile.profile_key,)
    assert await pool.state_for(profile.profile_key) == AgentEngineState.DRAINING

    release_builder.set()
    with pytest.raises(AgentEngineUnavailableError, match="RUNTIME_BUILD_INVALIDATED"):
        await acquire_task

    assert len(built) == 1
    assert built[0].graph is None
    assert await pool.state_for(profile.profile_key) == AgentEngineState.MISSING
    await pool.aclose()


async def test_agent_engine_pool_discards_failed_build_and_allows_retry():
    """失败的构建不能残留为不可用缓存项，下一次 acquire 必须重新调用工厂。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState, AgentEnginePool

    profile = _profile()
    builds = 0

    async def build(requested: Any) -> AgentEngine:
        nonlocal builds
        builds += 1
        if builds == 1:
            raise RuntimeError("first build failed")
        return AgentEngine(profile=requested, graph=object())

    pool = AgentEnginePool(build)
    with pytest.raises(RuntimeError, match="first build failed"):
        await pool.acquire(profile)
    assert await pool.state_for(profile.profile_key) == AgentEngineState.MISSING

    lease = await pool.acquire(profile)
    assert builds == 2
    await lease.release()
    await pool.aclose()


async def test_agent_engine_pool_evicts_lru_idle_engine_before_building_new_profile():
    """达到容量时只淘汰最久未使用且空闲的 AgentEngine，随后才允许新 Profile 构建。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState, AgentEnginePool

    first_profile = _profile("first")
    second_profile = _profile("second")
    built: list[str] = []

    def build(profile: Any) -> AgentEngine:
        built.append(profile.profile_key)
        return AgentEngine(profile=profile, graph=object())

    pool = AgentEnginePool(build, max_profiles=1)
    first_lease = await pool.acquire(first_profile)
    first_engine = first_lease.engine
    await first_lease.release()

    second_lease = await pool.acquire(second_profile)
    assert built == [first_profile.profile_key, second_profile.profile_key]
    assert first_engine.state == AgentEngineState.CLOSED
    assert await pool.state_for(first_profile.profile_key) == AgentEngineState.MISSING
    assert await pool.state_for(second_profile.profile_key) == AgentEngineState.ACTIVE

    await second_lease.release()
    await pool.aclose()


async def test_agent_engine_pool_rejects_capacity_when_all_profiles_are_active():
    """容量耗尽但没有安全候选时必须失败，不能淘汰正在运行的图。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEnginePool,
        AgentEnginePoolCapacityError,
    )

    pool = AgentEnginePool(
        lambda profile: AgentEngine(profile=profile, graph=object()),
        max_profiles=1,
    )
    first_lease = await pool.acquire(_profile("active"))

    with pytest.raises(AgentEnginePoolCapacityError, match="RUNTIME_POOL_CAPACITY_EXHAUSTED"):
        await pool.acquire(_profile("blocked"))

    assert first_lease.engine.graph is not None
    await first_lease.release()
    await pool.aclose()


async def test_agent_engine_pool_sweep_evicts_only_expired_idle_engine():
    """TTL sweep 不碰活动 AgentEngine，空闲到期项关闭后可从持久状态重建。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState, AgentEnginePool

    profile = _profile("ttl")
    pool = AgentEnginePool(
        lambda requested: AgentEngine(profile=requested, graph=object()),
        idle_ttl_seconds=10,
    )
    lease = await pool.acquire(profile)
    engine = lease.engine
    await lease.release()
    snapshot = await engine.snapshot()

    assert await pool.sweep(now=snapshot.last_used_at + 9.9) == ()
    assert engine.state == AgentEngineState.IDLE
    assert await pool.sweep(now=snapshot.last_used_at + 10) == (profile.profile_key,)
    assert engine.state == AgentEngineState.CLOSED
    assert await pool.state_for(profile.profile_key) == AgentEngineState.MISSING


async def test_agent_engine_pool_shutdown_records_close_timeout_and_keeps_closing_task_isolated():
    """Sidecar shutdown 超时只记录失败，不会阻止 Pool 清空其缓存条目。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEngineCloseAdapter,
        AgentEnginePool,
        AgentEngineResourceBundle,
    )

    release = asyncio.Event()

    async def slow_close() -> None:
        await release.wait()

    resources = AgentEngineResourceBundle.from_sequences(
        flushers=(AgentEngineCloseAdapter("slow", slow_close),),
    )
    profile = _profile("close-timeout")
    pool = AgentEnginePool(
        lambda requested: AgentEngine(profile=requested, graph=object(), resources=resources),
        close_timeout_seconds=0.01,
    )
    lease = await pool.acquire(profile)
    await lease.release()

    reports = await pool.aclose()
    assert reports[0].failures[0].resource_name == "runtime_close_timeout"
    assert await pool.size() == 0

    release.set()
    await asyncio.sleep(0)


async def test_draining_engine_rejects_new_lease_until_old_run_and_lease_release():
    """淘汰中的 AgentEngine 保留既有 run，但拒绝新租约，空闲后才能真正关闭。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEngineState,
        AgentEnginePool,
        AgentEngineUnavailableError,
    )

    profile = _profile()
    pool = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    lease = await pool.acquire(profile)
    run = await lease.run()

    assert await pool.evict(profile.profile_key, reason="test") is False
    assert await pool.state_for(profile.profile_key) == AgentEngineState.DRAINING
    with pytest.raises(AgentEngineUnavailableError, match="RUNTIME_DRAINING"):
        await pool.acquire(profile)

    await run.release()
    await lease.release()
    assert await pool.finalize_draining(profile.profile_key) is True
    assert await pool.state_for(profile.profile_key) == AgentEngineState.MISSING


async def test_engine_close_continues_after_resource_failure_and_is_idempotent():
    """一个资源关闭失败时仍按顺序关闭其余资源，并最终清空图引用。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEngineState,
        AgentEngineCloseAdapter,
        AgentEngineResourceBundle,
    )

    events: list[str] = []

    async def close(name: str, *, fail: bool = False) -> None:
        events.append(name)
        if fail:
            raise RuntimeError(f"{name} failed")

    resources = AgentEngineResourceBundle.from_sequences(
        flushers=(AgentEngineCloseAdapter("checkpoint", lambda: close("flush")),),
        tool_resources=(AgentEngineCloseAdapter("scheduler", lambda: close("tool", fail=True)),),
        mcp_resources=(AgentEngineCloseAdapter("manager", lambda: close("mcp")),),
        sandbox_resources=(AgentEngineCloseAdapter("sandbox", lambda: close("sandbox")),),
        model_resources=(AgentEngineCloseAdapter("owned-client", lambda: close("model")),),
    )
    engine = AgentEngine(profile=_profile(), graph=object(), resources=resources)

    first = await engine.aclose()
    second = await engine.aclose()

    assert events == ["flush", "tool", "mcp", "sandbox", "model"]
    assert first == second
    assert first.closed_cleanly is False
    assert first.failures[0].resource_name == "tool:scheduler"
    assert engine.graph is None
    assert engine.state == AgentEngineState.CLOSED


async def test_engine_close_cancels_registered_background_tasks():
    """AgentEngine 关闭必须取消并等待自身创建的后台任务，不能把它们留给进程退出。"""
    from harness_agent.runtime.agent_engine import AgentEngine

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = AgentEngine(profile=_profile(), graph=object())
    task = asyncio.create_task(worker())
    await engine.register_background_task(task)
    await started.wait()

    await engine.aclose()

    assert task.cancelled() is True
    assert cancelled.is_set() is True


async def test_pool_removes_engine_after_close_failure():
    """关闭报告包含失败时，Pool 仍必须删除已 CLOSED 的条目，避免缓存毒化。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEngineState,
        AgentEngineCloseAdapter,
        AgentEnginePool,
        AgentEngineResourceBundle,
    )

    profile = _profile()

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    resources = AgentEngineResourceBundle.from_sequences(
        tool_resources=(AgentEngineCloseAdapter("broken", fail_close),),
    )
    built: list[AgentEngine] = []

    def build(requested: Any) -> AgentEngine:
        engine = AgentEngine(profile=requested, graph=object(), resources=resources)
        built.append(engine)
        return engine

    pool = AgentEnginePool(build)
    lease = await pool.acquire(profile)
    await lease.release()

    assert await pool.evict(profile.profile_key) is True
    assert built[0].state == AgentEngineState.CLOSED
    assert built[0].close_report is not None
    assert await pool.state_for(profile.profile_key) == AgentEngineState.MISSING


async def test_engine_state_transitions_cover_ready_active_idle_draining_and_closed():
    """AgentEngine 记录完整状态序列，MISSING/BUILDING 由 Pool 条目另行表示。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState

    engine = AgentEngine(profile=_profile(), graph=object())
    lease = await engine.acquire_lease()
    await lease.release()
    await engine.begin_draining(reason="test")
    await engine.aclose()

    assert [transition.state for transition in engine.transitions] == [
        AgentEngineState.READY,
        AgentEngineState.ACTIVE,
        AgentEngineState.IDLE,
        AgentEngineState.DRAINING,
        AgentEngineState.CLOSED,
    ]


async def test_agent_engine_pool_diagnostics_are_bounded_and_redacted():
    """诊断记录命中、构建、淘汰与关闭失败，但不返回完整 Profile Key。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEngineCloseAdapter,
        AgentEnginePool,
        AgentEngineResourceBundle,
    )

    first = _profile("diagnostic-first")
    second = _profile("diagnostic-second")

    async def fail_close() -> None:
        raise RuntimeError("expected close failure")

    resources = AgentEngineResourceBundle.from_sequences(
        tool_resources=(AgentEngineCloseAdapter("broken", fail_close),),
    )
    pool = AgentEnginePool(
        lambda profile: AgentEngine(profile=profile, graph=object(), resources=resources),
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
        "resource_scope_counts": {"engine": 1},
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


async def test_agent_engine_pool_pressure_reuses_one_graph_for_1000_threads_without_state_crosstalk():
    """1000 个 mock thread 共享一张图，且消息/epoch/artifact 始终按 thread 独立保存。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool

    profile = _profile("pressure-shared")
    builds = 0
    persisted: dict[str, dict[str, str]] = {}

    def build(requested: Any) -> AgentEngine:
        nonlocal builds
        builds += 1
        return AgentEngine(profile=requested, graph=object())

    pool = AgentEnginePool(build, max_profiles=4)

    async def run_thread(index: int) -> None:
        thread_id = f"thread-{index:04d}"
        # 这些字段模拟 ThreadPersistence/Checkpointer 的持久状态，而不是 AgentEngine 图的成员。
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


async def test_agent_engine_pool_pressure_keeps_engine_count_bounded_across_multiple_profiles():
    """历史 thread 即使覆盖多个 Profile，Pool 也不会超过容量并保留每次淘汰原因。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool

    profiles = tuple(_profile(f"pressure-{index}") for index in range(10))
    builds = 0

    def build(requested: Any) -> AgentEngine:
        nonlocal builds
        builds += 1
        return AgentEngine(profile=requested, graph=object())

    pool = AgentEnginePool(build, max_profiles=3)
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


async def test_agent_engine_pool_restart_diagnostics_do_not_reference_closed_pool():
    """Sidecar 重启后新 Pool 从零开始，不携带旧 AgentEngine 的事件或短标识。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool

    profile = _profile("restart")
    first = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    lease = await first.acquire(profile)
    await lease.release()
    first_diagnostics = await first.diagnostics()
    await first.aclose()

    second = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    second_diagnostics = await second.diagnostics()
    assert first_diagnostics.engines[0].profile_id == profile.profile_key[:12]
    assert second_diagnostics.pool_size == 0
    assert second_diagnostics.recent_events == ()
    assert second_diagnostics.hits == second_diagnostics.misses == 0
    await second.aclose()


async def test_agent_engine_pool_structured_logs_use_only_short_profile_id(caplog: pytest.LogCaptureFixture):
    """生命周期日志应包含事件、原因、耗时和关闭失败字段，但不能输出完整 Profile Key。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool

    profile = _profile("log-fields")
    caplog.set_level(logging.INFO, logger="harness_agent.runtime.agent_engine")
    pool = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    lease = await pool.acquire(profile)
    await lease.release()

    messages = [record.getMessage() for record in caplog.records if "AgentEnginePool event=" in record.getMessage()]
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


async def test_engine_eviction_releases_shared_lease_without_closing_host_resource():
    """淘汰引擎只释放借用；共享资源要等 Host owner 关闭。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineResourceBundle
    from harness_agent.runtime.resource_lifecycle import ResourceScope, SharedResourceHandle

    closed: list[str] = []
    shared = SharedResourceHandle(
        name="test-provider",
        scope=ResourceScope.HOST,
        value=object(),
        close=lambda: closed.append("host"),
    )
    shared_lease = await shared.acquire()
    engine = AgentEngine(
        profile=_profile("shared-resource"),
        graph=object(),
        resources=AgentEngineResourceBundle.from_sequences(shared_leases=(shared_lease,)),
    )

    await engine.aclose()
    snapshot = await shared.snapshot()
    assert snapshot.borrowers == 0
    assert snapshot.state.value == "ready"
    assert closed == []

    await shared.begin_draining(reason="host_shutdown")
    await shared.close()
    assert closed == ["host"]


async def test_pool_invalidate_drains_only_matching_profile_and_rejects_new_acquire():
    """Profile 失效会保留活动引擎收尾，但不让新执行复用 draining 图。"""
    from harness_agent.runtime.agent_engine import (
        AgentEngine,
        AgentEnginePool,
        AgentEngineState,
        AgentEngineUnavailableError,
    )

    first = _profile("invalidate-first")
    second = _profile("invalidate-second")
    pool = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    first_lease = await pool.acquire(first)
    second_lease = await pool.acquire(second)

    await pool.invalidate(
        lambda profile: profile.profile_key == first.profile_key,
        reason="mcp_snapshot_changed",
    )
    assert await pool.state_for(first.profile_key) == AgentEngineState.DRAINING
    assert await pool.state_for(second.profile_key) == AgentEngineState.ACTIVE
    with pytest.raises(AgentEngineUnavailableError, match="RUNTIME_DRAINING"):
        await pool.acquire(first)

    await first_lease.release()
    assert await pool.finalize_draining(first.profile_key) is True
    await second_lease.release()
    diagnostics = await pool.diagnostics()
    assert diagnostics.eviction_reasons == {"mcp_snapshot_changed": 1}
    await pool.aclose()
