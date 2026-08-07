"""Host 共享资源 owner/borrower 生命周期测试。"""

from __future__ import annotations

from harness_agent.runtime.resource_ownership import ResourceScope, SharedResourceOwner


async def test_retired_shared_resource_waits_for_last_engine_borrower() -> None:
    """淘汰一个 generation 不得中断仍持有租约的 AgentEngine。"""
    closed: list[str] = []
    owner = SharedResourceOwner(
        {"name": "mcp-v1"},
        name="mcp-v1",
        scope=ResourceScope.HOST,
        fingerprint="fingerprint-v1",
        close=lambda resource: closed.append(resource["name"]),
    )
    first = await owner.acquire()
    second = await owner.acquire()

    await owner.retire()
    assert closed == []
    assert (await owner.snapshot()).borrowers == 2

    await first.release()
    assert closed == []
    await second.release()
    assert closed == ["mcp-v1"]
    assert (await owner.snapshot()).closed is True


async def test_retired_owner_rejects_new_borrowers() -> None:
    """新 AgentEngine 不能借用已进入 retired 的旧 snapshot。"""
    owner = SharedResourceOwner(
        object(),
        name="snapshot",
        scope=ResourceScope.WORKSPACE,
        fingerprint="snapshot-v1",
        close=lambda _resource: None,
    )
    await owner.retire()

    try:
        await owner.acquire()
    except RuntimeError as exc:
        assert str(exc) == "SHARED_RESOURCE_NOT_ACCEPTING"
    else:  # pragma: no cover - 失败信息更清晰。
        raise AssertionError("retired owner accepted a new borrower")
