"""Compose 崩溃恢复 E2E（WP15）：跨 Host 重启的安全边界恢复。

真实 SQLite 在两次 open 之间保留全部事实：Work Item identity、文档 digest、
Activity attempt、effect ledger 与 Run binding。模拟 SIGKILL 后重开
ThreadPersistence：启动扫描收敛 running→interrupted，引擎从最后安全边界
继续，不回到初始 Task，不重复已确认副作用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.task import TaskInterviewContext
from harness_agent.compose.document_store import ComposeDocumentStore
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ThreadMode,
)
from harness_agent.compose.work_item_engine import (
    ComposeTurnOutcome,
    ComposeTurnPorts,
    ComposeTurnRequest,
    ComposeWorkItemEngine,
    TypedDecisionRequest,
    TypedDecisionResult,
)
from harness_agent.threads.compose_work_item_store import ComposeWorkItemStore
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-recovery-e2e"
NOW = 1_700_000_000_000


class _FakeInteraction:
    """按 question_id 脚本化回答。"""

    def __init__(self, answers: dict[str, list[str | None]] | None = None) -> None:
        self.answers: dict[str, list[str | None]] = {
            key: list(values) for key, values in (answers or {}).items()
        }
        self.requests: list[TypedDecisionRequest] = []

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult:
        self.requests.append(request)
        queue = self.answers.get(request.question_id, [])
        value = queue.pop(0) if queue else None
        if value is None:
            return TypedDecisionResult({"answers": {}}, expired=True)
        return TypedDecisionResult({"answers": {request.question_id: [value]}})


class _CrashingTaskDriver:
    """访谈中在第二个问题前崩溃的 grill driver。"""

    def __init__(self) -> None:
        self.calls = 0

    async def next_question(self, context: TaskInterviewContext) -> str | None:
        self.calls += 1
        if self.calls >= 2:
            raise SystemExit(137)  # 模拟 SIGKILL：进程级退出语义
        return "目标边界是什么？"

    async def draft_task(self, context: TaskInterviewContext) -> str:
        return "# 目标\n\n实现站内搜索"


class _ResumingTaskDriver:
    """重启后的 grill driver：验证已答问题后直接成稿。"""

    def __init__(self) -> None:
        self.contexts: list[TaskInterviewContext] = []

    async def next_question(self, context: TaskInterviewContext) -> str | None:
        self.contexts.append(context)
        return None

    async def draft_task(self, context: TaskInterviewContext) -> str:
        self.contexts.append(context)
        return "# 目标\n\n实现站内搜索"


class _EngineFactory:
    """跨两次 persistence open 构造引擎。"""

    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "project"
        self.workspace = tmp_path / "workspace"
        self.home = tmp_path / "home"
        self.persistence: ThreadPersistence | None = None

    async def open(self) -> None:
        self.project.mkdir(exist_ok=True)
        self.persistence = await ThreadPersistence.open(
            project=self.project, home=self.home
        )
        await self.persistence.accept_run(
            AcceptRun(
                message="受理",
                binding=make_test_binding(THREAD, "run-0"),
                mode=ThreadMode.COMPOSE,
            )
        )

    def store(self) -> ComposeWorkItemStore:
        assert self.persistence is not None
        return self.persistence.compose_work_item_store()

    def engine(
        self,
        interaction: _FakeInteraction,
        task_driver: object,
    ) -> ComposeWorkItemEngine:
        assert self.persistence is not None

        class _Classifier:
            def __init__(self) -> None:
                self.outputs: list[object] = []

            async def classify(self, _context: object) -> object:
                return self.outputs.pop(0)

        classifier = _Classifier()
        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=classifier,
            interaction=interaction,
            now_ms=lambda: NOW,
            task_driver=task_driver,
        )
        engine = ComposeWorkItemEngine(ports)
        engine._classifier = classifier  # noqa: SLF001
        return engine

    async def close(self) -> None:
        assert self.persistence is not None
        await self.persistence.close()


def _turn(message: str, run_id: str) -> ComposeTurnRequest:
    return ComposeTurnRequest(
        thread_id=THREAD,
        run_id=run_id,
        message=message,
        explicit_intent=None,
        cancelled=False,
    )


async def test_restart_continues_from_last_answered_question(tmp_path: Path) -> None:
    """SIGKILL 后重开：Work Item/Activity/草稿一致恢复，访谈不重新提问。"""
    factory = _EngineFactory(tmp_path)
    try:
        await factory.open()
        interaction = _FakeInteraction({"task-interview": ["站内标题与正文"]})
        engine = factory.engine(interaction, _CrashingTaskDriver())
        engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        with pytest.raises(SystemExit):
            await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        # 崩溃时 Work Item 与草稿已落盘。
        store = factory.store()
        active = await store.load_active(THREAD)
        assert active is not None
        work_item_id = active.work_item_id
        assert await store.load_run_binding(THREAD, "run-1") == work_item_id
        activity = await store.load_activity(f"task:{work_item_id}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RUNNING
        await factory.close()

        # 模拟 Host 重启：重新 open 同一 SQLite/工作区，并取新 store。
        await factory.open()
        store = factory.store()
        converged = await store.mark_running_activities_interrupted(now_ms=NOW + 1)
        assert converged == 1

        resuming = _ResumingTaskDriver()
        interaction2 = _FakeInteraction({"task-gate": ["confirm"]})
        engine = factory.engine(interaction2, resuming)
        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        result = await engine.execute_turn(_turn("继续", "run-2"))
        assert result.work_item is not None
        assert result.work_item.work_item_id == work_item_id
        assert result.work_item.readiness.task_confirmed is True
        # 重启后访谈上下文携带崩溃前已答问题，不重新提问。
        assert len(resuming.contexts) == 2
        assert resuming.contexts[0].answers == (("目标边界是什么？", "站内标题与正文"),)
        activity = await store.load_activity(f"task:{work_item_id}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
        assert activity.attempt == 2
    finally:
        await factory.close()
