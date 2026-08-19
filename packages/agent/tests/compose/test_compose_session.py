"""ComposeSession 首轮 Grill：无分类器、分配 slug、派生需求阶段。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.models import ThreadMode
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding


class _BoomClassifier:
    """任何调用都视为失败：停点 1 不得再跑意图分类。"""

    async def classify(self, context: object) -> object:
        raise AssertionError("TurnIntent 分类器不得被 ComposeSession 调用")


@pytest.mark.asyncio
async def test_first_compose_turn_skips_classifier_and_starts_grill(
    tmp_path: Path,
) -> None:
    """首条 Compose 消息创建 session，阶段为 grill，且不调用分类器。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_progress_store()

    grilled: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(f"{request.thread_id}:{slug}")

    session = ComposeSession(
        ComposeSessionPorts(
            store=store,
            workspace=workspace,
            run_grill=run_grill,
            classifier=_BoomClassifier(),
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-1",
            run_id="run-1",
            message="写一个 Python CLI 工具 jsondiff，附 pytest 和 README。",
        )
    )
    await persistence.close()

    assert grilled and grilled[0].startswith("thread-1:")
    assert result.progress is not None
    assert result.progress["current_stage"] == "grill"
    assert result.progress["status"] == "active"
    assert result.progress["waiting"] == "task_confirm"
    assert any(
        stage["id"] == "requirement" and stage["state"] == "current"
        for stage in result.progress["stages"]
    )
    assert result.progress["slug"]


@pytest.mark.asyncio
async def test_proceed_message_confirms_requirement_and_skips_grill(
    tmp_path: Path,
) -> None:
    """用户明确推进时不再 Grill，而是确认需求并进入下一阶段。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
        is_proceed_message,
    )

    assert is_proceed_message("推进到下个阶段")
    assert is_proceed_message("确认，进入下一阶段")
    assert not is_proceed_message("数组按索引比还是当集合？")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_progress_store()
    grilled: list[str] = []
    specced: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(slug)

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        specced.append(slug)

    session = ComposeSession(
        ComposeSessionPorts(
            store=store,
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
        )
    )
    first = await session.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-1",
            run_id="run-1",
            message="写一个 jsondiff CLI，附测试和 README。",
        )
    )
    assert first.progress is not None
    assert grilled == [first.progress["slug"]]

    second = await session.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-1",
            run_id="run-2",
            message="推进到下个阶段",
        )
    )
    await persistence.close()

    assert grilled == [first.progress["slug"]]
    assert specced == [first.progress["slug"]]
    assert second.progress is not None
    assert second.progress["current_stage"] == "spec"
    assert second.progress["waiting"] == "ask_user"
    assert any(
        stage["id"] == "requirement" and stage["state"] == "confirmed"
        for stage in second.progress["stages"]
    )
    task = workspace / "docs" / "compose" / first.progress["slug"] / "task.md"
    assert task.is_file()
    assert "jsondiff" in task.read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_confirming_existing_spec_enters_plan_not_spec(
    tmp_path: Path,
) -> None:
    """规格产出物已在时，确认进入下一阶段应写计划，不得再跑 Spec。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    grilled: list[str] = []
    specced: list[str] = []
    planned: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(slug)

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        specced.append(slug)
        spec = workspace / "docs" / "compose" / slug / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# Spec\n\njsondiff CLI 规格\n", encoding="utf-8")

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        planned.append(slug)

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
        )
    )
    await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个 jsondiff CLI")
    )
    await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-2", message="确认进入下一阶段")
    )
    assert specced == grilled
    third = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-3", message="确认进入下一阶段")
    )
    await persistence.close()

    assert specced == grilled
    assert planned == grilled
    assert third.progress is not None
    assert any(
        stage["id"] == "spec" and stage["state"] == "confirmed"
        for stage in third.progress["stages"]
    )
    assert third.progress["current_stage"] == "plan"


