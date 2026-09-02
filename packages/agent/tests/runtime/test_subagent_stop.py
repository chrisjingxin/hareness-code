"""Qwen SubagentStop 终态门禁与 child Interaction 的离线契约测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log
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


class _RecordingDiagnosticLog:
    """记录 SubagentStop 的稳定诊断字段，不接收 Hook 正文。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def warn(self, event: str, fields: Mapping[str, object]) -> None:
        self.records.append(("warn", event, dict(fields)))


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
    """Qwen 空 JSON 不阻断 child，也不应被当成 Hook 失败。"""
    runner = _FakeHookRunner([HookResult(0, stdout="{}")])
    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == ""


@pytest.mark.asyncio
async def test_subagent_stop_empty_or_no_decision_is_normal_allow() -> None:
    """空 stdout、空对象和合法 no-decision 都是 Qwen no-decision。"""
    for hook_result in (
        HookResult(0, stdout=""),
        HookResult(0, stdout="{}"),
        HookResult(0, document={}),
        HookResult(0, document={"reason": "informational"}),
    ):
        runner = _FakeHookRunner([hook_result])
        controller = SubagentStopController(
            hook_runner=runner.run,
            interaction_port=lambda _request: asyncio.sleep(0),
        )

        result = await controller.evaluate(_request())

        assert result.action == "allow"
        assert result.warning == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    (
        {"continue": "false"},
        {"stopReason": 123},
        {"suppressOutput": 1},
        {"hookSpecificOutput": {"decision": 123}},
        {"hookSpecificOutput": {"additionalContext": 123}},
    ),
)
async def test_subagent_stop_nonempty_type_errors_are_malformed(
    document: dict[str, object],
) -> None:
    """非空输出中的类型错误不能伪装成合法 no-decision。"""
    runner = _FakeHookRunner([HookResult(0, document=document)])
    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_INVALID; child result returned."
    )


@pytest.mark.asyncio
async def test_subagent_stop_runner_marks_nonzero_malformed_output_as_invalid() -> None:
    """Runner 已确认非空输出解析失败时，controller 仍返回 malformed 稳定码。"""
    runner = _FakeHookRunner(
        [HookResult(2, stderr="Hook returned invalid JSON", malformed=True)]
    )
    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_INVALID; child result returned."
    )


@pytest.mark.asyncio
async def test_subagent_stop_non_mapping_document_is_malformed() -> None:
    """假的 runner 不能用非对象 document 绕过 SubagentStop 输出校验。"""
    runner = _FakeHookRunner([HookResult(0, document=[])])  # type: ignore[arg-type]
    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_INVALID; child result returned."
    )


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
async def test_subagent_stop_hook_failures_allow_completed_child_with_warning() -> None:
    """Qwen Hook 级失败只告警并释放已完成 child，不能丢弃 child 结果。"""
    for hook_result in (
        HookResult(0, document={"unexpected": True}),
        HookResult(0, stdout="not-json"),
        HookResult(1, stderr="offline hook failed"),
        HookResult(0, timed_out=True),
    ):
        runner = _FakeHookRunner([hook_result])

        controller = SubagentStopController(
            hook_runner=runner.run,
            interaction_port=lambda _request: asyncio.sleep(0),
        )
        result = await controller.evaluate(_request())
        assert result.action == "allow"
        assert "SUBAGENT_STOP_HOOK_" in result.warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    (
        (
            HookResult(1, stderr="offline hook failed"),
            HookResult(
                0,
                document={
                    "decision": "block",
                    "reason": "先检查生成物",
                    "hookSpecificOutput": {"additionalContext": "补充门禁上下文"},
                },
            ),
        ),
        (
            HookResult(
                0,
                document={
                    "decision": "block",
                    "reason": "先检查生成物",
                    "hookSpecificOutput": {"additionalContext": "补充门禁上下文"},
                },
            ),
            HookResult(1, stderr="offline hook failed"),
        ),
    ),
)
async def test_subagent_stop_aggregates_failure_and_block_regardless_of_order(
    results: tuple[HookResult, HookResult],
) -> None:
    """失败 Hook 不能短路，任意顺序的有效 block 都必须进入 interaction。"""
    calls = 0
    interactions: list[InteractionRequest] = []
    log = _RecordingDiagnosticLog()

    async def runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        nonlocal calls
        calls += 1
        return results

    async def interaction(request: InteractionRequest) -> InteractionResult:
        interactions.append(request)
        return InteractionResult({"answers": {"question-1": ["continue"]}})

    controller = SubagentStopController(
        hook_runner=runner,
        interaction_port=interaction,
        diagnostic_log=log,
    )
    result = await controller.evaluate(_request())

    assert calls == 1
    assert result.action == "continue"
    assert result.warning == ""
    assert len(interactions) == 1
    assert "先检查生成物" in interactions[0].serial_context["reason"]  # type: ignore[index]
    assert "补充门禁上下文" in interactions[0].serial_context["additional_context"]  # type: ignore[index]
    assert [record[2]["summary_code"] for record in log.records] == [
        "SUBAGENT_STOP_HOOK_NONZERO"
    ]
    assert all("outcome" not in record[2] for record in log.records)


