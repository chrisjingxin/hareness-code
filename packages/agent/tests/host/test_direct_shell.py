"""DirectShellRunAdapter 直出 Shell 执行器回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from harness_agent.host.run_coordinator import (
    ConnectionRef,
    RunPreparation,
    RunState,
    StartRun,
)
from harness_agent.host.run_execution import (
    CONTENT_DELTA,
    DirectShellRunAdapter,
    RUN_STARTED,
    TOOL_COMPLETED,
    TOOL_STARTED,
)


class _MockPort:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.running_marked = False
        self.execution_started = False
        self.cancelled = False

    def emit(
        self,
        _run: Any,
        event_type: str,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        self.events.append((event_type, payload))

    def mark_running(self, _run: Any) -> None:
        self.running_marked = True

    async def start_execution(self, _run: Any) -> None:
        self.execution_started = True

    def is_cancelled(self, _run: Any) -> bool:
        return self.cancelled


@pytest.mark.asyncio
async def test_direct_shell_executes_echo_command(tmp_path: Path) -> None:
    """DirectShellRunAdapter 正确执行命令并触发 tool 与 content 事件。"""
    adapter = DirectShellRunAdapter()
    port = _MockPort()

    start_run = StartRun(
        mode="direct_shell",
        message="echo hello_direct_shell",
        thread_id="thread-1",
        run_id="run-1",
    )
    run = RunState(
        start=start_run,
        owner=ConnectionRef("conn-1"),
        persistence=None,
        preparation=RunPreparation(),
        message=start_run.message,
    )

    await adapter.execute(run, port)

    assert port.running_marked

    event_types = [e[0] for e in port.events]
    assert RUN_STARTED in event_types
    assert TOOL_STARTED in event_types
    assert TOOL_COMPLETED in event_types

    tool_completed = next(e[1] for e in port.events if e[0] == TOOL_COMPLETED)
    assert "hello_direct_shell" in tool_completed["result"]["content"]
    assert tool_completed["result"]["is_error"] is False


@pytest.mark.asyncio
async def test_direct_shell_handles_empty_command(tmp_path: Path) -> None:
    """空命令直接报告错误，不创建子进程。"""
    adapter = DirectShellRunAdapter()
    port = _MockPort()

    start_run = StartRun(
        mode="direct_shell",
        message="   ",
        thread_id="thread-1",
        run_id="run-1",
    )
    run = RunState(
        start=start_run,
        owner=ConnectionRef("conn-1"),
        persistence=None,
        preparation=RunPreparation(),
        message=start_run.message,
    )

    await adapter.execute(run, port)

    tool_completed = next(e[1] for e in port.events if e[0] == TOOL_COMPLETED)
    assert tool_completed["result"]["is_error"] is True
    assert "non-empty string" in tool_completed["result"]["content"]


@pytest.mark.asyncio
async def test_direct_shell_with_persistence_resume(tmp_path: Path) -> None:
    """在已存在的历史 Thread（无论已有模式是 BUILD 还是 COMPOSE）中执行 direct_shell 不会抛 ValueError。"""
    from harness_agent.host.run_coordinator import RunCoordinator
    from harness_agent.runtime.agent_execution import AgentExecutionRegistry
    from harness_agent.threads.thread_persistence import (
        AcceptRun,
        ThreadPersistence,
    )
    from harness_agent.compose.models import ThreadMode
    from harness_agent.runtime.execution_binding import RunExecutionBinding

    store = await ThreadPersistence.open(project=tmp_path, home=tmp_path / "home")

    from harness_agent.config.config import ModelProfile, ModelSettings
    from harness_agent.runtime.execution_binding import SafeModelProfile

    profile_record = SafeModelProfile.from_profile(
        ModelProfile(
            profile_id="fast",
            settings=ModelSettings(name="deepseek-chat", base_url="https://api.deepseek.com/v1"),
            source="test",
        )
    ).to_record()

    # 模拟历史会话中已有首个 Run，并锁定了 ThreadMode.BUILD
    binding = RunExecutionBinding.from_records(
        thread_id="thread-hist",
        run_id="run-0",
        requested_selection={"primary_profile": "fast"},
        actual_primary_binding={
            "profile": profile_record,
            "source": "thread-primary",
        },
        runtime_profile_id="fast",
        created_at_ms=1000,
    )
    await store.accept_run(
        AcceptRun(
            message="initial history run",
            binding=binding,
            context_snapshot=None,
            mode=ThreadMode.BUILD,
        )
    )

    class _NoopInteraction:
        async def request(self, _req):
            return None

    async def _get_store():
        return store

    async def _get_prep(cmd, pers):
        return RunPreparation(
            execution_binding=RunExecutionBinding.from_records(
                thread_id=cmd.thread_id,
                run_id=cmd.run_id,
                requested_selection={"primary_profile": "fast"},
                actual_primary_binding={
                    "profile": profile_record,
                    "source": "thread-primary",
                },
                runtime_profile_id="fast",
                created_at_ms=2000,
            )
        )

    coordinator = RunCoordinator(
        persistence_provider=_get_store,
        preparation_provider=_get_prep,
        runtime_provider=lambda _run: None,
        interaction_port=_NoopInteraction(),
        execution_registry=AgentExecutionRegistry(),
    )

    # 历史会话恢复后发起 direct_shell
    command = StartRun(
        mode="direct_shell",
        message="echo from_history_thread",
        thread_id="thread-hist",
        run_id="run-direct-1",
    )
    execution = await coordinator.start(command, ConnectionRef("conn-1"))
    assert execution.accepted is True

    events = [event async for event in execution.events]
    event_types = [e.type for e in events]
    assert "run.started" in event_types
    for e in events:
        if e.type == "run.failed":
            print(f"FAILED PAYLOAD: {e.payload}")
    assert "tool.started" in event_types
    assert "tool.completed" in event_types
    assert "run.completed" in event_types

    await store.close()
