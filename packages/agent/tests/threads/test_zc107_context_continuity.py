"""ZC-107 跨层上下文连续性验收；全部使用临时 project/home 和 typed fixture。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from harness_agent.threads.context_compaction import CompressionCheckpointDraft
from harness_agent.threads.context_lifecycle import ContextLifecycle
from harness_agent.threads.context_projection import ContextProjector
from harness_agent.threads.runtime_state import RuntimeStateSnapshot
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.extensions.skills import SkillCatalogManager
from harness_agent.threads.prompting import HISTORY_REWRITE_VERSION
from harness_agent.threads.thread_persistence import (
    AcceptRun,
    CommitContextRewrite,
    ContextArtifactDraft,
    ContextState,
    ThreadPersistence,
    TranscriptAppend,
)
from tests.support.thread_fixtures import test_binding as make_test_binding


class _NoopInteraction:
    """固定顺序测试不需要真实 Interaction transport。"""

    async def request(self, _owner: object, _run: object, _interaction: object) -> InteractionResult:
        return InteractionResult({"decision": "reject"})


def _context_spec(
    workspace: Path,
    project_fingerprint: str,
    registry: object,
    prompt: str,
) -> SimpleNamespace:
    """构造真实 ContextLifecycle 所需的最小 immutable Agent spec。"""
    return SimpleNamespace(
        project_fingerprint=project_fingerprint,
        prompt=prompt,
        effective_policy=SimpleNamespace(
            fingerprint="policy-fingerprint",
            approval_mode="auto-edit",
            isolation="local",
        ),
        tools=(),
        skill_registry=registry,
        execution=SimpleNamespace(
            mode="local",
            sandbox_enabled=False,
            approval_mode="auto-edit",
            remote=None,
        ),
        workspace=workspace,
        enable_memory=True,
        enable_skills=True,
        enable_ask_user=False,
    )


def _write_skill(manifest: Path, body: str) -> None:
    """写入测试用的最小合法 Skill，内容变化必须改变 snapshot。"""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "---\nname: review\ndescription: review skill\n---\n" + body + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_run_start_uses_one_preparation_acceptance_and_projection_order() -> None:
    """Coordinator 的生产 seam 固定为 preparation → accept → runtime/project。"""
    order: list[str] = []

    class Persistence:
        async def accept_run(self, _command: object) -> SimpleNamespace:
            order.append("accept binding+snapshot+user transcript")
            return SimpleNamespace(created=True)

    persistence = Persistence()

    async def persistence_provider() -> Persistence:
        order.append("persistence owner")
        return persistence

    async def preparation_provider(_command: StartRun, _persistence: Persistence) -> RunPreparation:
        order.append("RunPreparation")
        return RunPreparation(
            execution_binding=make_test_binding("ordered", "run-ordered")
        )

    async def runtime_provider(_run: object) -> RunRuntime:
        order.extend(("acquire AgentEngine", "construct RunContext", "ContextProjector"))

        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )
    execution = await coordinator.start(
        StartRun(thread_id="ordered", run_id="run-ordered", message="按固定顺序执行"),
        ConnectionRef("owner"),
    )
    _ = [event async for event in execution.events]
    assert order == [
        "persistence owner",
        "RunPreparation",
        "accept binding+snapshot+user transcript",
        "acquire AgentEngine",
        "construct RunContext",
        "ContextProjector",
    ]


@pytest.mark.asyncio
async def test_default_host_lifecycle_keeps_ui_transcript_and_model_projection_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Host 生命周期验证刷新、原子受理、重启恢复和有限模型投影。"""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    (workspace / ".harness").mkdir()
    (workspace / ".harness" / "AGENTS.md").write_text(
        "第一版 AGENTS 规则", encoding="utf-8"
    )
    skill_manifest = workspace / ".harness" / "skills" / "review" / "SKILL.md"
    _write_skill(skill_manifest, "第一版 Skill 正文")
    config_path = home / ".harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """[config]
