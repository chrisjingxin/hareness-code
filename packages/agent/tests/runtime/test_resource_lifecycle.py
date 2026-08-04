"""Host/workspace 共享资源租约和排空原语测试。"""

from __future__ import annotations

from pathlib import Path

import pytest


async def test_shared_resource_rejects_new_leases_after_draining() -> None:
    """配置失效只停止新借用，已有借用释放后 owner 才能关闭。"""
    from harness_agent.runtime.resource_lifecycle import (
        ResourceScope,
        SharedResourceBusyError,
        SharedResourceHandle,
        SharedResourceUnavailableError,
    )

    closed: list[str] = []
    resource = SharedResourceHandle(
        name="workspace",
        scope=ResourceScope.WORKSPACE,
        value=object(),
        close=lambda: closed.append("closed"),
    )
    lease = await resource.acquire()
    await resource.begin_draining(reason="sandbox_config_changed")
    with pytest.raises(SharedResourceUnavailableError, match="SHARED_RESOURCE_DRAINING"):
        await resource.acquire()
    with pytest.raises(SharedResourceBusyError, match="SHARED_RESOURCE_HAS_BORROWERS"):
        await resource.close()

    await lease.release()
    report = await resource.close()
    assert report.failures == ()
    assert closed == ["closed"]


async def test_shared_resource_close_is_idempotent() -> None:
    """Host close 的重复调用不会重复关闭底层 transport。"""
    from harness_agent.runtime.resource_lifecycle import ResourceScope, SharedResourceHandle

    calls = 0

    def close() -> None:
        nonlocal calls
        calls += 1

    resource = SharedResourceHandle(
        name="provider",
        scope=ResourceScope.HOST,
        value=object(),
        close=close,
    )
    first = await resource.close()
    second = await resource.close()
    assert first is second
    assert calls == 1


async def test_workspace_execution_pool_shares_backend_and_releases_after_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 workspace 身份共享 backend，配置失效不提前关闭活动借用。"""
    from types import SimpleNamespace

    from harness_agent.config.config import ExecutionSettings
    from harness_agent.runtime.execution import ExecutionContext, WorkspaceExecutionResourcePool

    closed: list[str] = []

    def create(_settings: object, workspace: Path) -> ExecutionContext:
        return ExecutionContext(
            backend=SimpleNamespace(aclose=lambda: closed.append("sandbox")),
            mode="local",
            workspace_path=str(workspace),
            provider=None,
        )

    monkeypatch.setattr("harness_agent.runtime.execution.create_execution_context", create)
    pool = WorkspaceExecutionResourcePool()
    settings = ExecutionSettings()
    first = await pool.acquire("sandbox-a", settings, tmp_path)
    second = await pool.acquire("sandbox-a", settings, tmp_path)
    assert first.value is second.value

    await pool.invalidate("sandbox-a", reason="sandbox_config_changed")
    await first.release()
    await second.release()
    await pool.reap()
    assert closed == ["sandbox"]
    await pool.aclose()


async def test_workspace_pool_keeps_draining_generation_when_same_key_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 key 重租新 backend 不会覆盖仍有 borrower 的旧 generation。"""
    from types import SimpleNamespace

    from harness_agent.config.config import ExecutionSettings
    from harness_agent.runtime.execution import ExecutionContext, WorkspaceExecutionResourcePool

    created: list[str] = []
    closed: list[str] = []

    def create(_settings: object, workspace: Path) -> ExecutionContext:
        generation = f"sandbox-{len(created) + 1}"
        created.append(generation)
        return ExecutionContext(
            backend=SimpleNamespace(aclose=lambda generation=generation: closed.append(generation)),
            mode="local",
            workspace_path=str(workspace),
            provider=None,
        )

    monkeypatch.setattr("harness_agent.runtime.execution.create_execution_context", create)
    pool = WorkspaceExecutionResourcePool()
    settings = ExecutionSettings()

    old_lease = await pool.acquire("sandbox-generation", settings, tmp_path)
    await pool.invalidate("sandbox-generation", reason="sandbox_config_changed")
    new_lease = await pool.acquire("sandbox-generation", settings, tmp_path)

    assert created == ["sandbox-1", "sandbox-2"]
    assert old_lease.value is not new_lease.value

    await old_lease.release()
    await pool.reap()
    assert closed == ["sandbox-1"]

    await new_lease.release()
    await pool.reap()
    assert closed == ["sandbox-1"]
    await pool.aclose()
