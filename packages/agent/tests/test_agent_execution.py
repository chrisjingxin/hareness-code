"""轻量 AgentExecution registry 的生命周期与父子关系测试。"""

from __future__ import annotations

import pytest

from harness_agent.agent_execution import AgentExecutionRegistry, ExecutionRegistryError
from harness_agent.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
)


def _binding(
    execution_id: str,
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    parent_execution_id: str | None = None,
    depth: int = 0,
) -> AgentExecutionBinding:
    """构造不带模型和 Policy 细节的最小执行事实。"""
    return AgentExecutionBinding(
        ref=ExecutionRef(
            thread_id=thread_id,
            run_id=run_id,
            execution_id=execution_id,
            parent_execution_id=parent_execution_id,
        ),
        agent_id="main" if depth == 0 else "general-purpose",
        mode=ExecutionMode.MANAGED if depth == 0 else ExecutionMode.INLINE,
        depth=depth,
    )


@pytest.mark.asyncio
async def test_registry_keeps_identity_and_allows_one_terminal_transition() -> None:
    """执行身份不可变，状态只能从 pending 进入 running 再进入终态。"""
    registry = AgentExecutionRegistry()
    binding = _binding("exec-root")

    await registry.accept(binding)
    await registry.start(binding.ref)
    await registry.finalize(
        binding.ref,
        status=ExecutionStatus.COMPLETED,
        usage={"input_tokens": 3, "output_tokens": 2},
    )

    stored = await registry.get(binding.ref)
    assert stored is not None
    assert stored.ref == binding.ref
    assert stored.agent_id == "main"
    assert stored.mode is ExecutionMode.MANAGED
    assert stored.status is ExecutionStatus.COMPLETED
    assert stored.usage == {"input_tokens": 3, "output_tokens": 2}
    with pytest.raises(TypeError):
        stored.usage["input_tokens"] = 99  # type: ignore[index]

    with pytest.raises(ExecutionRegistryError, match="EXECUTION_ALREADY_TERMINAL"):
        await registry.start(binding.ref)


@pytest.mark.asyncio
async def test_registry_cancels_unfinished_descendants_before_run_seal() -> None:
    """Run 取消必须收敛 root 和所有未终结子 execution。"""
    registry = AgentExecutionRegistry()
    root = _binding("exec-root")
    child = _binding("exec-child", parent_execution_id="exec-root", depth=1)

    await registry.accept(root)
    await registry.accept(child)
    await registry.start(root.ref)
    await registry.start(child.ref)

    await registry.cancel_run(root.ref)
    await registry.seal_run(root.ref)

    assert (await registry.get(root.ref)).status is ExecutionStatus.CANCELLED  # type: ignore[union-attr]
    assert (await registry.get(child.ref)).status is ExecutionStatus.CANCELLED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_registry_rejects_new_execution_after_run_is_sealed() -> None:
    """Run 封口后不能再追加 execution。"""
    registry = AgentExecutionRegistry()
    root = _binding("exec-root")

    await registry.accept(root)
    await registry.start(root.ref)
    await registry.finalize(root.ref, status=ExecutionStatus.COMPLETED)
    await registry.seal_run(root.ref)

    with pytest.raises(ExecutionRegistryError, match="EXECUTION_RUN_SEALED"):
        await registry.accept(_binding("exec-child", parent_execution_id="exec-root", depth=1))


@pytest.mark.asyncio
async def test_registry_scopes_identical_run_ids_by_thread() -> None:
    """不同 Thread 的同名 Run 不共享 execution 状态。"""
    registry = AgentExecutionRegistry()
    first = _binding("exec-first", thread_id="thread-a")
    second = _binding("exec-second", thread_id="thread-b")

    await registry.accept(first)
    await registry.accept(second)
    await registry.start(first.ref)
    await registry.start(second.ref)
    await registry.cancel_run(first.ref)

    assert (await registry.get(first.ref)).status is ExecutionStatus.CANCELLED  # type: ignore[union-attr]
    assert (await registry.get(second.ref)).status is ExecutionStatus.RUNNING  # type: ignore[union-attr]
    assert [binding.ref.execution_id for binding in await registry.list(first.ref)] == [
        "exec-first"
    ]
    assert [binding.ref.execution_id for binding in await registry.list(second.ref)] == [
        "exec-second"
    ]


@pytest.mark.asyncio
async def test_registry_rejects_conflicting_parent_and_identity() -> None:
    """子 execution 必须紧接父深度，冲突身份不能覆盖已有记录。"""
    registry = AgentExecutionRegistry()
    root = _binding("exec-root")
    await registry.accept(root)

    with pytest.raises(ExecutionRegistryError, match="EXECUTION_PARENT_NOT_FOUND"):
        await registry.accept(
            _binding("exec-orphan", parent_execution_id="missing", depth=1)
        )
    with pytest.raises(ExecutionRegistryError, match="EXECUTION_DEPTH_INVALID"):
        await registry.accept(
            _binding("exec-grandchild", parent_execution_id="exec-root", depth=2)
        )
    with pytest.raises(ExecutionRegistryError, match="EXECUTION_ID_CONFLICT"):
        await registry.accept(
            AgentExecutionBinding(
                ref=root.ref,
                agent_id="other",
                mode=ExecutionMode.MANAGED,
                depth=0,
            )
        )