@pytest.mark.asyncio
async def test_runtime_asks_confirm_after_grill_finishes(
    tmp_path: Path,
) -> None:
    """需求 Agent 本轮结束后必须弹出产出物确认，不能把用户丢回输入栏。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    asked: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def request_stage_confirm(record: object, artifact: str) -> bool:
        del record
        asked.append(artifact)
        return False

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            request_stage_confirm=request_stage_confirm,
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个 jsondiff CLI")
    )
    await persistence.close()

    assert asked == ["task"]
    assert result.progress is not None
    assert result.progress["waiting"] == "task_confirm"
    assert result.progress["current_stage"] == "grill"


@pytest.mark.asyncio
async def test_artifact_confirm_advances_reject_stays(
    tmp_path: Path,
) -> None:
    """产出物就绪后 Runtime 用 ask_user 确认：确认进下一阶段，我要改则留下改。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    asked: list[str] = []
    planned: list[str] = []
    answers = iter((True, False, True))

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        del request
        spec = workspace / "docs" / "compose" / slug / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# Spec\n\njsondiff CLI 规格与验收。\n", encoding="utf-8")

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        planned.append(slug)

    async def request_stage_confirm(record: object, artifact: str) -> bool:
        del record
        asked.append(artifact)
        return next(answers)

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
            request_stage_confirm=request_stage_confirm,
        )
    )
    rejected = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个 jsondiff CLI")
    )
    assert rejected.progress is not None
    assert rejected.progress["current_stage"] == "spec"
    assert rejected.progress["waiting"] == "spec_confirm"
    assert any(
        stage["id"] == "spec" and stage["state"] == "current"
        for stage in rejected.progress["stages"]
    )
    assert asked == ["task", "spec"]
    assert planned == []

    accepted = await session.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-1",
            run_id="run-2",
            message="验收里补上 stdin 空输入",
        )
    )
    await persistence.close()

    assert asked == ["task", "spec", "spec"]
    assert accepted.progress is not None
    assert accepted.progress["current_stage"] == "plan"
    assert any(
        stage["id"] == "spec" and stage["state"] == "confirmed"
        for stage in accepted.progress["stages"]
    )
    assert planned == [rejected.progress["slug"]]


@pytest.mark.asyncio
async def test_confirming_plan_starts_implement(
    tmp_path: Path,
) -> None:
    """确认计划后同一轮必须启动实现，进度为实现进行中。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
        build_implement_prompt,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    implemented: list[str] = []
    answers = iter((True, True, True))

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        del request
        spec = workspace / "docs" / "compose" / slug / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# Spec\n\n限流器规格\n", encoding="utf-8")

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        docs = workspace / "docs" / "compose" / slug
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "plan.md").write_text(
            "---\nverify_command: pytest -q\n---\n# Plan\n\n实现限流器。\n",
            encoding="utf-8",
        )
        (docs / "todo.md").write_text("- [ ] 写测试\n- [ ] 写实现\n", encoding="utf-8")

    async def run_implement(request: ComposeTurnRequest, slug: str) -> None:
        del request
        implemented.append(slug)

    async def request_stage_confirm(record: object, artifact: str) -> bool:
        del record
        if artifact == "review":
            return False
        return next(answers)

    async def run_review(request: ComposeTurnRequest, slug: str) -> None:
        del request
        review = workspace / "docs" / "compose" / slug / "review.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# Review\n\n未发现阻断问题。\n", encoding="utf-8")

    snapshots: list[dict[str, object]] = []
    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
            run_implement=run_implement,
            run_review=run_review,
            request_stage_confirm=request_stage_confirm,
            on_progress=snapshots.append,
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个滑动窗口限流器")
    )
    await persistence.close()

    assert implemented
    assert result.progress is not None
    assert any(item.get("current_stage") == "review" for item in snapshots)
    assert any(
        stage["id"] == "plan" and stage["state"] == "confirmed"
        for stage in result.progress["stages"]
    )
    assert not any(stage["id"] == "verify" for stage in result.progress["stages"])
    assert (workspace / "docs" / "compose" / implemented[0] / "review.md").is_file()
    pack = build_implement_prompt(workspace, implemented[0])
    assert "todo.md" in pack
    assert "plan.md" in pack
    assert "用户消息：" not in pack
    assert "滑动窗口" in pack or "限流" in pack


def _write_pipeline_docs(workspace: Path, slug: str) -> None:
    docs = workspace / "docs" / "compose" / slug
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "spec.md").write_text("# Spec\n\n限流器规格\n", encoding="utf-8")
    (docs / "plan.md").write_text(
        "---\nverify_command: pytest -q\n---\n# Plan\n\n实现限流器。\n",
        encoding="utf-8",
    )
    (docs / "todo.md").write_text("- [ ] 写测试\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_implement_goes_to_review_without_runtime_verify(tmp_path: Path) -> None:
    """TDD 之后不再由 Runtime 跑第二次验收，直接进入检视。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        del request
        _write_pipeline_docs(workspace, slug)

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_implement(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    reviewed: list[str] = []

    async def run_review(request: ComposeTurnRequest, slug: str) -> None:
        del request
        reviewed.append(slug)
        review = workspace / "docs" / "compose" / slug / "review.md"
        review.write_text("# Review\n\n可接受。\n", encoding="utf-8")

    async def request_stage_confirm(record: object, artifact: str) -> bool:
        del record
        return artifact != "review"

    snapshots: list[dict[str, object]] = []
    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
            run_implement=run_implement,
            run_review=run_review,
            request_stage_confirm=request_stage_confirm,
            on_progress=snapshots.append,
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个滑动窗口限流器")
    )
    await persistence.close()

    assert reviewed
    assert result.progress is not None
    assert any(item.get("current_stage") == "review" for item in snapshots)
    assert any(
        item.get("waiting") == "review_confirm" for item in snapshots
    )
    assert not any(stage["id"] == "verify" for stage in result.progress["stages"])