version = 1

[models]
default_profile = "fixture"

[models.profiles.fixture]
provider = "openai-compatible"
provider_label = "ZC-107 fixture"
model = "fixture-model"
base_url = "https://fixture.invalid/v1"
api_key_env = "ZC104_FIXTURE_KEY"
capabilities = ["tool-calling", "streaming"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZC104_FIXTURE_KEY", "fixture-only-no-network")

    def request(method: str, params: dict[str, object], request_id: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}

    initialize = request(
        "initialize",
        {
            "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
            "client": {"name": "zc104-test", "version": "1", "kind": "test"},
            "capabilities": {
                "requests": ["run.multithread", "threads.read", "skills.read"],
                "handles": [],
            },
        },
        "init",
    )

    class FakeGraph:
        def __init__(self) -> None:
            self.contexts: list[object] = []
            self.cache_updates: list[dict[str, object]] = []
            self._answers = 0

        async def aupdate_state(
            self,
            _config: dict[str, object],
            update: dict[str, object],
            *,
            as_node: str,
        ) -> None:
            assert as_node == "model"
            self.cache_updates.append(update)

        async def astream(self, _input: object, **kwargs: object):
            self.contexts.append(kwargs["context"])
            self._answers += 1
            yield ((), "messages", (AIMessage(content=f"fixture answer {self._answers}"), {}))

    def install_fake_runtime(host: object, graph: FakeGraph, prepared: list[dict[str, object]]) -> None:
        async def acquire(run: object) -> FakeGraph:
            preparation = run.preparation  # type: ignore[attr-defined]
            profile = run.resolved_agent_engine_profile  # type: ignore[attr-defined]
            assert profile is not None
            spec = host._resolved_agent_specs[profile.profile_key]  # type: ignore[attr-defined]
            run.run_context = await host._create_run_context(  # type: ignore[attr-defined]
                run,
                profile=profile,
                spec=spec,
                execution_context=SimpleNamespace(),
            )
            snapshot = preparation.context_snapshot
            skill = preparation.requested_skill
            assert snapshot is not None and skill is not None
            prepared.append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "system_prompt": snapshot.system_prompt,
                    "skill_snapshot_id": preparation.skill_snapshot_id,
                    "skill_body": skill.body,
                    "profile_skill_catalog_fingerprint": profile.skill_catalog_fingerprint,
                    "binding_snapshot_id": preparation.execution_binding.context_snapshot_id,
                }
            )
            return graph

        host._acquire_default_agent_engine_for_run = acquire  # type: ignore[attr-defined]

    async def wait_completed(frames: list[dict[str, object]], run_id: str) -> None:
        for _ in range(300):
            if any(
                frame.get("method") == "event"
                and frame.get("params", {}).get("run_id") == run_id  # type: ignore[union-attr]
                and frame.get("params", {}).get("type") == "run.completed"  # type: ignore[union-attr]
                for frame in frames
            ):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"run {run_id} did not complete: {frames}")

    import asyncio
    from harness_agent.host.agent_host import AgentHost

    frames: list[dict[str, object]] = []
    graph = FakeGraph()
    prepared: list[dict[str, object]] = []
    host = AgentHost(config_home=home, workspace=workspace)

    async def capture(message: dict[str, object]) -> None:
        frames.append(message)

    host.send = capture  # type: ignore[method-assign]
    await host.dispatch(initialize)
    install_fake_runtime(host, graph, prepared)

    await host.dispatch(
        request(
            "run.start",
            {
                "message": "第一轮请求",
                "thread_id": "continuity-host",
                "run_id": "run-1",
                "requested_skill": {"id": "project/review", "args": ""},
            },
            "start-1",
        )
    )
    await wait_completed(frames, "run-1")
    assert prepared[0]["skill_body"] == "第一版 Skill 正文"
    assert "第一版 AGENTS 规则" in str(prepared[0]["system_prompt"])
    assert prepared[0]["binding_snapshot_id"] == prepared[0]["snapshot_id"]

    await host.dispatch(
        request("threads.open", {"thread_id": "continuity-host"}, "open-1")
    )
    opened_first = next(frame["result"] for frame in frames if frame.get("id") == "open-1")
    assert [item["content"] for item in opened_first["messages"]] == [  # type: ignore[index]
        "第一轮请求",
        "fixture answer 1",
    ]

    (workspace / ".harness" / "AGENTS.md").write_text(
        "第二版 AGENTS 规则", encoding="utf-8"
    )
    _write_skill(skill_manifest, "第二版 Skill 正文")
    await host.dispatch(
        request(
            "run.start",
            {
                "message": "第二轮请求",
                "thread_id": "continuity-host",
                "run_id": "run-2",
                "requested_skill": {"id": "project/review", "args": ""},
            },
            "start-2",
        )
    )
    await wait_completed(frames, "run-2")
    assert prepared[1]["skill_snapshot_id"] != prepared[0]["skill_snapshot_id"]
    assert prepared[1]["profile_skill_catalog_fingerprint"] != prepared[0]["profile_skill_catalog_fingerprint"]
    assert "第二版 AGENTS 规则" in str(prepared[1]["system_prompt"])
    assert prepared[1]["skill_body"] == "第二版 Skill 正文"
    assert "第一版 AGENTS 规则" in str(prepared[0]["system_prompt"])
    assert "第一版 Skill 正文" not in str(prepared[1]["system_prompt"])

    persistence = host._thread_persistence
    assert persistence is not None
    first_snapshot = await persistence.load_context_snapshot(
        str(prepared[0]["snapshot_id"]), thread_id="continuity-host"
    )
    assert "第一版 AGENTS 规则" in first_snapshot.system_prompt
    cursor = await persistence._connection.execute(  # type: ignore[attr-defined]
        """
        SELECT run_id, context_snapshot_id
        FROM harness_run_execution_bindings
        WHERE project_fingerprint = ? AND thread_id = ?
        ORDER BY created_at_ms ASC, run_id ASC
        """,
        (persistence.project_fingerprint, "continuity-host"),
    )
    binding_rows = await cursor.fetchall()
    await cursor.close()
    assert [(str(row[0]), str(row[1])) for row in binding_rows] == [
        ("run-1", str(prepared[0]["snapshot_id"])),
        ("run-2", str(prepared[1]["snapshot_id"])),
    ]

    await host.close()

    restarted_frames: list[dict[str, object]] = []
    restarted_graph = FakeGraph()
    restarted_prepared: list[dict[str, object]] = []
    restarted = AgentHost(config_home=home, workspace=workspace)

    async def capture_restarted(message: dict[str, object]) -> None:
        restarted_frames.append(message)

    restarted.send = capture_restarted  # type: ignore[method-assign]
    await restarted.dispatch(initialize)
    install_fake_runtime(restarted, restarted_graph, restarted_prepared)
    await restarted.dispatch(
        request("threads.open", {"thread_id": "continuity-host"}, "open-restart")
    )
    opened_restart = next(
        frame["result"] for frame in restarted_frames if frame.get("id") == "open-restart"
    )
    assert [item["content"] for item in opened_restart["messages"]] == [  # type: ignore[index]
        "第一轮请求",
        "fixture answer 1",
        "第二轮请求",
        "fixture answer 2",
    ]

    await restarted.dispatch(
        request(
            "run.start",
            {
                "message": "重启后请求",
                "thread_id": "continuity-host",
                "run_id": "run-3",
                "requested_skill": {"id": "project/review", "args": ""},
            },
            "start-restart",
        )
    )
    await wait_completed(restarted_frames, "run-3")
    assert "第二版 AGENTS 规则" in str(restarted_prepared[0]["system_prompt"])
    assert restarted_prepared[0]["binding_snapshot_id"] == restarted_prepared[0]["snapshot_id"]
    projected_cache = restarted_graph.cache_updates[0]["messages"]
    assert isinstance(projected_cache, list)
    projected_contents = [
        message.content
        for message in projected_cache
        if not isinstance(message, RemoveMessage)
    ]
    assert projected_contents == [
        "第一轮请求",
        "fixture answer 1",
        "第二轮请求",
        "fixture answer 2",
    ]
    await restarted.close()