@pytest.mark.asyncio
async def test_subagent_stop_hook_runner_continues_after_one_hook_exception(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """真实 HookRunner 收集一个异常后仍须让后续匹配 Hook 参与聚合。"""
    definitions = tuple(
        HookDefinition(
            plugin_id="plugin-za38",
            event="SubagentStop",
            matcher="*",
            command=command,
            args=(),
            timeout_seconds=1.0,
            asynchronous=False,
            shell=None,
            root=tmp_path,
            data=tmp_path,
            workspace=tmp_path,
        )
        for command in ("raise", "block")
    )
    runner = HookRunner(definitions)

    async def invoke(
        definition: HookDefinition,
        _payload: Mapping[str, object],
        *,
        tool_name: str,
        diagnostic_log: Any,
    ) -> HookResult:
        assert tool_name == "za38-frontend-executor"
        assert diagnostic_log is not None
        if definition.command == "raise":
            raise RuntimeError("must not escape")
        return HookResult(0, document={"decision": "block", "reason": "后续门禁"})

    monkeypatch.setattr(runner, "_invoke", invoke)

    results = await runner.run(
        "SubagentStop",
        tool_name="za38-frontend-executor",
        plugin_id="plugin-za38",
        payload={},
    )

    assert len(results) == 2
    assert results[0].exit_code != 0
    assert results[1].document == {"decision": "block", "reason": "后续门禁"}


@pytest.mark.asyncio
async def test_subagent_stop_merges_multiple_blocks_with_bounded_feedback() -> None:
    """多个 block 按 Qwen OR 语义有界合并 reason 与 additionalContext。"""
    first_reason = "A" * 9000
    second_reason = "B" * 9000
    first_context = "C" * 9000
    second_context = "D" * 9000
    results = (
        HookResult(
            0,
            document={
                "decision": "block",
                "reason": first_reason,
                "additionalContext": first_context,
            },
        ),
        HookResult(
            0,
            document={
                "decision": "block",
                "reason": second_reason,
                "additionalContext": second_context,
            },
        ),
    )
    interactions: list[InteractionRequest] = []

    async def runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        return results

    async def interaction(request: InteractionRequest) -> InteractionResult:
        interactions.append(request)
        return InteractionResult({"answers": {"question-1": ["continue"]}})

    controller = SubagentStopController(
        hook_runner=runner,
        interaction_port=interaction,
    )
    result = await controller.evaluate(_request())

    assert result.action == "continue"
    assert len(interactions) == 1
    serial_context = interactions[0].serial_context
    assert serial_context is not None
    assert len(str(serial_context["reason"]).encode()) <= 16 * 1024
    assert len(str(serial_context["additional_context"]).encode()) <= 16 * 1024
    assert first_reason in str(serial_context["reason"])
    assert second_reason not in str(serial_context["reason"])


@pytest.mark.asyncio
async def test_subagent_stop_failure_and_allow_warn_only_when_no_block() -> None:
    """失败与合法 allow 聚合时释放 child，并保留稳定 warning。"""
    log = _RecordingDiagnosticLog()

    async def runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        return (
            HookResult(1, stderr="offline hook failed"),
            HookResult(0, document={"decision": "allow"}),
        )

    controller = SubagentStopController(
        hook_runner=runner,
        interaction_port=lambda _request: asyncio.sleep(0),
        diagnostic_log=log,
    )
    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_NONZERO; child result returned."
    )
    assert [record[2]["summary_code"] for record in log.records] == [
        "SUBAGENT_STOP_HOOK_NONZERO"
    ]
    assert all("outcome" not in record[2] for record in log.records)


