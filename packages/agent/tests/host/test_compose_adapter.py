"""ComposeRunAdapter 生产接线回归测试（WP15 后修复）。

覆盖 adapter 经真实 ThreadPersistence 驱动 ComposeWorkItemEngine 的一个完整
Turn：意图分类走 stage seam、Work Item 创建、compose.work_item 事件发射，
并证明 Task Activity 的可重试失败不会被默认收敛成 completed。此前
`result.outcome` 字段名笔误在任何 Turn 都会抛 AttributeError，本测试同时
锁住 adapter 的生产接线与失败终态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.engine_services import EngineDriverServices
from harness_agent.compose.models import ThreadMode
from harness_agent.compose.stage_agents import StageResult
from harness_agent.host.run_coordinator import RunState
from harness_agent.host.run_execution import ComposeRunAdapter
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from harness_agent.runtime.run_context import RunCancellationToken
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-adapter"
NOW = 1_700_000_000_000


class _FakeStageAgent:
    """脚本化 stage 结果：意图分类返回 start_new_work。"""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def run(self, request: object, observer: object | None = None) -> StageResult:
        self.requests.append(request)
        return StageResult(
            execution_id="exec-intent",
            agent_id="main",
            status="completed",
            output={"intent": "start_new_work", "detail": ""},
        )


class _FakePort:
    """满足 RunLifecyclePort 的最小实现：记录事件、无取消、无交互。"""

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
    """携带 adapter 所需字段的最小 Run 代理。"""

    def __init__(self, persistence: ThreadPersistence) -> None:
        self.persistence = persistence
        self.message = "实现站内搜索"
        self.cancellation_token = RunCancellationToken()

        class Ref:
            thread_id = THREAD
            run_id = "run-1"

        self.ref = Ref()

    def root_execution_ref(self):
        from harness_agent.runtime.execution_binding import ExecutionRef

        return ExecutionRef.root(THREAD, "run-1")


async def test_adapter_surfaces_retryable_task_failure_as_failed_outcome(
    tmp_path: Path,
) -> None:
    """Task Activity 可重试失败不能被 coordinator 默认显示为 completed。"""
    project = tmp_path / "project"
    project.mkdir()
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
            stage_agent=_FakeStageAgent(),
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
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.code == "COMPOSE_ACTIVITY_RETRYABLE_FAILED"
        assert outcome.retryable is True
        # Work Item 已创建并绑定 Run。
        store = persistence.compose_work_item_store()
        active = await store.load_active(THREAD)
        assert active is not None
        assert await store.load_run_binding(THREAD, "run-1") == active.work_item_id
        # 事件已发射（blocked 判断行必然被执行且不再抛异常）。
        assert any(event_type == "compose.work_item" for event_type, _ in port.emitted)
        payload = next(payload for event_type, payload in port.emitted if event_type == "compose.work_item")
        assert payload["work_item"]["work_item_id"] == active.work_item_id
        assert payload["work_item"]["status"] == "active"
    finally:
        await persistence.close()
