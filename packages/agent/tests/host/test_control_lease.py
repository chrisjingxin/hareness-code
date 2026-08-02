"""ControlLease 的 holder 转移、permit 线性化与 revoke 收敛测试。"""

from __future__ import annotations

import asyncio

import pytest

from harness_agent.host.control_lease import (
    ActivityFacts,
    ControlLease,
    ControlLeaseError,
)


def _no_activity() -> ActivityFacts:
    return ActivityFacts()


@pytest.mark.asyncio
async def test_initial_holder_is_owner() -> None:
    """Host 启动后 holder 固定为 owner Connection。"""
    lease = ControlLease("owner-1")
    status = lease.status()
    assert status.state == "owner"
    assert status.holder.connection_id == "owner-1"
    assert status.holder.role == "owner"
    assert status.holder.attachment_id is None


@pytest.mark.asyncio
async def test_attached_can_acquire_and_release_control() -> None:
    """有效 attached Connection 可 acquire，release 后原子归还 owner。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    status = await lease.acquire("web-1", "att-1", _no_activity())
    assert status.state == "attached"
    assert status.holder.connection_id == "web-1"
    assert status.holder.role == "attached"
    assert status.holder.attachment_id == "att-1"

    released = await lease.release("web-1", _no_activity())
    assert released.state == "owner"
    assert released.holder.connection_id == "owner-1"
    assert released.holder.attachment_id is None


@pytest.mark.asyncio
async def test_non_holder_operations_are_rejected() -> None:
    """非 holder 的 permit/acquire/release 都返回统一控制权错误。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    await lease.register_attachment("att-2", "web-2")

    with pytest.raises(ControlLeaseError) as before_acquire:
        async with lease.permit("web-1"):
            pass
    assert before_acquire.value.code == "CONTROL_NOT_HOLDER"

    await lease.acquire("web-1", "att-1", _no_activity())
    with pytest.raises(ControlLeaseError) as other_acquire:
        await lease.acquire("web-2", "att-2", _no_activity())
    assert other_acquire.value.code == "CONTROL_BUSY"
    with pytest.raises(ControlLeaseError) as owner_permit:
        async with lease.permit("owner-1"):
            pass
    assert owner_permit.value.code == "CONTROL_NOT_HOLDER"
    with pytest.raises(ControlLeaseError) as other_release:
        await lease.release("web-2", _no_activity())
    assert other_release.value.code == "CONTROL_NOT_HOLDER"


@pytest.mark.asyncio
async def test_acquire_and_owner_permit_contend_at_barrier() -> None:
    """acquire 与 owner 受控操作同时竞争时只受理一方，无 TOCTOU 窗口。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    barrier = asyncio.Barrier(2)
    outcomes: list[str] = []

    async def owner_side() -> None:
        await barrier.wait()
        try:
            async with lease.permit("owner-1"):
                outcomes.append("owner-permit")
                await asyncio.sleep(0.05)
        except ControlLeaseError as exc:
            outcomes.append(exc.code)

    async def web_side() -> None:
        await barrier.wait()
        try:
            status = await lease.acquire("web-1", "att-1", _no_activity())
            outcomes.append(status.state)
        except ControlLeaseError as exc:
            outcomes.append(exc.code)

    await asyncio.gather(owner_side(), web_side())
    assert sorted(outcomes) == sorted(
        ["owner-permit", "CONTROL_BUSY"]
    ) or sorted(outcomes) == sorted(["attached", "CONTROL_NOT_HOLDER"])
    assert len(outcomes) == 2


@pytest.mark.asyncio
async def test_release_is_blocked_by_permit_runs_and_interactions() -> None:
    """permit、starting/active Run 与 pending Interaction 分别阻止 release。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    await lease.acquire("web-1", "att-1", _no_activity())

    async with lease.permit("web-1"):
        with pytest.raises(ControlLeaseError) as blocked:
            await lease.release("web-1", _no_activity())
        assert blocked.value.code == "CONTROL_RELEASE_BLOCKED"

    with pytest.raises(ControlLeaseError) as runs:
        await lease.release(
            "web-1",
            ActivityFacts(starting_or_active_runs=1),
        )
    assert runs.value.code == "CONTROL_RELEASE_BLOCKED"

    with pytest.raises(ControlLeaseError) as interactions:
        await lease.release(
            "web-1",
            ActivityFacts(pending_interactions=1),
        )
    assert interactions.value.code == "CONTROL_RELEASE_BLOCKED"
    assert lease.status().state == "attached"