@pytest.mark.asyncio
async def test_subagent_stop_failure_diagnostic_matches_v1_hook_contract(
    tmp_path: Path,
) -> None:
    """SubagentStop failure diagnostic 必须能通过共享 v1 schema。"""
    diagnostic_log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
    )
    async def runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        return (HookResult(1, stderr="redacted"),)

    controller = SubagentStopController(
        hook_runner=runner,
        interaction_port=lambda _request: asyncio.sleep(0),
        diagnostic_log=diagnostic_log,
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert "SUBAGENT_STOP_HOOK_NONZERO" in result.warning
    assert lifecycle.snapshot()["contract_violations"] == 0
    await lifecycle.close()


@pytest.mark.asyncio
async def test_subagent_stop_hook_exception_is_redacted_and_allows_stop() -> None:
    """Hook runner 异常不泄露异常正文，也不改变 child 的成功终态。"""
    async def broken_runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        raise RuntimeError("secret host path and credential")

    controller = SubagentStopController(
        hook_runner=broken_runner,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    result = await controller.evaluate(_request())

    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_FAILED; child result returned."
    )
    assert "secret" not in result.warning


@pytest.mark.asyncio
async def test_subagent_stop_hook_failure_does_not_swallow_cancellation() -> None:
    """Hook 普通异常与 Run cancellation 同时发生时仍传播 canonical cancellation。"""
    cancelled = False

    async def failing_runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        nonlocal cancelled
        cancelled = True
        raise RuntimeError("hook failed while the run was cancelled")

    controller = SubagentStopController(
        hook_runner=failing_runner,
        interaction_port=lambda _request: asyncio.sleep(0),
    )
    request = replace(_request(), is_cancelled=lambda: cancelled)

    with pytest.raises(asyncio.CancelledError):
        await controller.evaluate(request)


@pytest.mark.asyncio
async def test_subagent_stop_matched_empty_and_closed_runner_allow_with_warning() -> None:
    """命中的 Qwen gate 若 runner 关闭，只告警并放行已完成 child。"""
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
            result = await controller.evaluate(_request())
            assert result.action == "allow"
            assert "SUBAGENT_STOP_HOOK_RUNNER_CLOSED" in result.warning
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

    async def fake_invoke(
        definition: HookDefinition,
        _payload: Mapping[str, object],
        **_kwargs: object,
    ) -> HookResult:
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
async def test_subagent_stop_rejects_invalid_interaction_but_propagates_cancellation() -> None:
    """用户门禁答案仍 fail closed，但取消必须保留 canonical cancellation。"""
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
    with pytest.raises(asyncio.CancelledError):
        await controller.evaluate(_request())


@pytest.mark.asyncio
async def test_subagent_stop_does_not_use_plugin_authorization_state() -> None:
    """Hook gate 不再读取已删除的 Plugin enabled/trusted 授权字段。"""
    runner = _FakeHookRunner([HookResult(0, document={"decision": "allow"})])

    async def interaction(_request: InteractionRequest) -> InteractionResult:
        raise AssertionError("allow Hook 不应请求交互")

    controller = SubagentStopController(
        hook_runner=runner.run,
        interaction_port=interaction,
    )
    result = await controller.evaluate(_request())
    assert result.action == "allow"
    assert len(runner.payloads) == 1


@pytest.mark.asyncio
async def test_subagent_stop_block_cap_returns_latest_child_with_warning() -> None:
    """达到 Qwen blocking cap 后结束当前 child，并附稳定 warning。"""
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
    result = await controller.evaluate(_request())
    assert result.action == "allow"
    assert result.warning == (
        "SubagentStop hook blocked continuation 8 consecutive times; "
        "overriding and ending the turn."
    )


@pytest.mark.asyncio
async def test_subagent_stop_hook_cancellation_is_not_treated_as_hook_success() -> None:
    """Hook task 被取消时不走 Hook fail-open，直接传播 Run cancellation。"""
    async def cancelled_runner(_event: str, **_kwargs: object) -> tuple[HookResult, ...]:
        raise asyncio.CancelledError

    controller = SubagentStopController(
        hook_runner=cancelled_runner,
        interaction_port=lambda _request: asyncio.sleep(0),
    )

    with pytest.raises(asyncio.CancelledError):
        await controller.evaluate(_request())


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
async def test_managed_executor_keeps_child_result_when_gate_returns_warning() -> None:
    """终态 Hook warning 随已完成 child 结果返回，不把 delegation 改成失败。"""
    runtime = _Runtime(_FakeAgent())

    async def gate(_final: ManagedFinalOutput) -> FinalOutputGateDecision:
        return FinalOutputGateDecision(
            action="allow",
            warning=(
                "SubagentStop Hook warning: SUBAGENT_STOP_HOOK_FAILED; "
                "child result returned."
            ),
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
        idempotency_key="delegate-warning-1",
        final_output_gate=gate,
    )

    result = await ManagedAgentExecutor().execute(request, _Observer())

    assert result.final_content == "first output"
    assert "SUBAGENT_STOP_HOOK_FAILED" in result.warning
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
    coordinator._runs["thread-1"] = type(
        "ActiveRun",
        (),
        {
            "ref": RunRef("thread-1", "run-1"),
            "owner": ConnectionRef("owner"),
            "completion": None,
                "cancel_requested": False,
                "status": "running",
                "timing": None,
                "diagnostic_log": type(
                "Log",
                (),
                {"info": lambda *_args: None, "warn": lambda *_args: None, "error": lambda *_args: None},
            )(),
        },
    )()
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
