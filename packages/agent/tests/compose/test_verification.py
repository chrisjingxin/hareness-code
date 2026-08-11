"""Compose Verify 阶段测试：fresh evidence、fix loop、预算与安全边界。

工作流级测试使用 fake VerificationPort 走真实 RunCoordinator；Managed
VerificationPort 单元测试覆盖 Policy/Approval/并发锁/backend 边界。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from harness_agent.compose.models import VerificationEvidence
from harness_agent.compose.stage_agents import StageRequest, StageResult
from harness_agent.compose.verification import (
    ManagedVerificationPort,
    VerificationError,
    VerificationRequest,
    WorkspaceChangesSnapshot,
)
from harness_agent.compose.workflow import ComposeServices
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.threads.thread_persistence import ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding


def _understanding() -> dict[str, Any]:
    return {
        "goal": "实现搜索",
        "constraints": [],
        "acceptance": ["搜索结果可排序"],
        "out_of_scope": [],
        "open_decisions": [],
        "change_kind": "feature",
    }


def _plan() -> dict[str, Any]:
    return {
        "solution": "新增 search module",
        "tasks": [
            {
                "id": "task-1",
                "title": "实现搜索",
                "kind": "behavior",
                "acceptance": "搜索返回结果",
                "depends_on": [],
                "verification_commands": ["pytest -q tests/test_search.py"],
            }
        ],
        "relevant_pointers": ["src/search.py"],
    }


def _task_result(*, task_id: str = "task-1", **overrides: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "changed_paths": ["src/search.py"],
        "focused_test_evidence": "pytest -q tests/test_search.py 通过",
        "red_evidence": "先写测试：test_search 失败（RED）",
        "remaining_issue": "",
        **overrides,
    }


def _reviewer() -> dict[str, Any]:
    return {"verdict": "pass", "findings": []}


def _evidence(*, command: str = "pytest -q tests/test_search.py", exit_code: int = 0) -> VerificationEvidence:
    return VerificationEvidence(
        command=command,
        working_dir="packages/agent",
        started_at_ms=1,
        finished_at_ms=2,
        exit_code=exit_code,
        output_digest="a" * 64,
        output_summary="3 passed" if exit_code == 0 else "1 failed",
        truncated=False,
    )


class _FakeStageAgent:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.tasks: list[str] = []

    async def run(self, request: StageRequest) -> StageResult:
        self.calls.append(request.stage)
        self.tasks.append(request.task)
        item = self.script.pop(0)
        if not isinstance(item, dict):
            raise ValueError("STAGE_OUTPUT_NOT_OBJECT")
        return StageResult(
            execution_id=f"exec-{len(self.calls)}",
            agent_id=request.stage,
            status="completed",
            output=item,
        )


class _FakeVerification:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[VerificationRequest] = []

    async def run(self, request: VerificationRequest) -> VerificationEvidence:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def capture_workspace_changes(
        self, _resource_key: str
    ) -> WorkspaceChangesSnapshot:
        return WorkspaceChangesSnapshot(
            status_summary=" M src/search.py",
            diff="diff --git a/src/search.py b/src/search.py\n+change",
        )


class _ScriptedInteraction:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def request(self, _owner, _run, interaction) -> InteractionResult:
        self.requests.append(interaction)
        return InteractionResult(self.responses.pop(0))


async def _run_compose(
    tmp_path: Path,
    stage_script: list[dict[str, Any]],
    verification_script: list[Any],
    interaction_responses: list[dict[str, Any]],
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    stage_agent = _FakeStageAgent(stage_script)
    verification = _FakeVerification(verification_script)
    interactions = _ScriptedInteraction(interaction_responses)

    async def persistence_provider() -> ThreadPersistence:
        return persistence

    async def preparation_provider(_command, _persistence) -> RunPreparation:
        return RunPreparation(
            execution_binding=make_test_binding("thread-1", "run-1"),
        )

    async def noop_runtime(_run) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    async def compose_services() -> ComposeServices | None:
        return ComposeServices(
            stage_agent=stage_agent,
            method_assets={
                "understand": "u", "plan": "p", "build": "BUILD-METHOD",
                "tdd": "TDD-METHOD", "debug": "DEBUG-METHOD",
                "code-review": "REVIEW-METHOD",
            },
            workspace_root=str(project),
            verification=verification,
            now_ms=lambda: 42,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=interactions,
        compose_services_provider=compose_services,
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-1", run_id="run-1", message="实现搜索功能", mode="compose"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    store = persistence.compose_artifact_store()
    stored_state = await store.load_run("run-1")
    anchor = (
        await store.load_artifact("run-1", stored_state.verification_evidence_id)
        if stored_state is not None and stored_state.verification_evidence_id
        else None
    )
    await coordinator.close()
    await persistence.close()
    return events, stage_agent, verification, interactions, stored_state, anchor


def _state_frames(events) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.type == "compose.state"]


async def test_verify_pass_reaches_review_boundary_with_evidence(tmp_path: Path) -> None:
    """全部命令 fresh pass 后进入 Review；evidence 进入 projection 并完成。"""
    events, stage_agent, verification, interactions, stored_state, anchor = await _run_compose(
        tmp_path,
        [
            _understanding(), _plan(), _task_result(task_id="task-1"),
            _reviewer(), _reviewer(),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    frames = _state_frames(events)
    assert frames[-1]["stage"] == "review"
    assert frames[-1]["evidence"] == [
        {"label": "pytest -q tests/test_search.py", "status": "passed"}
    ]
    assert events[-1].type == "run.completed"
    assert [request.command for request in verification.requests] == [
        "pytest -q tests/test_search.py"
    ]
    assert stored_state is not None
    assert anchor is not None
    assert anchor.artifact_id == stored_state.verification_evidence_id
    assert anchor.kind.value == "verification"


async def test_verify_fail_creates_fix_task_and_reloops(tmp_path: Path) -> None:
    """验证失败生成来源明确的 fix task，经 Build 修复后重新 Verify。"""
    events, stage_agent, verification, interactions, stored_state, anchor = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="fix-verify-1"),
            _reviewer(),
            _reviewer(),
        ],
        [_evidence(exit_code=1), _evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    # fix task 在第二阶段被执行并完成。
    assert any("fix-verify-1" in text for text in stage_agent.tasks)
    frames = _state_frames(events)
    assert frames[-1]["stage"] == "review"
    assert [task["id"] for task in frames[-1]["tasks"]] == ["task-1", "fix-verify-1"]
    assert frames[-1]["tasks"][1]["status"] == "passed"
    assert [frame["status"] for frame in frames].count("waiting_user") == 1
    # 只执行了一个 fix 轮，双轴 Review 后完成。
    assert events[-1].type == "run.completed"
    reviewer_packs = [
        task
        for stage, task in zip(stage_agent.calls, stage_agent.tasks, strict=True)
        if stage in {"requirement-reviewer", "code-reviewer"}
    ]
    assert reviewer_packs
    assert all("exit 1" not in pack for pack in reviewer_packs)
    assert all(pack.count("exit 0") == 1 for pack in reviewer_packs)


async def test_verify_fix_budget_exhausted_blocks(tmp_path: Path) -> None:
    """Verify 两轮 fix 后仍失败进入 blocked，保留失败 evidence。"""
    events, stage_agent, verification, interactions, stored_state, anchor = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="fix-verify-1"),
            _task_result(task_id="fix-verify-2"),
        ],
        [
            _evidence(exit_code=1),
            _evidence(exit_code=1),
            _evidence(exit_code=1),
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"
    frames = _state_frames(events)
    assert frames[-1]["status"] == "blocked"
    assert "verify" in (frames[-1]["blocked_reason"] or "")
    # 两轮 fix 都已执行完成，第三次 Verify 失败后 blocked。
    assert [task["status"] for task in frames[-1]["tasks"]] == ["passed", "passed", "passed"]
    assert frames[-1]["evidence"][-1]["status"] == "failed"


async def test_verify_policy_denied_blocks_without_fix_loop(tmp_path: Path) -> None:
    """策略拒绝/用户拒绝的验证命令不可修复，直接 blocked。"""
    events, stage_agent, verification, interactions, stored_state, anchor = await _run_compose(
        tmp_path,
        [_understanding(), _plan(), _task_result(task_id="task-1")],
        [VerificationError("POLICY_DENIED", "denied")],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"
    frames = _state_frames(events)
    assert "POLICY_DENIED" in (frames[-1]["blocked_reason"] or "")
    assert len(stage_agent.tasks) == 3  # 没有生成 fix task，不再调用 Builder


# ---------- ManagedVerificationPort 单元测试 ----------


class _FakeBackend:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.commands: list[str] = []

    def execute(self, command: str, *, timeout: int | None = None):
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.response


class _ScriptedBackend:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.commands: list[str] = []

    def execute(self, command: str, *, timeout: int | None = None):
        self.commands.append(command)
        return self.responses.pop(0)


class _FakeLease:
    def __init__(self, backend: Any, workspace_path: str = "packages/agent") -> None:
        self.value = type("Context", (), {"backend": backend, "workspace_path": workspace_path})()
        self.released = False

    async def release(self) -> None:
        self.released = True


class _FakePool:
    def __init__(self, lease: _FakeLease) -> None:
        self.lease = lease
        self.keys: list[str] = []

    async def acquire(self, key: str, _settings, _workspace):
        self.keys.append(key)
        return self.lease


class _FailingPool:
    async def acquire(self, _key: str, _settings, _workspace):
        raise RuntimeError("sandbox unavailable")


class _FakeLock:
    def __init__(self) -> None:
        self.read_count = 0
        self.read_release_count = 0
        self.write_count = 0
        self.release_count = 0

    async def acquire_read(self) -> None:
        self.read_count += 1

    async def release_read(self) -> None:
        self.read_release_count += 1

    async def acquire_write(self) -> None:
        self.write_count += 1

    async def release_write(self) -> None:
        self.release_count += 1


def _port(*, backend: Any, rules: list[Any] | None = None, lock: _FakeLock | None = None) -> tuple[ManagedVerificationPort, _FakeLease, _FakeLock]:
    lease = _FakeLease(backend)
    pool = _FakePool(lease)
    rwlock = lock or _FakeLock()
    port = ManagedVerificationPort(
        pool=pool,  # type: ignore[arg-type]
        settings=type("S", (), {"sandbox_enabled": False})(),  # type: ignore[arg-type]
        workspace=Path("."),
        rules_provider=lambda: rules or [],
        rwlock=rwlock,
        now_ms=lambda: 7,
    )
    return port, lease, rwlock


@pytest.mark.asyncio
async def test_managed_port_records_fresh_evidence_for_passing_command() -> None:
    """通过命令产生带 digest 与时间戳的 fresh evidence。"""
    backend = _FakeBackend(response=type("R", (), {"output": "3 passed", "exit_code": 0})())
    port, lease, rwlock = _port(backend=backend)
    evidence = await port.run(VerificationRequest(
        command="git status", label="git status", resource_key="fp-1",
    ))
    assert evidence.exit_code == 0
    assert evidence.started_at_ms == 7 and evidence.finished_at_ms == 7
    assert len(evidence.output_digest) == 64
    assert rwlock.write_count == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_managed_port_deny_rule_blocks_before_execution() -> None:
    """Policy deny 规则直接拒绝，命令绝不执行。"""
    from harness_agent.policy.permission_rules import PermissionRule

    backend = _FakeBackend(response=type("R", (), {"output": "", "exit_code": 0})())
    port, lease, _ = _port(
        backend=backend,
        rules=[PermissionRule(tool="execute", resource="*", effect="deny")],
    )
    with pytest.raises(VerificationError, match="POLICY_DENIED"):
        await port.run(VerificationRequest(
            command="pytest -q tests", label="pytest", resource_key="fp-1",
        ))
    assert backend.commands == []
    assert lease.released is False


@pytest.mark.asyncio
async def test_managed_port_safe_command_skips_approval() -> None:
    """安全白名单命令不弹窗；unsafe 命令必须请求批准。"""
    backend = _FakeBackend(response=type("R", (), {"output": "", "exit_code": 0})())
    approvals: list[str] = []

    async def approve(description: str) -> bool:
        approvals.append(description)
        return True

    port, _, _ = _port(backend=backend)
    await port.run(VerificationRequest(
        command="git status", label="git status", resource_key="fp-1",
        approve=approve,
    ))
    assert approvals == []  # 未走到审批回调


@pytest.mark.asyncio
async def test_managed_port_unsafe_command_requires_approval() -> None:
    """不在白名单的命令必须经用户批准；拒绝则 VERIFICATION_DENIED。"""
    backend = _FakeBackend(response=type("R", (), {"output": "", "exit_code": 0})())
    port, lease, _ = _port(backend=backend)

    async def reject(_description: str) -> bool:
        return False

    with pytest.raises(VerificationError, match="VERIFICATION_DENIED"):
        await port.run(VerificationRequest(
            command="pytest -q tests/test_search.py",
            label="pytest",
            resource_key="fp-1",
            approve=reject,
        ))
    assert backend.commands == []
    assert lease.released is False


@pytest.mark.asyncio
async def test_managed_port_without_approval_channel_fails_closed() -> None:
    """需要审批但未提供交互通道时 fail closed，不执行命令。"""
    backend = _FakeBackend(response=type("R", (), {"output": "", "exit_code": 0})())
    port, lease, _ = _port(backend=backend)
    with pytest.raises(VerificationError, match="APPROVAL_REQUIRED"):
        await port.run(VerificationRequest(
            command="pytest -q tests/test_search.py",
            label="pytest",
            resource_key="fp-1",
        ))
    assert backend.commands == []


@pytest.mark.asyncio
async def test_managed_port_backend_failure_and_timeout_are_not_pass() -> None:
    """backend 异常与超时都产生稳定错误，不伪造 evidence。"""
    backend = _FakeBackend(error=RuntimeError("sandbox down"))
    port, _, _ = _port(backend=backend)
    with pytest.raises(VerificationError, match="BACKEND_FAILED"):
        await port.run(VerificationRequest(
            command="git status", label="git status", resource_key="fp-1",
        ))

    class _HangingBackend:
        def execute(self, command: str, *, timeout: int | None = None):
            import time

            time.sleep(timeout or 1)  # 尊重 backend timeout：到点抛超时
            raise TimeoutError(f"command timed out after {timeout}s")

    hanging_port, _, _ = _port(backend=_HangingBackend())
    with pytest.raises(VerificationError, match="VERIFICATION_TIMEOUT"):
        # execute 在 to_thread 中运行；用会阻塞的 backend 触发 wait_for 超时。
        await asyncio.wait_for(
            hanging_port.run(VerificationRequest(
                command="git status", label="git status", resource_key="fp-1",
                timeout_seconds=0.01,
            )),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_managed_port_rejects_write_outside_workspace_before_approval(
    tmp_path: Path,
) -> None:
    """验证命令不能借助审批写到工作区外。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = _FakeBackend(
        response=type("R", (), {"output": "", "exit_code": 0})()
    )
    lease = _FakeLease(backend, workspace_path=str(workspace))
    lock = _FakeLock()
    approvals: list[str] = []

    async def approve(description: str) -> bool:
        approvals.append(description)
        return True

    port = ManagedVerificationPort(
        pool=_FakePool(lease),  # type: ignore[arg-type]
        settings=type("S", (), {"sandbox_enabled": False})(),  # type: ignore[arg-type]
        workspace=workspace,
        rules_provider=lambda: [],
        rwlock=lock,
        now_ms=lambda: 7,
    )
    with pytest.raises(VerificationError, match="WORKSPACE_BOUNDARY_DENIED"):
        await port.run(
            VerificationRequest(
                command="echo changed > ../outside.txt",
                label="outside write",
                resource_key="fp-1",
                approve=approve,
            )
        )
    assert approvals == []
    assert backend.commands == []
    assert lock.write_count == 0