@pytest.mark.asyncio
async def test_owner_activity_blocks_acquire() -> None:
    """owner 有 Run 或未收敛 Interaction 时 acquire 返回 CONTROL_BUSY。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    with pytest.raises(ControlLeaseError) as runs:
        await lease.acquire(
            "web-1",
            "att-1",
            ActivityFacts(starting_or_active_runs=1),
        )
    assert runs.value.code == "CONTROL_BUSY"
    with pytest.raises(ControlLeaseError) as interactions:
        await lease.acquire(
            "web-1",
            "att-1",
            ActivityFacts(pending_interactions=1),
        )
    assert interactions.value.code == "CONTROL_BUSY"
    assert lease.status().state == "owner"


@pytest.mark.asyncio
async def test_repeated_acquire_and_revoke_are_idempotent() -> None:
    """holder 重复 acquire 幂等；重复 revoke 不产生第二次状态转换。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    first = await lease.acquire("web-1", "att-1", _no_activity())
    second = await lease.acquire("web-1", "att-1", _no_activity())
    assert first == second
    assert lease.status().state == "attached"

    assert await lease.begin_revoke("att-1") == "web-1"
    assert lease.status().state == "revoking"
    assert await lease.begin_revoke("att-1") == "web-1"
    await lease.complete_revoke("att-1")
    assert lease.status().state == "owner"
    await lease.complete_revoke("att-1")
    assert lease.status().state == "owner"


@pytest.mark.asyncio
async def test_revoked_attachment_cannot_acquire_or_register() -> None:
    """已撤销 attachment 的 acquire 与二次注册都被拒绝。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    await lease.begin_revoke("att-1")
    with pytest.raises(ControlLeaseError) as inactive:
        await lease.acquire("web-1", "att-1", _no_activity())
    assert inactive.value.code == "ATTACHMENT_NOT_ACTIVE"
    assert await lease.register_attachment("att-1", "web-1") is False


@pytest.mark.asyncio
async def test_revoking_rejects_new_permits_until_owner_restored() -> None:
    """revoking 期间 holder 与 owner 的新 permit 都被拒绝。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    await lease.acquire("web-1", "att-1", _no_activity())
    await lease.begin_revoke("att-1")
    assert lease.status().state == "revoking"
    with pytest.raises(ControlLeaseError) as denied_holder:
        async with lease.permit("web-1"):
            pass
    assert denied_holder.value.code == "CONTROL_NOT_HOLDER"
    with pytest.raises(ControlLeaseError) as denied_owner:
        async with lease.permit("owner-1"):
            pass
    assert denied_owner.value.code == "CONTROL_NOT_HOLDER"

    await lease.complete_revoke("att-1")
    assert lease.status().state == "owner"
    async with lease.permit("owner-1"):
        pass


@pytest.mark.asyncio
async def test_disconnect_restores_owner_after_convergence() -> None:
    """断线先进入 revoking，收敛完成后恢复 owner；未知连接无副作用。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "web-1")
    await lease.acquire("web-1", "att-1", _no_activity())
    attachment_id = await lease.connection_disconnected("web-1")
    assert attachment_id == "att-1"
    assert lease.status().state == "revoking"
    await lease.complete_revoke(attachment_id)
    assert lease.status().state == "owner"
    assert lease.status().holder.connection_id == "owner-1"

    assert await lease.connection_disconnected("unknown-connection") is None


@pytest.mark.asyncio
async def test_acquire_requires_matching_registered_attachment() -> None:
    """未登记或连接不匹配的 attachment 不能 acquire。"""
    lease = ControlLease("owner-1")
    with pytest.raises(ControlLeaseError) as missing:
        await lease.acquire("web-1", "att-missing", _no_activity())
    assert missing.value.code == "ATTACHMENT_NOT_ACTIVE"

    await lease.register_attachment("att-1", "web-1")
    with pytest.raises(ControlLeaseError) as mismatch:
        await lease.acquire("web-2", "att-1", _no_activity())
    assert mismatch.value.code == "ATTACHMENT_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_owner_cannot_acquire() -> None:
    """owner 已是 holder，acquire 返回 CONTROL_BUSY。"""
    lease = ControlLease("owner-1")
    await lease.register_attachment("att-1", "owner-1")
    with pytest.raises(ControlLeaseError) as busy:
        await lease.acquire("owner-1", "att-1", _no_activity())
    assert busy.value.code == "CONTROL_BUSY"