async def _async_true(*_args: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_confirming_review_marks_session_completed(tmp_path: Path) -> None:
    """确认 review.md 后 session 结束。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnOutcome,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        del request
        _write_pipeline_docs(workspace, slug)

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_implement(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_review(request: ComposeTurnRequest, slug: str) -> None:
        del request
        (workspace / "docs" / "compose" / slug / "review.md").write_text(
            "# Review\n\n可接受。\n", encoding="utf-8"
        )

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
            run_implement=run_implement,
            run_review=run_review,
            request_stage_confirm=lambda *_args: _async_true(),
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个滑动窗口限流器")
    )
    await persistence.close()

    assert result.status is ComposeTurnOutcome.COMPLETED
    assert result.progress is not None
    assert result.progress["status"] == "completed"
    assert result.progress["current_stage"] == "review"
    assert any(
        stage["id"] == "review" and stage["state"] == "confirmed"
        for stage in result.progress["stages"]
    )


@pytest.mark.asyncio
async def test_rejecting_review_returns_to_implement(tmp_path: Path) -> None:
    """检视点「按意见改」后回到实现，并把 review.md 交给下一轮实现。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
        build_implement_prompt,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    implemented: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        del request
        _write_pipeline_docs(workspace, slug)

    async def run_plan(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    async def run_implement(request: ComposeTurnRequest, slug: str) -> None:
        del request
        implemented.append(slug)

    async def run_review(request: ComposeTurnRequest, slug: str) -> None:
        del request
        (workspace / "docs" / "compose" / slug / "review.md").write_text(
            "# Review\n\n文件过大，请拆分。\n", encoding="utf-8"
        )

    async def request_stage_confirm(record: object, artifact: str) -> bool:
        del record
        return artifact != "review"

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            run_plan=run_plan,
            run_implement=run_implement,
            run_review=run_review,
            request_stage_confirm=request_stage_confirm,
        )
    )
    result = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个滑动窗口限流器")
    )
    await persistence.close()

    assert implemented
    assert result.progress is not None
    assert result.progress["current_stage"] == "implement"
    assert any(
        stage["id"] == "implement" and stage["state"] == "current"
        for stage in result.progress["stages"]
    )
    pack = build_implement_prompt(workspace, implemented[0])
    assert "review.md" in pack
    assert "文件过大" in pack


