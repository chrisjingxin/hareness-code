"""Compose / child / Plugin execution 必须继承同一份 Diagnostic Log context。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from harness_agent.compose.stage_agents import (
    ManagedStageAgentPort,
    StageRequest,
    make_activity_scope,
)
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.child_stream import child_context_for
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
)
from harness_agent.runtime.run_context import RunCancellationToken, RunContext
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot


class _RecordingLog:
    """记录 child context 与事件，供断言 execution 关联。"""

    def __init__(self) -> None:
        self.records: list[tuple[dict[str, object], str, str, dict[str, object]]] = []
        self.context: dict[str, object] = {}

    def child(self, context):
        nested = _RecordingLog()
        nested.records = self.records
        nested.context = {**self.context, **dict(context)}
        return nested

    def debug(self, event, fields) -> None:
        self.records.append((dict(self.context), "debug", event, dict(fields)))

    def info(self, event, fields) -> None:
        self.records.append((dict(self.context), "info", event, dict(fields)))

    def warn(self, event, fields) -> None:
        self.records.append((dict(self.context), "warn", event, dict(fields)))

    def error(self, event, fields) -> None:
        self.records.append((dict(self.context), "error", event, dict(fields)))


def _parent_context(log: _RecordingLog, execution_id: str = "root") -> RunContext:
    return RunContext(
        thread_id="thread-1",
        run_id="run-1",
        approval_mode="yolo",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="thread-1",
            system_prompt="parent",
            workspace="/tmp",
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        execution_id=execution_id,
        agent_id="main",
        diagnostic_log=log.child(
            {
                "thread_id": "thread-1",
                "run_id": "run-1",
                "execution_id": execution_id,
                "agent_id": "main",
            }
        ),
    )


class _FakeEngine:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    @property
    def graph(self) -> Any:
        return self

    async def ainvoke(self, messages: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"messages": [AIMessage(content=self.output)]}

    async def astream(self, stream_input: Any, **kwargs: Any):
        self.calls.append({"messages": stream_input, "kwargs": kwargs})
        yield ("messages", (AIMessage(content=self.output), {}))


class _FailingEngine:
    """在 execution stream 内抛错，覆盖稳定失败诊断。"""

    @property
    def graph(self) -> Any:
        return self

    async def astream(self, _stream_input: Any, **_kwargs: Any):
        raise RuntimeError("CANARY_HC163_CHILD_FAILURE")
        yield  # pragma: no cover - 保持 async generator 形状。


class _FakeRunLease:
    async def release(self) -> None:
        return None


class _FakeLease:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.acquire_source = "new"
        self.queue_ms = 0
        self.build_ms = 1

    async def run(self) -> _FakeRunLease:
        return _FakeRunLease()

    async def release(self) -> None:
        return None


class _FakePool:
    def __init__(self, lease: _FakeLease) -> None:
        self.lease = lease

    async def acquire(self, profile: Any) -> _FakeLease:
        return self.lease

    async def finalize_draining(self, profile_key: str) -> None:
        return None


class _FakeSpec:
    def __init__(self) -> None:
        from harness_agent.runtime.agent_catalog import DelegationPolicy, EffectiveExecutionPolicy

        self.project_fingerprint = "a" * 64
        self.agent_id = "work-item-task"
        self.prompt = "fake stage prompt"
        self.tools = ()
        self.enable_memory = False
        self.enable_skills = False
        self.definition_fingerprint = "b" * 64
        self.model_view = None
        self.policy_fingerprint = "c" * 64
        self.runtime_profile = type(
            "Profile",
            (),
            {
                "profile_key": "stage-profile",
                "mcp_config_fingerprint": "d" * 64,
                "policy_fingerprint": "c" * 64,
                "definition_fingerprint": "b" * 64,
            },
        )()
        self.skill_registry = type(
            "Registry",
            (),
            {"snapshot_id": "e" * 64, "system_prompt_fragment": lambda: "skills"},
        )()
        self.mcp_snapshot = None
        self.workspace = Path(".")
        self.execution = type(
            "Exec",
            (),
            {"approval_mode": "default", "mode": "local", "sandbox_enabled": False},
        )()
        self.effective_policy = EffectiveExecutionPolicy(
            policy_ids=("builtin-main",),
            tools=None,
            mcp_tools=None,
            skills=None,
            filesystem_read=None,
            filesystem_write=None,
            shell=None,
            network=None,
            isolation="local",
            approval_mode="default",
            delegation=DelegationPolicy(
                enabled=True,
                allowed_agents=("general-purpose",),
                max_depth=1,
                max_parallelism=4,
            ),
        )


async def _registry_with_root() -> tuple[AgentExecutionRegistry, ExecutionRef]:
    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-1", "run-1")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    return registry, root


def test_child_context_binds_execution_ids_on_diagnostic_log() -> None:
    """Inline child 的 logger 必须带 child execution 身份，不能复用父 execution_id。"""
    log = _RecordingLog()
    parent = _parent_context(log)
    child_ref = ExecutionRef(
        thread_id="thread-1",
        run_id="run-1",
        execution_id="child-aaaa",
        parent_execution_id="root",
    )
    child = child_context_for(parent, child_ref=child_ref, agent_id="general-purpose")
    child.diagnostic_log.info(
        "tool.started",
        {"tool_name": "read_file", "tool_kind": "read", "model_round": 1},
    )
    context, _level, event, _fields = log.records[-1]
    assert event == "tool.started"
    assert context["execution_id"] == "child-aaaa"
    assert context["parent_execution_id"] == "root"
    assert context["agent_id"] == "general-purpose"
    assert context["thread_id"] == "thread-1"
    assert context["run_id"] == "run-1"


def test_concurrent_child_logs_do_not_share_execution_id() -> None:
    """两个并发 child 写入同一 JSONL 流时，execution 身份不得串线。"""
    log = _RecordingLog()
    parent = _parent_context(log)
    first = child_context_for(
        parent,
        child_ref=ExecutionRef(
            thread_id="thread-1",
            run_id="run-1",
            execution_id="child-one",
            parent_execution_id="root",
        ),
        agent_id="general-purpose",
    )
    second = child_context_for(
        parent,
        child_ref=ExecutionRef(
            thread_id="thread-1",
            run_id="run-1",
            execution_id="child-two",
            parent_execution_id="root",
        ),
        agent_id="explore",
    )
    first.diagnostic_log.info(
        "tool.started",
        {"tool_name": "read_file", "tool_kind": "read", "model_round": 1},
    )
    second.diagnostic_log.info(
        "tool.started",
        {"tool_name": "glob", "tool_kind": "read", "model_round": 1},
    )
    first_ctx = next(ctx for ctx, _, event, fields in log.records if fields.get("tool_name") == "read_file")
    second_ctx = next(ctx for ctx, _, event, fields in log.records if fields.get("tool_name") == "glob")
    assert first_ctx["execution_id"] == "child-one"
    assert first_ctx["agent_id"] == "general-purpose"
    assert second_ctx["execution_id"] == "child-two"
    assert second_ctx["agent_id"] == "explore"


@pytest.mark.asyncio
async def test_inline_child_logs_execution_lifecycle() -> None:
    """Inline child 在自己的 execution context 下记录完整成功终态。"""
    from harness_agent.runtime.child_stream import stream_inline_child

    log = _RecordingLog()
    parent = _parent_context(log)
    child_ref = ExecutionRef(
        thread_id="thread-1",
        run_id="run-1",
        execution_id="child-lifecycle",
        parent_execution_id="root",
    )

    result = await stream_inline_child(
        graph=_FakeEngine("child done"),
        parent=parent,
        child_ref=child_ref,
        agent_id="general-purpose",
        task="inspect",
        cancelled=lambda: False,
    )

    assert result["messages"][-1].content == "child done"
    lifecycle = [record for record in log.records if record[2].startswith("execution.")]
    assert [record[2] for record in lifecycle] == [
        "execution.started",
        "execution.completed",
    ]
    assert all(record[0]["execution_id"] == "child-lifecycle" for record in lifecycle)
    assert lifecycle[0][3] == {"kind": "child", "agent_id": "general-purpose"}
    assert lifecycle[1][3]["outcome"] == "completed"
    assert lifecycle[1][3]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_inline_child_logs_execution_failure_without_error_text() -> None:
    """Inline child 异常只记录类型与稳定摘要，不复制异常正文。"""
    from harness_agent.runtime.child_stream import stream_inline_child

    log = _RecordingLog()
    parent = _parent_context(log)
    child_ref = ExecutionRef(
        thread_id="thread-1",
        run_id="run-1",
        execution_id="child-failed",
        parent_execution_id="root",
    )

    with pytest.raises(RuntimeError, match="CANARY_HC163_CHILD_FAILURE"):
        await stream_inline_child(
            graph=_FailingEngine(),
            parent=parent,
            child_ref=child_ref,
            agent_id="explore",
            task="inspect",
            cancelled=lambda: False,
        )

    lifecycle = [record for record in log.records if record[2].startswith("execution.")]
    assert [record[2] for record in lifecycle] == ["execution.started", "execution.failed"]
    assert lifecycle[-1][3]["error_type"] == "RuntimeError"
    assert "CANARY_HC163_CHILD_FAILURE" not in repr(log.records)


@pytest.mark.asyncio
async def test_compose_stage_inherits_run_and_activity_log_context() -> None:
    """Compose stage 走共有 runtime/tool 事件，并带上 child execution 与 activity_id。"""
    log = _RecordingLog()
    engine = _FakeEngine(
        output='{"goal": "实现搜索", "acceptance": ["可排序"], "open_decisions": [], "change_kind": "feature"}'
    )
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()
    scope = make_activity_scope(
        stage_agent_id="work-item-task",
        attempt=2,
        invocation_id="abcdef1234567890",
    )
    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, **_kwargs: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )
    result = await port.run(
        StageRequest(
            stage="work-item-task",
            task="实现搜索功能",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
            compose_scope=scope,
            diagnostic_log=log.child(
                {
                    "thread_id": "thread-1",
                    "run_id": "run-1",
                    "execution_id": root.execution_id,
                    "agent_id": "main",
                }
            ),
        )
    )
    assert result.status == "completed"
    runtime_records = [
        (ctx, event, fields)
        for ctx, _level, event, fields in log.records
        if event.startswith("runtime.")
    ]
    assert runtime_records, "stage 必须经共有 runtime 事件，不能另起 Compose logger"
    context, event, _fields = runtime_records[0]
    assert context["thread_id"] == "thread-1"
    assert context["run_id"] == "run-1"
    assert context["execution_id"] == result.execution_id
    assert context["parent_execution_id"] == root.execution_id
    assert context["agent_id"] == "work-item-task"
    assert context["activity_id"] == scope["activity_id"]
    assert result.execution_id != root.execution_id
    lifecycle = [record for record in log.records if record[2].startswith("execution.")]
    assert [record[2] for record in lifecycle] == [
        "execution.started",
        "execution.completed",
    ]
    assert all(record[0]["execution_id"] == result.execution_id for record in lifecycle)
    assert lifecycle[0][3] == {"kind": "compose_stage", "agent_id": "work-item-task"}
    assert lifecycle[1][3]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_compose_stage_logs_execution_failure() -> None:
    """Compose stage 的 Delegator 失败在 stage execution 上收敛。"""
    from harness_agent.runtime.agent_delegation import AgentDelegationError

    log = _RecordingLog()
    pool = _FakePool(_FakeLease(_FailingEngine()))
    registry, root = await _registry_with_root()
    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, **_kwargs: _FakeSpec(),  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )

    with pytest.raises(AgentDelegationError):
        await port.run(
            StageRequest(
                stage="work-item-task",
                task="实现搜索功能",
                parent_ref=root,
                profile_key="stage-profile",
                cancellation_token=RunCancellationToken(),
                diagnostic_log=log.child(
                    {
                        "thread_id": "thread-1",
                        "run_id": "run-1",
                        "execution_id": root.execution_id,
                        "agent_id": "main",
                    }
                ),
            )
        )

    lifecycle = [record for record in log.records if record[2].startswith("execution.")]
    assert [record[2] for record in lifecycle] == ["execution.started", "execution.failed"]
    assert lifecycle[-1][3]["kind"] == "compose_stage"
    assert lifecycle[-1][3]["summary_code"] == "COMPOSE_STAGE_EXECUTION_FAILED"


def test_plugin_adapter_binds_child_execution_log() -> None:
    """Plugin child 必须把 execution/parent/agent 绑到同一 Diagnostic Log。"""
    from harness_agent.host.agent_host import AgentHost
    import inspect

    source = inspect.getsource(AgentHost._plugin_delegation_targets)
    assert "bind_execution_log" in source
    assert "diagnostic_log=" in source
    assert "parent_execution_id" in source
