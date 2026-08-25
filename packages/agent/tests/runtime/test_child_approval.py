"""内置 child 审批模式与 HITL 桥。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_delegation import (
    DelegateAgent,
    DelegationCallContext,
    child_execution_ref,
)
from harness_agent.runtime.agent_delegation import _CURRENT_DELEGATION_CALL
from harness_agent.runtime.child_approval import (
    ChildHitlMiddleware,
    bind_child_command,
    reset_child_command,
    task_dispatch_description,
)
from harness_agent.runtime.execution_binding import ExecutionRef
from harness_agent.runtime.interactions import InteractionResult
from harness_agent.runtime.run_context import RunContext
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot


def _context(*, port=None) -> RunContext:
    snapshot = prepare_embedded_context_snapshot(
        thread_id="thread-1",
        system_prompt="parent",
        workspace="/tmp",
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    return RunContext(
        thread_id="thread-1",
        run_id="run-1",
        approval_mode="default",
        context_snapshot=snapshot,
        interaction_port=port,
    )


def _request(name: str, args: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": args or {}, "id": "call-1"},
        runtime=None,
    )


@pytest.mark.asyncio
async def test_auto_edit_write_file_does_not_call_interaction() -> None:
    """父 default 下 GP 内部 auto-edit：普通写文件不弹审批。"""
    called: list[object] = []

    async def port(spec):
        called.append(spec)
        return InteractionResult({"decision": "reject"})

    ctx = _context(port=port)
    token = _CURRENT_DELEGATION_CALL.set(DelegationCallContext(run_context=ctx, tool_call_id="t1"))
    command = DelegateAgent(
        parent_ref=ExecutionRef.root("thread-1", "run-1"),
        target_agent_id="general-purpose",
        task="edit file",
        idempotency_key="k1",
        delegation_policy=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
        cancellation_token=ctx.cancellation_token,
    )
    child_token = bind_child_command(command)
    middleware = ChildHitlMiddleware(agent_id="general-purpose", approval_mode="auto-edit")

    async def handler(_request):
        return ToolMessage(content="wrote", name="write_file", tool_call_id="call-1")

    try:
        result = await middleware.awrap_tool_call(
            _request("write_file", {"file_path": "/src/a.py", "content": "x"}),
            handler,
        )
    finally:
        reset_child_command(child_token)
        _CURRENT_DELEGATION_CALL.reset(token)

    assert result.content == "wrote"
    assert called == []


@pytest.mark.asyncio
async def test_execute_asks_and_reject_returns_tool_message() -> None:
    """execute 必须询问；拒绝回到 child tool result，不抛错。"""
    called: list[object] = []

    async def port(spec):
        called.append(spec)
        return InteractionResult({"decision": "reject"})

    ctx = _context(port=port)
    token = _CURRENT_DELEGATION_CALL.set(DelegationCallContext(run_context=ctx, tool_call_id="t1"))
    command = DelegateAgent(
        parent_ref=ExecutionRef.root("thread-1", "run-1"),
        target_agent_id="general-purpose",
        task="run cmd",
        idempotency_key="k2",
        delegation_policy=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
        cancellation_token=ctx.cancellation_token,
    )
    child_token = bind_child_command(command)
    middleware = ChildHitlMiddleware(agent_id="general-purpose", approval_mode="auto-edit")

    async def handler(_request):
        raise AssertionError("rejected execute must not run")

    try:
        result = await middleware.awrap_tool_call(
            _request("execute", {"command": "rm -rf /tmp/not-safe-enough"}),
            handler,
        )
    finally:
        reset_child_command(child_token)
        _CURRENT_DELEGATION_CALL.reset(token)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "拒绝" in result.content
    assert called[0].agent_id == "general-purpose"
    assert called[0].execution_id == child_execution_ref(command).execution_id
    assert called[0].parent_execution_id == command.parent_ref.execution_id


def test_task_dispatch_description_mentions_auto_edit_for_gp() -> None:
    """GP 派出审批必须写明可改文件、命令仍问。"""
    text = task_dispatch_description(
        {"subagent_type": "general-purpose", "description": "改 README"}
    )
    assert "general-purpose" in text
    assert "改 README" in text
    assert "普通写入过程中不再询问" in text
    assert "执行命令" in text