@pytest.mark.asyncio
async def test_abandon_leaves_docs_and_clears_current_session(tmp_path: Path) -> None:
    """/abandon 废弃当前套件，文档仍在，inspect 不再返回进行中进度。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionError,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    grilled: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(slug)

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
        )
    )
    started = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个限流器")
    )
    slug = str(started.progress["slug"])
    docs = workspace / "docs" / "compose" / slug
    abandoned = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-2", message="/abandon")
    )
    inspected = await session.inspect(thread_id="thread-1")
    with pytest.raises(ComposeSessionError) as exc:
        await session.execute_turn(
            ComposeTurnRequest(thread_id="thread-1", run_id="run-3", message="/abandon")
        )
    await persistence.close()

    assert abandoned.progress is not None
    assert abandoned.progress["status"] == "abandoned"
    assert docs.is_dir()
    assert (docs / "task.md").is_file()
    assert inspected is None
    assert exc.value.code == "COMPOSE_NOTHING_TO_ABANDON"


@pytest.mark.asyncio
async def test_abandon_rejects_goal_and_new_work_requires_goal(tmp_path: Path) -> None:
    """带目标的 /abandon 与空 /new-work 都拒绝。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionError,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
        )
    )
    await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写限流器")
    )
    with pytest.raises(ComposeSessionError) as abandon_exc:
        await session.execute_turn(
            ComposeTurnRequest(
                thread_id="thread-1",
                run_id="run-2",
                message="/abandon 写 HTTP 服务",
            )
        )
    assert abandon_exc.value.code == "COMPOSE_ABANDON_TAKES_NO_GOAL"
    with pytest.raises(ComposeSessionError) as new_work_exc:
        await session.execute_turn(
            ComposeTurnRequest(thread_id="thread-1", run_id="run-3", message="/new-work")
        )
    assert new_work_exc.value.code == "COMPOSE_NEW_WORK_GOAL_REQUIRED"
    await persistence.close()


@pytest.mark.asyncio
async def test_new_work_starts_new_slug_and_keeps_old_docs(tmp_path: Path) -> None:
    """/new-work <目标> 放下当前套件并立刻 Grill 新 slug，旧目录保留。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=tmp_path / "project",
        home=tmp_path / "home",
    )
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    grilled: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(slug)

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
        )
    )
    first = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个限流器")
    )
    old_slug = str(first.progress["slug"])
    second = await session.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-1",
            run_id="run-2",
            message="/new-work 写 HTTP 服务",
        )
    )
    await persistence.close()

    new_slug = str(second.progress["slug"])
    assert new_slug != old_slug
    assert grilled[-1] == new_slug
    assert (workspace / "docs" / "compose" / old_slug / "task.md").is_file()
    assert second.progress["current_stage"] == "grill"


@pytest.mark.asyncio
async def test_confirmed_task_survives_reopen_without_grilling(tmp_path: Path) -> None:
    """确认 Task 后重开持久化，下一 Turn 从确认进度走，不重新 Grill。"""
    from harness_agent.compose.session import (
        ComposeSession,
        ComposeSessionPorts,
        ComposeTurnRequest,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "project"
    home = tmp_path / "home"
    persistence = await ThreadPersistence.open(project=project, home=home)
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-1", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    grilled: list[str] = []
    specced: list[str] = []

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        grilled.append(slug)

    async def run_spec(request: ComposeTurnRequest, slug: str) -> None:
        specced.append(slug)

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
            request_stage_confirm=lambda *_args: _async_true(),
        )
    )
    first = await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-1", message="写一个限流器")
    )
    await session.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-2", message="确认")
    )
    await persistence.close()

    reopened = await ThreadPersistence.open(project=project, home=home)
    resumed = ComposeSession(
        ComposeSessionPorts(
            store=reopened.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
            run_spec=run_spec,
        )
    )
    inspected = await resumed.inspect(thread_id="thread-1")
    after = await resumed.execute_turn(
        ComposeTurnRequest(thread_id="thread-1", run_id="run-3", message="补充边界")
    )
    await reopened.close()

    assert first.progress is not None
    assert inspected is not None
    assert inspected["current_stage"] != "grill"
    assert inspected["stages"][0]["state"] == "confirmed"
    assert after.progress is not None
    assert after.progress["current_stage"] != "grill"
    assert len(grilled) == 1
    assert specced
