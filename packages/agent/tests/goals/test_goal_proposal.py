"""明确 Goal proposal 的零工具解析与 Run adapter 行为。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from harness_agent.config.config import ModelSettings
from harness_agent.extensions.providers.harness_gateway import create_openai_compatible_model
from harness_agent.goals.proposal import (
    GoalClarification,
    GoalDraft,
    GoalDraftOutput,
    GoalProposalContext,
    GoalProposalServices,
    generate_goal_draft,
)
from harness_agent.goals.models import GoalStoreError
from harness_agent.goals.repository import (
    GOAL_MAX_REPOSITORY_CONTEXT_CHARS,
    GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS,
    GOAL_MAX_REPOSITORY_TOOL_CALLS,
    GoalRepositoryBudget,
    create_goal_repository_tools,
)
from harness_agent.host.goal_proposal import GoalProposalRunAdapter
from harness_agent.host.run_coordinator import GoalProposalRunInput, InteractionResult
from harness_agent.threads.thread_persistence import ThreadPersistence


def test_goal_draft_output_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GoalDraftOutput.model_validate(
            {
                "readiness": "ready",
                "objective": "完成协议升级",
                "assumptions": [],
                "criteria": ["契约测试通过"],
                "extra": True,
            }
        )


def test_goal_draft_output_accepts_ready_draft_with_unused_clarification_fields() -> None:
    parsed = GoalDraftOutput.model_validate(
        {
            "readiness": "ready",
            "objective": "从0到1构建 TaskPulse 看板服务",
            "assumptions": ["沿用 Bun.serve"],
            "criteria": ["静态单页可打开", "REST API 可创建完成与标签过滤", "附带单元测试"],
            "understood_objective": "",
            "missing_information": [],
            "questions": [],
        }
    )

    assert parsed.readiness == "ready"
    assert parsed.objective == "从0到1构建 TaskPulse 看板服务"
    assert parsed.criteria == (
        "静态单页可打开",
        "REST API 可创建完成与标签过滤",
        "附带单元测试",
    )
    assert parsed.understood_objective is None
    assert parsed.missing_information == ()
    assert parsed.questions == ()


def test_goal_draft_output_accepts_ready_draft_when_understood_objective_is_copied() -> None:
    parsed = GoalDraftOutput.model_validate(
        {
            "readiness": "ready",
            "objective": "完成计时器",
            "assumptions": [],
            "criteria": ["页面显示 60 秒"],
            "understood_objective": "完成计时器",
            "missing_information": [],
            "questions": [],
        }
    )

    assert parsed.readiness == "ready"
    assert parsed.objective == "完成计时器"
    assert parsed.understood_objective is None


def test_goal_draft_output_accepts_clarification_draft_with_unused_ready_fields() -> None:
    parsed = GoalDraftOutput.model_validate(
        {
            "readiness": "needs_clarification",
            "objective": "",
            "assumptions": [],
            "criteria": [],
            "understood_objective": "改进登录",
            "missing_information": ["交付物不明确"],
            "questions": ["需要交付哪些登录行为？"],
        }
    )

    assert parsed.readiness == "needs_clarification"
    assert parsed.understood_objective == "改进登录"
    assert parsed.objective is None
    assert parsed.criteria == ()


def test_goal_draft_output_rejects_ready_draft_without_criteria() -> None:
    with pytest.raises(ValidationError, match="incomplete"):
        GoalDraftOutput.model_validate(
            {
                "readiness": "ready",
                "objective": "完成计时器",
                "assumptions": [],
                "criteria": [],
                "understood_objective": "",
                "missing_information": [],
            }
        )


@pytest.mark.asyncio
async def test_generate_goal_draft_forces_structured_output() -> None:
    structured = _StructuredModel()
    model = _ProposalModel(structured)

    draft = await generate_goal_draft(model, GoalProposalContext(input_text="完成计时器"))

    assert model.schema is GoalDraftOutput
    assert model.method == "function_calling"
    assert model.include_raw is True
    assert draft.objective == "完成计时器"
    assert draft.criteria == ("页面显示 60 秒",)
    assert "<goal-input>完成计时器</goal-input>" in structured.prompt


@pytest.mark.asyncio
async def test_generate_goal_draft_uses_forced_schema_on_openai_compatible_wire() -> None:
    requests: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads((await request.aread()).decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "GoalDraftOutput",
                                        "arguments": json.dumps(
                                            {
                                                "readiness": "ready",
                                                "objective": "完成计时器",
                                                "assumptions": [],
                                                "criteria": ["页面显示 60 秒"],
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        model = create_openai_compatible_model(
            ModelSettings(
                name="mock",
                base_url="https://example.invalid/v1",
                api_key="test",
            ),
            async_client=client,
        )
        draft = await generate_goal_draft(model, GoalProposalContext(input_text="完成计时器"))
    finally:
        await client.aclose()

    assert draft.criteria == ("页面显示 60 秒",)
    assert requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "GoalDraftOutput"},
    }


@pytest.mark.asyncio
async def test_generate_goal_draft_accepts_ready_tool_payload_with_unused_fields() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "GoalDraftOutput",
                                        "arguments": json.dumps(
                                            {
                                                "readiness": "ready",
                                                "objective": "从0到1构建 TaskPulse 看板服务",
                                                "assumptions": [],
                                                "criteria": [
                                                    "静态单页可打开",
                                                    "REST API 可创建完成与标签过滤",
                                                    "附带单元测试",
                                                ],
                                                "understood_objective": "",
                                                "missing_information": [],
                                                "questions": [],
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        model = create_openai_compatible_model(
            ModelSettings(
                name="mock",
                base_url="https://example.invalid/v1",
                api_key="test",
            ),
            async_client=client,
        )
        draft = await generate_goal_draft(
            model,
            GoalProposalContext(input_text="从0到1构建 TaskPulse 看板服务"),
        )
    finally:
        await client.aclose()

    assert draft.objective == "从0到1构建 TaskPulse 看板服务"
    assert draft.criteria == (
        "静态单页可打开",
        "REST API 可创建完成与标签过滤",
        "附带单元测试",
    )


@pytest.mark.asyncio
async def test_generate_goal_draft_maps_structured_output_validation_error() -> None:
    from langchain.agents.structured_output import StructuredOutputValidationError
    from langchain_core.messages import AIMessage

    class _Factory:
        def __call__(self, _model, _tools, **_kwargs):
            class _Agent:
                async def ainvoke(self, _input, **_kwargs):
                    raise StructuredOutputValidationError(
                        "GoalDraftOutput",
                        ValueError("ready goal draft is incomplete or mixed"),
                        AIMessage(content=""),
                    )

            return _Agent()

    with pytest.raises(GoalStoreError, match="GOAL_OBJECTIVE_INVALID"):
        await generate_goal_draft(
            object(),
            GoalProposalContext(input_text="完成计时器"),
            repository_tools=(object(),),
            agent_factory=_Factory(),
        )


class _StructuredModel:
    def __init__(self) -> None:
        self.prompt = ""

    async def ainvoke(self, messages):
        self.prompt = messages[0].content
        return {
            "raw": SimpleNamespace(content=[{"type": "text", "text": "ignored"}]),
            "parsed": GoalDraftOutput(
                readiness="ready",
                objective="完成计时器",
                assumptions=[],
                criteria=["页面显示 60 秒"],
            ),
            "parsing_error": None,
        }


class _ProposalModel:
    def __init__(self, structured: _StructuredModel) -> None:
        self.structured = structured
        self.schema = None
        self.method = None
        self.include_raw = None

    def with_structured_output(self, schema, *, method, include_raw):
        self.schema = schema
        self.method = method
        self.include_raw = include_raw
        return self.structured


@pytest.mark.asyncio
async def test_generate_goal_draft_returns_bounded_clarification() -> None:
    structured = _StructuredClarificationModel()
    model = _ProposalModel(structured)

    result = await generate_goal_draft(
        model,
        GoalProposalContext(
            input_text="把它做好",
            recent_messages=("用户：请修改登录页",),
        ),
    )

    assert result == GoalClarification(
        understood_objective="修改登录页",
        missing_information=("交付物不明确",),
        questions=("你希望修改登录页的哪一部分？",),
    )
    assert "最近对话" in structured.prompt
    assert "最多 3 个" in structured.prompt


@pytest.mark.asyncio
async def test_generate_goal_draft_uses_bounded_read_only_repository_tools(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("目标说明\n", encoding="utf-8")
    budget = GoalRepositoryBudget()
    tools = create_goal_repository_tools(tmp_path, budget)
    factory = _RepositoryAgentFactory()

    draft = await generate_goal_draft(
        object(),
        GoalProposalContext(input_text="完成 README 中的要求"),
        repository_tools=tools,
        repository_budget=budget,
        agent_factory=factory,
    )

    assert draft.objective == "完成 README 中的要求"
    assert factory.tool_names == ["ls", "read_file", "glob", "grep"]
    assert budget.calls == 1
    assert budget.result_chars == len("目标说明\n")


def test_goal_repository_budget_and_workspace_boundary_fail_closed(tmp_path: Path) -> None:
    budget = GoalRepositoryBudget()
    for _ in range(GOAL_MAX_REPOSITORY_TOOL_CALLS):
        budget.consume("x")
    with pytest.raises(GoalStoreError, match="GOAL_REPOSITORY_TOOL_LIMIT"):
        budget.consume("x")

    context_budget = GoalRepositoryBudget(result_chars=GOAL_MAX_REPOSITORY_CONTEXT_CHARS)
    with pytest.raises(GoalStoreError, match="GOAL_REPOSITORY_CONTEXT_LIMIT"):
        context_budget.consume("x")

    boundary_budget = GoalRepositoryBudget()
    read_tool = create_goal_repository_tools(tmp_path, boundary_budget)[1]
    with pytest.raises(GoalStoreError, match="GOAL_REPOSITORY_TOOL_FORBIDDEN"):
        read_tool.invoke({"file_path": "../outside", "offset": 0, "limit": 10})


def test_goal_repository_oversized_query_returns_recoverable_bounded_error(
    tmp_path: Path,
) -> None:
    for index in range(180):
        (tmp_path / f"goal-context-{index:03d}-{'x' * 64}.md").write_text(
            "目标上下文\n",
            encoding="utf-8",
        )
    budget = GoalRepositoryBudget()
    glob_tool = create_goal_repository_tools(tmp_path, budget)[2]

    result = glob_tool.invoke({"pattern": "**/*.md", "path": "/"})

    assert result == (
        "ERROR: repository result exceeds 12000 characters; "
        "narrow the path, pattern, or glob"
    )
    assert len(result) <= GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS
    assert budget.calls == 1
    assert budget.result_chars == len(result)
    assert budget.limited_results == 1
    assert budget.last_tool == "glob"
    assert budget.last_result_chars > GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS
    assert budget.diagnostic_fields() == {
        "repository_calls": 1,
        "repository_result_chars": len(result),
        "repository_limited_results": 1,
        "last_repository_tool": "glob",
        "last_repository_result_chars": budget.last_result_chars,
    }
    assert budget.violation is None
    budget.validate()


def test_goal_repository_broad_scan_skips_external_symlink_without_reading_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.md").write_text("可见目标\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("外部秘密\n", encoding="utf-8")
    (workspace / "outside-link.md").symlink_to(outside)
    budget = GoalRepositoryBudget()
    tools = {tool.name: tool for tool in create_goal_repository_tools(workspace, budget)}

    paths = tools["glob"].invoke({"pattern": "**/*.md", "path": "/"})
    matches = tools["grep"].invoke(
        {"pattern": "目标|秘密", "path": "/", "glob": "**/*.md"}
    )

    assert paths == "/inside.md"
    assert matches == "/inside.md:1:可见目标"
    with pytest.raises(GoalStoreError, match="GOAL_REPOSITORY_TOOL_FORBIDDEN"):
        tools["read_file"].invoke(
            {"file_path": "/outside-link.md", "offset": 0, "limit": 10}
        )


class _StructuredClarificationModel(_StructuredModel):
    async def ainvoke(self, messages):
        self.prompt = messages[0].content
        return {
            "parsed": GoalDraftOutput(
                readiness="needs_clarification",
                understood_objective="修改登录页",
                missing_information=["交付物不明确"],
                questions=["你希望修改登录页的哪一部分？"],
            ),
        }


class _RepositoryAgentFactory:
    def __init__(self) -> None:
        self.tool_names: list[str] = []

    def __call__(self, _model, tools, **_kwargs):
        self.tool_names = [tool.name for tool in tools]
        read_tool = next(tool for tool in tools if tool.name == "read_file")

        class _Agent:
            async def ainvoke(self, _input, **_kwargs):
                read_tool.invoke({"file_path": "/README.md", "offset": 0, "limit": 20})
                return {
                    "structured_response": GoalDraftOutput(
                        readiness="ready",
                        objective="完成 README 中的要求",
                        assumptions=(),
                        criteria=("README 中的可观察要求已满足",),
                    )
                }

        return _Agent()


class _ProposalPort:
    def __init__(self, response: dict[str, object], reservation: "_Reservation") -> None:
        self.response = response
        self.reservation = reservation
        self.events: list[tuple[str, dict[str, object]]] = []
        self.started = False

    def emit(self, _run, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, payload))

    def mark_running(self, _run) -> None:
        pass

    async def start_execution(self, _run) -> None:
        self.started = True

    async def release_preparation_snapshot(self, _run) -> None:
        await self.reservation.release()

    async def request_interaction(self, _run, interaction) -> InteractionResult:
        assert self.reservation.released is True
        assert interaction.type == "goal"
        assert interaction.payload["criteria"] == ["契约测试通过"]
        return InteractionResult(self.response)


class _Reservation:
    def __init__(self) -> None:
        self.released = False
        self.release_count = 0

    async def release(self) -> None:
        self.released = True
        self.release_count += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_criterion", "expected_status"),
    [
        ({"decision": "accepted"}, "契约测试通过", "completed"),
        ({"decision": "edited", "criteria": ["协议和类型检查通过"]}, "协议和类型检查通过", "completed"),
        ({"decision": "cancelled"}, None, "cancelled"),
    ],
)
async def test_proposal_adapter_accepts_edits_or_cancels_without_transcript(
    tmp_path: Path,
    response: dict[str, object],
    expected_criterion: str | None,
    expected_status: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成协议升级",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )

        reservation = _Reservation()

        async def draft(_input_text: str) -> GoalDraft:
            assert reservation.released is True
            return GoalDraft("完成协议升级", (), ("契约测试通过",))

        adapter = GoalProposalRunAdapter(
            lambda _run: _async_value(GoalProposalServices(store, draft, lambda: 2))
        )
        run = SimpleNamespace(
            start=SimpleNamespace(input=GoalProposalRunInput("request-1")),
            thread_id="thread-1",
            run_id="run-1",
            context_summary={},
            preparation=SimpleNamespace(snapshot_reservation=reservation),
        )
        port = _ProposalPort(response, reservation)
        outcome = await adapter.execute(run, port)
        assert reservation.release_count == 1
        snapshot = await store.inspect("thread-1")
        assert port.started is True
        assert (outcome.status if outcome else "completed") == expected_status
        if expected_criterion is None:
            assert snapshot.goal is None
        else:
            assert snapshot.goal is not None
            assert snapshot.goal.criteria[0].text == expected_criterion
            assert run.context_summary["goal_continuation"]["goal_id"] == snapshot.goal.goal_id
            assert port.events[-1][0] == "goal.changed"
        assert (await persistence.open_thread("thread-1")).messages == ()
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_proposal_adapter_clarifies_then_reviews_in_same_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="把登录做好",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        seen: list[GoalProposalContext] = []

        async def draft(context: GoalProposalContext):
            seen.append(context)
            if not context.clarifications:
                return GoalClarification(
                    understood_objective="改进登录",
                    missing_information=("交付物不明确",),
                    questions=("需要交付哪些登录行为？",),
                )
            return GoalDraft("完善登录", (), ("登录成功进入首页",))

        reservation = _Reservation()
        port = _MultiStepProposalPort(
            goal_responses=[{"decision": "accepted"}],
            question_responses=[{"answers": {"goal-question-1-1": ["登录与错误提示"]}}],
            reservation=reservation,
        )
        adapter = GoalProposalRunAdapter(
            lambda _run: _async_value(GoalProposalServices(store, draft, lambda: 2))
        )
        run = _proposal_run(reservation)

        outcome = await adapter.execute(run, port)

        assert outcome is None
        assert seen[1].clarifications == (
            ("需要交付哪些登录行为？", "登录与错误提示"),
        )
        assert port.question_count == 1
        assert (await store.inspect("thread-1")).goal is not None
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_proposal_adapter_rejected_feedback_regenerates_before_accept(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成登录",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        contexts: list[GoalProposalContext] = []

        async def draft(context: GoalProposalContext):
            contexts.append(context)
            criterion = "覆盖错误密码" if context.feedback else "登录成功"
            return GoalDraft("完成登录", (), (criterion,))

        reservation = _Reservation()
        port = _MultiStepProposalPort(
            goal_responses=[
                {"decision": "rejected", "feedback": "还要覆盖错误密码"},
                {"decision": "accepted"},
            ],
            question_responses=[],
            reservation=reservation,
        )
        adapter = GoalProposalRunAdapter(
            lambda _run: _async_value(GoalProposalServices(store, draft, lambda: 2))
        )

        outcome = await adapter.execute(_proposal_run(reservation), port)

        assert outcome is None
        assert contexts[1].feedback == "还要覆盖错误密码"
        goal = (await store.inspect("thread-1")).goal
        assert goal is not None
        assert goal.criteria[0].text == "覆盖错误密码"
        assert port.goal_review_count == 2
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_proposal_adapter_resumes_reviewing_draft_without_regeneration(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成登录",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        await store.save_proposal(
            request_id="request-1",
            objective="完成登录",
            assumptions=("沿用现有会话",),
            criteria=("登录成功进入首页",),
            now_ms=2,
        )
        draft_calls = 0

        async def draft(_context: GoalProposalContext):
            nonlocal draft_calls
            draft_calls += 1
            return GoalDraft("不应重新生成", (), ("不应出现",))

        reservation = _Reservation()
        port = _MultiStepProposalPort(
            goal_responses=[{"decision": "accepted"}],
            question_responses=[],
            reservation=reservation,
        )
        adapter = GoalProposalRunAdapter(
            lambda _run: _async_value(GoalProposalServices(store, draft, lambda: 3))
        )

        outcome = await adapter.execute(_proposal_run(reservation), port)

        assert outcome is None
        assert draft_calls == 0
        goal = (await store.inspect("thread-1")).goal
        assert goal is not None
        assert goal.objective == "完成登录"
        assert goal.criteria[0].text == "登录成功进入首页"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_proposal_adapter_fails_after_two_clarification_rounds(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="把它做好",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )

        async def draft(_context: GoalProposalContext):
            return GoalClarification(
                understood_objective="仍不明确",
                missing_information=("缺少交付物",),
                questions=("具体交付什么？",),
            )

        reservation = _Reservation()
        port = _MultiStepProposalPort(
            goal_responses=[],
            question_responses=[
                {"answers": {"goal-question-1-1": ["还不确定"]}},
                {"answers": {"goal-question-2-1": ["仍不确定"]}},
            ],
            reservation=reservation,
        )
        adapter = GoalProposalRunAdapter(
            lambda _run: _async_value(GoalProposalServices(store, draft, lambda: 2))
        )

        outcome = await adapter.execute(_proposal_run(reservation), port)

        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.code == "GOAL_OBJECTIVE_UNCLEAR"
        pending = (await store.inspect("thread-1")).pending
        assert pending is not None
        assert pending.status == "failed"
        assert pending.error_code == "GOAL_OBJECTIVE_UNCLEAR"
    finally:
        await persistence.close()


class _MultiStepProposalPort(_ProposalPort):
    def __init__(
        self,
        *,
        goal_responses: list[dict[str, object]],
        question_responses: list[dict[str, object]],
        reservation: _Reservation,
    ) -> None:
        super().__init__({}, reservation)
        self.goal_responses = goal_responses
        self.question_responses = question_responses
        self.goal_review_count = 0
        self.question_count = 0

    async def request_interaction(self, _run, interaction) -> InteractionResult:
        assert interaction.type == "goal"
        self.goal_review_count += 1
        return InteractionResult(self.goal_responses.pop(0))

    async def request_question(
        self,
        _run,
        *,
        request_id: str,
        interrupt_id: str,
        questions: list[dict[str, object]],
    ) -> InteractionResult:
        del request_id, interrupt_id
        self.question_count += 1
        assert 1 <= len(questions) <= 3
        return InteractionResult(self.question_responses.pop(0))


def _proposal_run(reservation: _Reservation):
    return SimpleNamespace(
        start=SimpleNamespace(input=GoalProposalRunInput("request-1")),
        thread_id="thread-1",
        run_id="run-1",
        context_summary={},
        preparation=SimpleNamespace(snapshot_reservation=reservation),
    )


async def _async_value(value):
    return value