@pytest.mark.asyncio
async def test_run_boundary_refreshes_agents_and_skills_but_keeps_old_audit_snapshot(
    tmp_path: Path,
) -> None:
    """同一 Thread 的下一 Run 使用新上下文，旧 Run 仍保留旧 snapshot 和正文。"""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    (workspace / ".harness").mkdir()
    agents = workspace / ".harness" / "AGENTS.md"
    agents.write_text("第一版 AGENTS 规则", encoding="utf-8")
    manifest = workspace / ".harness" / "skills" / "review" / "SKILL.md"
    _write_skill(manifest, "第一版 Skill 正文")

    store = await ThreadPersistence.open(project=workspace, home=home)
    manager = SkillCatalogManager(workspace, home=home)
    lifecycle = ContextLifecycle(workspace, home=home)
    try:
        first_registry = manager.refresh()
        first_snapshot = lifecycle.prepare(
            thread_id="continuity",
            spec=_context_spec(
                workspace,
                store.project_fingerprint,
                first_registry,
                "核心规则 v1",
            ),
            now_ms=100,
        )
        first_binding = replace(
            make_test_binding("continuity", "run-1"),
            context_snapshot_id=first_snapshot.snapshot_id,
        )
        await store.accept_run(
            AcceptRun(
                message="重复消息",
                binding=first_binding,
                context_snapshot=first_snapshot,
            )
        )
        await store.append_transcript(
            TranscriptAppend(
                thread_id="continuity",
                record_id="assistant-1",
                kind="assistant",
                content="第一轮回答",
                run_id="run-1",
                execution_id="root-run-1",
            )
        )
        await store.complete_run("continuity")

        agents.write_text("第二版 AGENTS 规则", encoding="utf-8")
        _write_skill(manifest, "第二版 Skill 正文")
        second_registry = manager.refresh()
        second_snapshot = lifecycle.prepare(
            thread_id="continuity",
            spec=_context_spec(
                workspace,
                store.project_fingerprint,
                second_registry,
                "核心规则 v1",
            ),
            now_ms=200,
        )
        assert second_registry.snapshot_id != first_registry.snapshot_id
        assert second_snapshot.snapshot_id != first_snapshot.snapshot_id
        assert "第一版 AGENTS 规则" in first_snapshot.system_prompt
        assert "第二版 AGENTS 规则" not in first_snapshot.system_prompt
        assert "第二版 AGENTS 规则" in second_snapshot.system_prompt
        assert "第一版 Skill 正文" not in second_snapshot.system_prompt
        assert second_registry.load("project/review").body == "第二版 Skill 正文"

        second_binding = replace(
            make_test_binding("continuity", "run-2"),
            context_snapshot_id=second_snapshot.snapshot_id,
        )
        await store.accept_run(
            AcceptRun(
                message="重复消息",
                binding=second_binding,
                context_snapshot=second_snapshot,
            )
        )
        await store.complete_run("continuity")

        old_audit = await store.load_context_snapshot(
            first_snapshot.snapshot_id,
            thread_id="continuity",
        )
        assert old_audit == first_snapshot
        opened = await store.open_thread("continuity")
        assert [message.content for message in opened.messages] == [
            "重复消息",
            "第一轮回答",
            "重复消息",
        ]
        assert len([record for record in await store.load_transcript("continuity") if record.kind == "user"]) == 2
    finally:
        await store.close()

    reopened = await ThreadPersistence.open(project=workspace, home=home)
    try:
        reopened_snapshot = await reopened.load_context_snapshot(
            second_snapshot.snapshot_id,
            thread_id="continuity",
        )
        assert reopened_snapshot == second_snapshot
        assert [message.content for message in (await reopened.open_thread("continuity")).messages] == [
            "重复消息",
            "第一轮回答",
            "重复消息",
        ]
        projection = await ContextProjector(reopened).project("continuity")
        assert [message.content for message in projection.messages] == [
            "重复消息",
            "第一轮回答",
            "重复消息",
        ]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_projector_restarts_from_latest_checkpoint_plus_tail_and_runtime_state(
    tmp_path: Path,
) -> None:
    """多次投影、Artifact 和运行态重开后仍各自从正确 owner 恢复。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = await ThreadPersistence.open(project=workspace, home=tmp_path / "home")
    try:
        await store.accept_run(
            AcceptRun(
                message="原始请求",
                binding=make_test_binding("projection", "run-1"),
            )
        )
        await store.append_transcript(
            TranscriptAppend(
                thread_id="projection",
                record_id="assistant-old",
                kind="assistant",
                content="旧回答",
                run_id="run-1",
                execution_id="root-run-1",
            )
        )
        first = await store.commit_context(
            CommitContextRewrite(
                thread_id="projection",
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="full-1",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="摘要一"),),
                    source_record_sequence=2,
                    trigger="automatic",
                ),
            )
        )
        assert first.checkpoint is not None

        await store.append_transcript(
            TranscriptAppend(
                thread_id="projection",
                record_id="user-tail",
                kind="user",
                content="检查尾部",
                run_id="run-2",
                execution_id="root-run-2",
            )
        )
        runtime = RuntimeStateSnapshot(
            todos=({"content": "验证恢复", "status": "pending"},),
            execution_mode="remote-sandbox",
            approval_mode="yolo",
            context_snapshot_id="snapshot-current",
            capability_fingerprint="capability-current",
        )
        artifact_commit = await store.commit_context(
            CommitContextRewrite(
                thread_id="projection",
                artifacts=(ContextArtifactDraft(kind="history", content="可恢复原文"),),
                state=ContextState(
                    last_action="manual_full",
                    runtime_state=runtime,
                ),
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="full-2",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="摘要二"),),
                    source_record_sequence=2,
                    trigger="manual",
                ),
            )
        )
        assert artifact_commit.checkpoint is not None
        assert artifact_commit.artifacts
        assert artifact_commit.state is not None

        projection = await ContextProjector(store).project("projection")
        assert projection.checkpoint is not None
        assert projection.checkpoint.checkpoint_id == "full-2"
        assert [message.content for message in projection.messages] == [
            "摘要二",
            "检查尾部",
        ]
        opened = await store.open_thread("projection")
        assert [message.content for message in opened.messages] == [
            "原始请求",
            "旧回答",
            "检查尾部",
        ]
        assert await store.load_context_state("projection") == ContextState(
            last_action="manual_full",
            runtime_state=runtime,
        )
        artifact = await store.load_context_artifact(
            "projection",
            artifact_commit.artifacts[0].artifact_id,
        )
        assert artifact is not None and artifact.content == "可恢复原文"
    finally:
        await store.close()

    reopened = await ThreadPersistence.open(
        project=workspace,
        home=tmp_path / "home",
    )
    try:
        projection = await ContextProjector(reopened).project("projection")
        assert projection.checkpoint is not None
        assert projection.checkpoint.checkpoint_id == "full-2"
        assert [message.content for message in projection.messages] == [
            "摘要二",
            "检查尾部",
        ]
        assert (await reopened.open_thread("projection")).messages[-1].content == "检查尾部"
        assert (await reopened.load_context_state("projection")).runtime_state == RuntimeStateSnapshot(
            todos=({"content": "验证恢复", "status": "pending"},),
            execution_mode="remote-sandbox",
            approval_mode="yolo",
            context_snapshot_id="snapshot-current",
            capability_fingerprint="capability-current",
        )
    finally:
        await reopened.close()
