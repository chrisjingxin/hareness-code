"""Qwen SubagentStop 终态门禁与 child Interaction 的离线契约测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from harness_agent.plugins.runtime import (
    HookDefinition,
    HookResult,
    HookRunner,
    SubagentStopController,
    SubagentStopError,
    SubagentStopRequest,
)
from harness_agent.host.run_coordinator import ConnectionRef, RunCoordinator, RunRef
from harness_agent.runtime.interactions import (
    ChildInteractionRegistry,
    InteractionRequest,
    InteractionResult,
)
from harness_agent.runtime.managed_agent_executor import (
    FinalOutputGateDecision,
    ManagedFinalOutput,
    ManagedAgentExecutor,
    ManagedAgentRequest,
)


@dataclass
class _FakeHookRunner:
    results: list[HookResult]

    def __post_init__(self) -> None:
        self.payloads: list[Mapping[str, object]] = []
        self.plugin_ids: list[str | None] = []

    async def run(
        self,
        _event: str,
        *,
        tool_name: str,
        payload: Mapping[str, object],
        plugin_id: str | None = None,
    ) -> tuple[HookResult, ...]:
        assert tool_name == "za38-frontend-executor"
        self.payloads.append(dict(payload))
        self.plugin_ids.append(plugin_id)
        if self.results:
            return (self.results.pop(0),)
        return (HookResult(0, document={"decision": "allow"}),)


def _request(*, stop_hook_active: bool = False) -> SubagentStopRequest:
    return SubagentStopRequest(
        plugin_id="plugin-za38",
        agent_id="za38-frontend-executor",
        agent_type="qwen-code",
        last_output="完成了只读检查。",
        workspace="/.harness/workspace",
        stop_hook_active=stop_hook_active,
        execution_id="child-1",
        parent_execution_id="root-1",
        checkpoint_namespace="fp:thread:run:child-1",
    )


@pytest.mark.asyncio
async def test_subagent_stop_allow_uses_bounded_virtual_input() -> None:
    """allow 只把有界正文和虚拟工作区交给 Hook，不泄露宿主路径。"""
    runner = _FakeHookRunner([HookResult(0, document={"decision": "allow"})])

    async def interaction(_request: InteractionRequest) -> InteractionResult:
        raise AssertionError("allow 不应请求用户交互")

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert runner.payloads[0] == {
        "agent_id": "za38-frontend-executor",
        "agent_type": "qwen-code",
        "last_output": "完成了只读检查。",
        "cwd": "/.harness/workspace",
        "workspace": "/.harness/workspace",
        "stop_hook_active": False,
    }
    assert "/Users/" not in repr(runner.payloads[0])


@pytest.mark.asyncio
async def test_subagent_stop_empty_json_is_allow() -> None:
    """合法的空 JSON Hook 输出与空 stdout 一样表示没有阻断。"""
    runner = _FakeHookRunner([HookResult(0, stdout="{}")])
    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"


@pytest.mark.asyncio
async def test_subagent_stop_block_continue_preserves_child_provenance() -> None:
    """阻断后选择继续修改返回同一 child checkpoint 的 retry 输入。"""
    runner = _FakeHookRunner(
        [
            HookResult(
                0,
                document={
                    "decision": "block",
                    "reason": "请先完成提交门禁。",
                    "hookSpecificOutput": {
                        "hookEventName": "SubagentStop",
                        "additionalContext": "这是不可信的插件补充信息。",
                    },
                },
            ),
            HookResult(0, document={"decision": "allow"}),
        ]
    )
    requests: list[InteractionRequest] = []

    async def interaction(request: InteractionRequest) -> InteractionResult:
        requests.append(request)
        return InteractionResult({"answers": {"question-1": ["continue"]}})

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    first = await controller.evaluate(_request())
    second = await controller.evaluate(_request(stop_hook_active=True))

    assert first.action == "continue"
    assert "请先完成提交门禁" in first.continuation_prompt
    assert "不可信的插件补充信息" in first.continuation_prompt
    assert second.action == "allow"
    assert len(requests) == 1
    assert requests[0].execution_id == "child-1"
    assert requests[0].parent_execution_id == "root-1"
    assert requests[0].agent_id == "za38-frontend-executor"
    assert requests[0].serial_context == {
        "kind": "subagent_stop",
        "checkpoint_namespace": "fp:thread:run:child-1",
        "reason": "请先完成提交门禁。",
        "additional_context": "这是不可信的插件补充信息。",
    }


@pytest.mark.asyncio
async def test_subagent_stop_submit_continues_but_skip_only_releases_gate() -> None:
    """submit 要求同一 child 继续执行提交门禁，skip 才只放行当前 gate。"""
    for answer, expected_action in (("submit", "continue"), ("skip", "allow")):
        runner = _FakeHookRunner(
            [HookResult(0, document={"decision": "block", "reason": "检查点"})]
        )

        async def interaction(_request: InteractionRequest, *, value: str = answer) -> InteractionResult:
            return InteractionResult({"answers": {"question-1": [value]}})

        controller = SubagentStopController(
            hook_runner=runner.run,
            interaction_port=interaction,
        )
        result = await controller.evaluate(_request())

        assert result.action == expected_action
        assert result.skip_once is (answer == "skip")
        if answer == "submit":
            assert "提交门禁" in result.continuation_prompt


@pytest.mark.asyncio
async def test_subagent_stop_rejects_malformed_hook_and_expired_interaction() -> None:
    """坏输出、无交互客户端均不能把 child 放行。"""
    for hook_result in (
        HookResult(0, document={"unexpected": True}),
        HookResult(0, stdout="not-json"),
        HookResult(1, stderr="offline hook failed"),
        HookResult(0, timed_out=True),
    ):
        runner = _FakeHookRunner([hook_result])

        async def no_interaction(_request: InteractionRequest) -> InteractionResult:
            return InteractionResult({"answers": {}}, expired=True)

        controller = SubagentStopController(
            hook_runner=runner.run,
            interaction_port=no_interaction,
        )
        with pytest.raises(SubagentStopError) as error:
            await controller.evaluate(_request())
        assert error.value.code in {
            "SUBAGENT_STOP_HOOK_INVALID",
            "SUBAGENT_STOP_HOOK_FAILED",
            "SUBAGENT_STOP_HOOK_TIMEOUT",
        }


@pytest.mark.asyncio
async def test_subagent_stop_matched_empty_and_closed_runner_fail_closed() -> None:
    """已确认命中的 gate 没有同步裁决时不能伪装成无匹配并放行。"""
    closed_runner = HookRunner(())
    await closed_runner.aclose()
    try:
        async def empty_runner(
            _event: str,
            **_kwargs: object,
        ) -> tuple[HookResult, ...]:
            return ()

        for hook_runner in (empty_runner, closed_runner.run):
            controller = SubagentStopController(
                hook_runner=hook_runner,
                interaction_port=lambda _request: asyncio.sleep(0),
            )
            with pytest.raises(SubagentStopError) as error:
                await controller.evaluate(_request())
            assert error.value.code == "SUBAGENT_STOP_HOOK_NO_RESULT"
    finally:
        await closed_runner.aclose()


@pytest.mark.asyncio
async def test_subagent_stop_matcher_filters_before_hook_invocation() -> None:
    """SubagentStop 只运行 matcher 命中的同一 Qwen source Hook。"""
    root = Path("/.harness/plugins/plugin-za38")
    matching = HookDefinition(
        plugin_id="plugin-za38",
        source_id="plugin-za38",
        event="SubagentStop",
        matcher="^za38-frontend-executor$",
        command="mock",
        args=(),
        timeout_seconds=1,
        asynchronous=False,
        shell=None,
        root=root,
        data=root / "data",
        workspace=Path("/.harness/workspace"),
    )
    non_matching = replace(matching, matcher="^za38-backend-executor$")
    runner = HookRunner((matching, non_matching))
    invoked: list[str] = []

    async def fake_invoke(definition: HookDefinition, _payload: Mapping[str, object]) -> HookResult:
        invoked.append(definition.matcher)
        return HookResult(0, document={"decision": "allow"})

    runner._invoke = fake_invoke  # type: ignore[method-assign]
    try:
        results = await runner.run(
            "SubagentStop",
            tool_name="za38-frontend-executor",
            plugin_id="plugin-za38",
            payload={"agent_id": "za38-frontend-executor"},
        )
    finally:
        await runner.aclose()

    assert len(results) == 1
    assert invoked == ["^za38-frontend-executor$"]

    invoked.clear()
    results = await runner.run(
        "SubagentStop",
        tool_name="za38-java-executor",
        plugin_id="plugin-za38",
        payload={"agent_id": "za38-java-executor"},
    )
    assert results == ()
    assert invoked == []


@pytest.mark.asyncio
async def test_subagent_stop_rejects_invalid_and_cancelled_interaction() -> None:
    """无效答案和取消不能把 SubagentStop 门禁默认放行。"""
    runner = _FakeHookRunner(
        [HookResult(0, document={"decision": "block", "reason": "需要复核"})]
    )

    async def invalid_interaction(_request: InteractionRequest) -> InteractionResult:
        return InteractionResult({"answers": {"question-1": ["unknown"]}})

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=invalid_interaction,
    )
    with pytest.raises(SubagentStopError) as invalid:
        await controller.evaluate(_request())
    assert invalid.value.code == "SUBAGENT_STOP_INTERACTION_INVALID"

    async def malformed_result(_request: InteractionRequest) -> InteractionResult:
        return object()  # type: ignore[return-value]

    controller = SubagentStopController(
        hook_runner=_FakeHookRunner(
            [HookResult(0, document={"decision": "block", "reason": "需要复核"})]
        ).run,
        interaction_port=malformed_result,
    )
    with pytest.raises(SubagentStopError) as malformed:
        await controller.evaluate(_request())
    assert malformed.value.code == "SUBAGENT_STOP_INTERACTION_INVALID"

    async def multiple_answers(_request: InteractionRequest) -> InteractionResult:
        return InteractionResult(
            {"answers": {"question-1": ["submit", "skip"]}},
        )

    controller = SubagentStopController(
        hook_runner=_FakeHookRunner(
            [HookResult(0, document={"decision": "block", "reason": "需要复核"})]
        ).run,
        interaction_port=multiple_answers,
    )
    with pytest.raises(SubagentStopError) as multiple:
        await controller.evaluate(_request())
    assert multiple.value.code == "SUBAGENT_STOP_INTERACTION_INVALID"

    async def cancelled_interaction(_request: InteractionRequest) -> InteractionResult:
        raise asyncio.CancelledError

    controller = SubagentStopController(
        hook_runner=_FakeHookRunner(
            [HookResult(0, document={"decision": "block", "reason": "需要复核"})]
        ).run,
        interaction_port=cancelled_interaction,
    )
    with pytest.raises(SubagentStopError) as cancelled:
        await controller.evaluate(_request())
    assert cancelled.value.code == "SUBAGENT_STOP_INTERACTION_CANCELLED"


@pytest.mark.asyncio
async def test_subagent_stop_disabled_or_untrusted_never_calls_hook() -> None:
    """disabled/untrusted Plugin 不执行 Hook，且保持 fail-closed 目录行为。"""
    runner = _FakeHookRunner([HookResult(0, document={"decision": "block"})])

    async def interaction(_request: InteractionRequest) -> InteractionResult:
        raise AssertionError("未启用 Hook 不应请求交互")

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    for request in (
        replace(_request(), enabled=False, trusted=False),
        replace(_request(), enabled=True, trusted=False),
    ):
        result = await controller.evaluate(request)
        assert result.action == "allow"
    assert runner.payloads == []


@pytest.mark.asyncio
async def test_subagent_stop_allows_eight_retries_but_ninth_block_fails_closed() -> None:
    """连续八次阻断可继续同一 child，第九次阻断稳定失败关闭。"""
    runner = _FakeHookRunner(
        [HookResult(0, document={"decision": "block", "reason": "仍未提交"})] * 9
    )

    async def interaction(_request: InteractionRequest) -> InteractionResult:
        return InteractionResult({"answers": {"question-1": ["continue"]}})

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    for _ in range(8):
        result = await controller.evaluate(_request())
        assert result.action == "continue"
    with pytest.raises(SubagentStopError) as error:
        await controller.evaluate(_request())
    assert error.value.code == "SUBAGENT_STOP_BLOCK_LIMIT"


def test_child_interaction_registry_is_run_scoped_and_cleanup_is_idempotent() -> None:
    """交互记录带 child provenance，Run 结束/断连清理不残留。"""
    registry = ChildInteractionRegistry()
    registry.register(
        request_id="stop-1",
        run_id="run-1",
        execution_id="child-1",
        parent_execution_id="root-1",
        agent_id="za38-frontend-executor",
        checkpoint_namespace="fp:thread:run:child-1",
    )
    item = registry.get("stop-1")
    assert item is not None
    assert item.execution_id == "child-1"
    assert item.parent_execution_id == "root-1"
    assert registry.cancel_run("run-1") == ("stop-1",)
    assert registry.get("stop-1") is None
    assert registry.cancel_run("run-1") == ()


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def astream(self, stream_input: object, **_kwargs: Any):
        self.calls.append(stream_input)
        text = "first output" if len(self.calls) == 1 else "final output"
        yield ("messages", (AIMessageChunk(content=text), {}))


@dataclass
class _Runtime:
    agent: object
    released: int = 0

    run_context: object | None = None

    def graph_config(self, namespace: str) -> Mapping[str, object]:
        return {"configurable": {"thread_id": namespace}}

    async def release(self) -> None:
        self.released += 1


class _Observer:
    def on_model_round(self) -> None:
        return None

    async def on_execution_complete(self, _result: object) -> None:
        return None

    def emit(self, _signal: object) -> None:
        return None

    async def interact(self, _request: object) -> object:
        raise AssertionError("测试 graph 不应产生交互")

    async def observe_message(self, _chunk: object, _session: object) -> bool:
        return False

    async def after_tool_boundary(self) -> None:
        return None

    def on_stream_event(self) -> None:
        return None


@pytest.mark.asyncio
async def test_managed_executor_retries_same_child_checkpoint_after_gate_continue() -> None:
    """终态 gate 的 continue 在同一次 Managed execution 内重跑 checkpoint。"""
    agent = _FakeAgent()
    runtime = _Runtime(agent)
    decisions = [
        FinalOutputGateDecision(action="continue", continuation_prompt="请完成提交门禁"),
        FinalOutputGateDecision(action="allow"),
    ]
    seen: list[str] = []

    async def gate(_final: object) -> FinalOutputGateDecision:
        seen.append("child-1")
        return decisions.pop(0)

    request = ManagedAgentRequest(
        execution_ref="child-1",
        parent_execution_ref="root-1",
        run_id="run-1",
        input="执行只读任务",
        checkpoint_namespace="fp:thread:run:child-1",
        output_policy="capture_only",
        runtime_provider=lambda: _ready(runtime),
        is_cancelled=lambda: False,
        idempotency_key="delegate-1",
        final_output_gate=gate,
    )
    result = await ManagedAgentExecutor().execute(request, _Observer())

    assert result.final_content == "final output"
    assert len(agent.calls) == 2
    assert seen == ["child-1", "child-1"]
    assert runtime.released == 1


@pytest.mark.asyncio
async def test_submit_continues_same_child_and_runs_subagent_stop_again() -> None:
    """submit 只注入提交指令，第二回合仍经同一 SubagentStop gate。"""
    agent = _FakeAgent()
    runtime = _Runtime(agent)
    runner = _FakeHookRunner(
        [
            HookResult(0, document={"decision": "block", "reason": "请提交"}),
            HookResult(0, document={"decision": "allow"}),
        ]
    )
    interactions: list[InteractionRequest] = []

    async def interaction(request: InteractionRequest) -> InteractionResult:
        interactions.append(request)
        return InteractionResult({"answers": {"question-1": ["submit"]}})

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    seen: list[ManagedFinalOutput] = []

    async def gate(final: ManagedFinalOutput) -> FinalOutputGateDecision:
        seen.append(final)
        return await controller.evaluate(
            replace(
                _request(),
                last_output=final.final_content,
                stop_hook_active=controller.block_count > 0,
            )
        )

    request = ManagedAgentRequest(
        execution_ref="child-1",
        parent_execution_ref="root-1",
        run_id="run-1",
        input="执行只读任务",
        checkpoint_namespace="fp:thread:run:child-1",
        output_policy="capture_only",
        runtime_provider=lambda: _ready(runtime),
        is_cancelled=lambda: False,
        idempotency_key="delegate-submit-1",
        final_output_gate=gate,
    )
    result = await ManagedAgentExecutor().execute(request, _Observer())

    assert result.final_content == "final output"
    assert len(agent.calls) == 2
    assert len(seen) == 2
    assert all(item.execution_ref == "child-1" for item in seen)
    assert all(item.parent_execution_ref == "root-1" for item in seen)
    assert all(item.run_id == "run-1" for item in seen)
    assert len(interactions) == 1
    assert "用户已选择" in repr(agent.calls[1])
    assert runner.payloads[0]["stop_hook_active"] is False
    assert runner.payloads[1]["stop_hook_active"] is True
    assert runtime.released == 1


def test_final_output_gate_rejects_unknown_or_mixed_decisions() -> None:
    """终态 gate 的未知动作和 continue+skip 组合必须 fail closed。"""
    with pytest.raises(ValueError, match="MANAGED_FINAL_GATE_ACTION_INVALID"):
        FinalOutputGateDecision(action="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="MANAGED_FINAL_GATE_SKIP_INVALID"):
        FinalOutputGateDecision(
            action="continue",
            continuation_prompt="继续",
            skip_once=True,
        )


@pytest.mark.asyncio
async def test_run_coordinator_routes_child_question_through_owner_and_cleans_registry() -> None:
    """Host Coordinator 的 child question 保留 provenance 并在响应后清理。"""
    async def no_persistence() -> None:
        return None

    async def no_preparation(*_args: object) -> None:
        return None

    async def no_runtime(*_args: object) -> None:
        return None

    class _Port:
        async def request(
            self,
            owner: ConnectionRef,
            run: RunRef,
            interaction: InteractionRequest,
        ) -> InteractionResult:
            assert owner.connection_id == "owner"
            assert run.run_id == "run-1"
            assert interaction.execution_id == "child-1"
            return InteractionResult({"answers": {"question-1": ["submit"]}})

    coordinator = RunCoordinator(
        persistence_provider=no_persistence,
        preparation_provider=no_preparation,
        runtime_provider=no_runtime,
        interaction_port=_Port(),
    )
    coordinator._emit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    run = type(
        "ActiveRun",
        (),
        {
            "ref": RunRef("thread-1", "run-1"),
            "owner": ConnectionRef("owner"),
            "completion": None,
            "cancel_requested": False,
            "status": "running",
        },
    )()
    coordinator._runs["thread-1"] = run
    interaction = InteractionRequest(
        request_id="stop-1",
        type="question",
        payload={"interrupt_id": "stop-1", "questions": []},
        interrupt_id="stop-1",
        execution_id="child-1",
        parent_execution_id="root-1",
        agent_id="za38-frontend-executor",
        serial_context={
            "kind": "subagent_stop",
            "checkpoint_namespace": "fp:thread:run:child-1",
        },
    )

    result = await coordinator.request_child_interaction(
        RunRef("thread-1", "run-1"),
        interaction,
    )

    assert result.expired is False
    assert coordinator.child_interactions.get("stop-1") is None


@pytest.mark.asyncio
async def test_run_coordinator_disconnect_cleans_pending_child_interaction() -> None:
    """owner 断连时清理仍等待中的 child Interaction。"""
    async def no_persistence() -> None:
        return None

    async def no_preparation(*_args: object) -> None:
        return None

    async def no_runtime(*_args: object) -> None:
        return None

    coordinator = RunCoordinator(
        persistence_provider=no_persistence,
        preparation_provider=no_preparation,
        runtime_provider=no_runtime,
        interaction_port=object(),  # type: ignore[arg-type]
    )
    coordinator._runs["thread-1"] = type(
        "ActiveRun",
        (),
        {
            "ref": RunRef("thread-1", "run-1"),
            "owner": ConnectionRef("owner"),
            "completion": None,
        },
    )()
    coordinator.child_interactions.register(
        request_id="stop-1",
        run_id="run-1",
        execution_id="child-1",
        parent_execution_id="root-1",
        agent_id="za38-frontend-executor",
        checkpoint_namespace="fp:thread:run:child-1",
    )

    async def no_cancel(_predicate: object) -> None:
        return None

    coordinator._cancel_runs = no_cancel  # type: ignore[method-assign]
    await coordinator.owner_disconnected(ConnectionRef("owner"))

    assert coordinator.child_interactions.get("stop-1") is None


async def _ready(runtime: _Runtime) -> _Runtime:
    return runtime
