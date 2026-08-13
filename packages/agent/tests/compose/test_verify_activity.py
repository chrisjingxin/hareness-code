"""Verify Activity（required command 真实验证）行为测试（WP12）。

覆盖：从 todo.md 提取 required command、全量 exit 0 写总结证据、失败追加
修复 Todo 并 FAILED、命令执行异常 retryable、无命令 fail closed、总结证据
绑定文档与 workspace revision、模型输出不能充当 exit-code evidence。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.plan import _render_document as render_plan
from harness_agent.compose.activities.spec import _render_document as render_spec
from harness_agent.compose.activities.task import _render_document as render_task
from harness_agent.compose.activities.verify import (
    VerificationCommandResult,
    VerifyActivity,
    VerifyActivityError,
    VerifyOutcome,
)
from harness_agent.compose.document_store import ComposeDocumentStore, DocumentCommit
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    ThreadMode,
)
from harness_agent.threads.compose_work_item_store import (
    CreateComposeWorkItem,
    RecordComposeConfirmation,
    UpsertComposeDocumentReference,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-verify"
WORK_ITEM_ID = "wi-verify"
NOW = 1_700_000_000_000
REVISION = "rev-verify"

TODO_WITH_COMMAND = (
    "## 执行清单\n\n"
    "- [x] 建立 SQLite 事实层：验证=pytest tests/threads\n"
    "- [x] 接入 engine 流水线：验证=pytest tests/compose\n"
)

TODO_WITHOUT_COMMAND = "## 执行清单\n\n- [x] 已实现但未声明验证\n"


class _FakePort:
    """脚本化命令执行结果；记录调用。"""

    def __init__(self, results: list[VerificationCommandResult] | None = None) -> None:
        self.results = list(results or [])
        self.commands: list[str] = []

    async def run_command(
        self,
        command: str,
        *,
        work_item_id: str,
    ) -> VerificationCommandResult:
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return VerificationCommandResult(
            command=command,
            exit_code=0,
            output_digest="a" * 64,
            execution_id=f"exec-{len(self.commands)}",
        )


async def _harness(tmp_path: Path, *, todo_body: str = TODO_WITH_COMMAND):
    """真实 SQLite + 文档齐全（上游已确认）的 Work Item。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    await persistence.accept_run(
        AcceptRun(
            message="实现搜索",
            binding=make_test_binding(THREAD, "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_work_item_store()
    await store.create(
        CreateComposeWorkItem(
            thread_id=THREAD,
            work_item_id=WORK_ITEM_ID,
            slug="verify",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    await _seed_documents(store, documents, todo_body)
    return persistence, store, documents


async def _seed_documents(store, documents, todo_body: str) -> None:
    kinds = (
        (ComposeDocumentKind.TASK, render_task(
            work_item_id=WORK_ITEM_ID, revision=1, status="proposed",
            updated_at_ms=NOW, body="# 目标\n\n实现站内搜索",
        )),
        (ComposeDocumentKind.SPEC, render_spec(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.SPEC,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="# 行为规格\n\n## interface\n\n- execute_turn",
        )),
        (ComposeDocumentKind.PLAN, render_plan(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.PLAN,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="# 实施计划\n\n## 步骤\n\n1. 两项",
        )),
        (ComposeDocumentKind.TODO, render_plan(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.TODO,
            revision=1, status="proposed", updated_at_ms=NOW,
            body=todo_body,
        )),
    )
    digests: dict[ComposeDocumentKind, str] = {}
    for kind, content in kinds:
        snapshot = await documents.commit(
            DocumentCommit(
                work_item_id=WORK_ITEM_ID,
                slug="verify",
                kind=kind,
                content=content,
                expected=None,
            )
        )
        digests[kind] = snapshot.digest
        await store.upsert_document_reference(
            UpsertComposeDocumentReference(
                work_item_id=WORK_ITEM_ID,
                kind=kind,
                relative_path=snapshot.relative_path,
                content_digest=snapshot.digest,
                revision=1,
                updated_at_ms=NOW,
            )
        )
    for confirmation_kind, digest_kinds in (
        ("task", (ComposeDocumentKind.TASK,)),
        ("spec", (ComposeDocumentKind.TASK, ComposeDocumentKind.SPEC)),
        ("plan", (ComposeDocumentKind.PLAN, ComposeDocumentKind.TODO)),
    ):
        await store.record_confirmation(
            RecordComposeConfirmation(
                work_item_id=WORK_ITEM_ID,
                confirmation_id=f"{confirmation_kind}-gate-fixture",
                confirmation_kind=confirmation_kind,
                document_digests=tuple(digests[kind] for kind in digest_kinds),
                confirmed_at_ms=NOW,
            )
        )


def _activity(store, documents, port):
    return VerifyActivity(
        store=store,
        documents=documents,
        port=port,
        workspace_revision=lambda: REVISION,
        now_ms=lambda: NOW,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_all_commands_pass_records_verification_evidence(tmp_path: Path) -> None:
    """全部命令 exit 0：写绑定文档与 workspace revision 的总结证据。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        port = _FakePort()
        result = await _activity(store, documents, port).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is VerifyOutcome.COMPLETED
        assert port.commands == ["pytest tests/threads", "pytest tests/compose"]
        command_evidence = await store.load_evidence(WORK_ITEM_ID, "verification_command")
        assert len(command_evidence) == 2
        assert all(record.payload["exit_code"] == 0 for record in command_evidence)
        summary = await store.load_evidence(WORK_ITEM_ID, "verification")
        assert len(summary) == 1
        assert summary[0].payload["workspace_revision"] == REVISION
        assert len(summary[0].payload["document_digests"]) == 4
        activity = await store.load_activity(f"verify:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_failed_command_appends_fix_todo_and_returns_failed(tmp_path: Path) -> None:
    """失败命令追加来源明确的修复 Todo；实现证据自动 stale。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        port = _FakePort(
            [
                VerificationCommandResult(
                    command="pytest tests/threads",
                    exit_code=1,
                    output_digest="b" * 64,
                    execution_id="exec-fail",
                )
            ]
        )
        result = await _activity(store, documents, port).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is VerifyOutcome.FAILED
        assert result.pending == "verify-failed"
        todo = await documents.inspect(WORK_ITEM_ID, "verify", ComposeDocumentKind.TODO)
        assert todo is not None
        assert "- [ ] 修复验证失败" in todo.content
        assert "来源=verify" in todo.content
        assert await store.load_evidence(WORK_ITEM_ID, "verification") == ()
        activity = await store.load_activity(f"verify:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.FAILED
    finally:
        await persistence.close()


async def test_port_exception_marks_retryable_failed(tmp_path: Path) -> None:
    """命令执行异常：Activity retryable_failed，不终结 Work Item。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        class _BrokenPort:
            async def run_command(self, command: str, *, work_item_id: str):
                raise RuntimeError("shell unavailable")

        with pytest.raises(VerifyActivityError) as excinfo:
            await _activity(store, documents, _BrokenPort()).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_VERIFY_EXECUTION_FAILED"
        activity = await store.load_activity(f"verify:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
        assert not (await _item(store)).terminal
    finally:
        await persistence.close()


async def test_missing_commands_fails_closed(tmp_path: Path) -> None:
    """todo 未声明 required command 时 fail closed，不能伪造 pass。"""
    persistence, store, documents = await _harness(
        tmp_path, todo_body=TODO_WITHOUT_COMMAND
    )
    try:
        port = _FakePort()
        with pytest.raises(VerifyActivityError) as excinfo:
            await _activity(store, documents, port).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_VERIFY_COMMANDS_MISSING"
        assert port.commands == []
        assert await store.load_evidence(WORK_ITEM_ID, "verification") == ()
    finally:
        await persistence.close()
