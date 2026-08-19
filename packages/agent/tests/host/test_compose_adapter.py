"""ComposeRunAdapter 经 Session 发射 compose.progress，不跑分类器。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.engine_services import EngineDriverServices
from harness_agent.compose.models import ThreadMode
from harness_agent.host.run_execution import ComposeRunAdapter
from harness_agent.runtime.run_context import RunCancellationToken
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-adapter"
NOW = 1_700_000_000_000


class _FakePort:
    """记录事件。"""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, object]]] = []

    def emit(self, run: object, event_type: str, payload: dict[str, object], **kwargs: object) -> None:
        self.emitted.append((event_type, payload))

    def is_cancelled(self, run: object) -> bool:
        return False

    def mark_running(self, run: object) -> None:
        return None

    async def start_execution(self, run: object) -> None:
        return None

    def append_transcript(self, run: object, record: object) -> None:
        return None

    async def resolve_runtime(self, run: object):
        raise NotImplementedError

    async def request_interaction(self, run: object, spec: object):
        raise NotImplementedError

    async def request_question(self, run: object, **kwargs: object):
        raise NotImplementedError

    async def request_approval(self, run: object, **kwargs: object):
        raise NotImplementedError

    async def collect_serial_approvals(self, run: object, spec: object):
        raise NotImplementedError

    def drain_context_updates(self, run: object) -> None:
        return None

    async def flush_transcript(self, run: object) -> None:
        return None


class _FakeRun:
    """无 preparation，adapter 只走 Session 不拉模型。"""

    def __init__(self, persistence: ThreadPersistence) -> None:
        self.persistence = persistence
        self.message = "写一个 jsondiff CLI"
        self.cancellation_token = RunCancellationToken()

        class Ref:
            thread_id = THREAD
            run_id = "run-1"

        self.ref = Ref()


@pytest.mark.asyncio
async def test_adapter_emits_compose_progress_without_work_item(
    tmp_path: Path,
) -> None:
    """首轮发出 compose.progress，且不含 compose.work_item。"""
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "workspace").mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    try:
        await persistence.accept_run(
            AcceptRun(
                message="受理",
                binding=make_test_binding(THREAD, "run-0"),
                mode=ThreadMode.COMPOSE,
            )
        )
        services = EngineDriverServices(
            stage_agent=None,
            parent_ref=None,
            workspace_root=str(tmp_path / "workspace"),
            verification=None,
            profile_key="",
            cancellation_token=RunCancellationToken(),
            now_ms=lambda: NOW,
        )
        adapter = ComposeRunAdapter(services)
        port = _FakePort()
        outcome = await adapter.execute(_FakeRun(persistence), port)  # type: ignore[arg-type]
        assert outcome is None
        types = [event_type for event_type, _ in port.emitted]
        assert "compose.work_item" not in types
        assert "compose.progress" in types
        payload = next(p for t, p in port.emitted if t == "compose.progress")
        assert payload["current_stage"] == "grill"
        assert payload["thread_id"] == THREAD
        assert not any(
            isinstance(value, str) and value.startswith("compose-task-interview-")
            for value in _walk_strings(payload)
        )
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_confirming_task_does_not_restart_root_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认需求后同一轮写 Spec，不得对已经 running 的 root 再 start。"""
    from types import SimpleNamespace

    from harness_agent.runtime.managed_agent_executor import (
        ManagedAgentExecutor,
        ManagedAgentResult,
    )

    starts: list[str] = []
    streams: list[str] = []

    class Port(_FakePort):
        async def start_execution(self, run: object) -> None:
            starts.append(getattr(getattr(run, "ref", None), "run_id", ""))

        async def request_question(self, run: object, **kwargs: object) -> object:
            del run, kwargs
            return SimpleNamespace(value={"answers": {"stage-confirm": ["proceed"]}})

        async def resolve_runtime(self, run: object) -> object:
            del run
            raise AssertionError("本测试不拉真实 runtime")

    class FakeRun(_FakeRun):
        def __init__(self, persistence: ThreadPersistence) -> None:
            super().__init__(persistence)
            self.preparation = SimpleNamespace(
                skill_snapshot_id=None,
                execution_binding=None,
            )
            self.start = SimpleNamespace(mode="compose")
            self.root_execution_ref = SimpleNamespace(execution_id="exec-root")
            self.usage = {}
            self.started_at = NOW
            self.approval_presentations = SimpleNamespace(
                lookup=lambda *_args, **_kwargs: None
            )

    async def fake_execute(self: object, request: object, observer: object) -> ManagedAgentResult:
        del self, observer
        streams.append(str(getattr(request, "input", "")))
        starter = getattr(request, "execution_starter", None)
        if starter is not None:
            await starter(getattr(request, "execution_ref", ""))
        return ManagedAgentResult(
            final_content="",
            usage={},
            output_policy="passthrough",
            used_agent=True,
        )

    monkeypatch.setattr(ManagedAgentExecutor, "execute", fake_execute)

    project = tmp_path / "project"
    project.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    try:
        await persistence.accept_run(
            AcceptRun(
                message="受理",
                binding=make_test_binding(THREAD, "run-0"),
                mode=ThreadMode.COMPOSE,
            )
        )
        adapter = ComposeRunAdapter(
            EngineDriverServices(
                stage_agent=None,
                parent_ref=None,
                workspace_root=str(workspace),
                verification=None,
                profile_key="",
                cancellation_token=RunCancellationToken(),
                now_ms=lambda: NOW,
            )
        )
        await adapter.execute(FakeRun(persistence), Port())  # type: ignore[arg-type]
    finally:
        await persistence.close()

    assert len(streams) >= 2
    assert starts == ["run-1"]


def test_artifact_confirm_copy_names_each_stage_document() -> None:
    """每个阶段的确认问句必须点名对应产出物。"""
    from harness_agent.host.run_execution import _artifact_confirm_copy

    task_header, task_question = _artifact_confirm_copy("task")
    spec_header, spec_question = _artifact_confirm_copy("spec")
    plan_header, plan_question = _artifact_confirm_copy("plan")
    review_header, review_question = _artifact_confirm_copy("review")
    assert "需求" in task_header
    assert "task.md" in task_question
    assert "规格" in spec_header
    assert "spec.md" in spec_question
    assert "计划" in plan_header
    assert "plan.md" in plan_question
    assert "检视" in review_header
    assert "review.md" in review_question


def test_coordinator_does_not_prestart_compose_root_execution() -> None:
    """Compose 与 Build 一样由 Managed executor start root，避免二次 start 报 EXECUTION_STATE_TRANSITION_INVALID。"""
    from pathlib import Path

    import harness_agent.host.run_coordinator as coordinator

    source = Path(coordinator.__file__).read_text(encoding="utf-8")
    assert 'run.start.mode not in {"build", "compose"}' in source


def _walk_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_walk_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_walk_strings(item))
        return found
    return []