@pytest.mark.asyncio
async def test_managed_port_releases_write_lock_when_resource_acquire_fails() -> None:
    """执行资源获取失败不能覆盖原始错误或泄漏 Host 写锁。"""
    lock = _FakeLock()
    port = ManagedVerificationPort(
        pool=_FailingPool(),  # type: ignore[arg-type]
        settings=type("S", (), {"sandbox_enabled": False})(),  # type: ignore[arg-type]
        workspace=Path("."),
        rules_provider=lambda: [],
        rwlock=lock,
        now_ms=lambda: 7,
    )
    with pytest.raises(VerificationError, match="BACKEND_FAILED"):
        await port.run(
            VerificationRequest(
                command="git status",
                label="git status",
                resource_key="fp-1",
            )
        )
    assert lock.write_count == 1
    assert lock.release_count == 1


@pytest.mark.asyncio
async def test_managed_port_captures_bounded_real_workspace_changes(
    tmp_path: Path,
) -> None:
    """Reviewer 的变更输入来自 backend 的 Git 状态与 diff，不依赖 Builder 自报。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = _ScriptedBackend(
        [
            type("R", (), {"output": " M src/search.py\n?? src/new.py", "exit_code": 0})(),
            type("R", (), {"output": "diff --git a/src/staged.py b/src/staged.py\n+staged", "exit_code": 0})(),
            type("R", (), {"output": "diff --git a/src/search.py b/src/search.py\n+unstaged", "exit_code": 0})(),
        ]
    )
    lease = _FakeLease(backend, workspace_path=str(workspace))
    lock = _FakeLock()
    port = ManagedVerificationPort(
        pool=_FakePool(lease),  # type: ignore[arg-type]
        settings=type("S", (), {"sandbox_enabled": False})(),  # type: ignore[arg-type]
        workspace=workspace,
        rules_provider=lambda: [],
        rwlock=lock,
        now_ms=lambda: 7,
    )
    snapshot = await port.capture_workspace_changes("fp-1")
    assert snapshot.status_summary == " M src/search.py\n?? src/new.py"
    assert "## Staged changes" in snapshot.diff
    assert "+staged" in snapshot.diff
    assert "## Unstaged changes" in snapshot.diff
    assert "+unstaged" in snapshot.diff
    assert backend.commands == [
        "git status --short --untracked-files=all",
        "git diff --cached --no-ext-diff --unified=3 -- .",
        "git diff --no-ext-diff --unified=3 -- .",
    ]
    assert lease.released is True
