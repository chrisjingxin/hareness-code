"""Compose 文档摘要驱动的纯 ReadinessResolver 测试。"""

from __future__ import annotations

from harness_agent.compose.models import ComposeDocumentKind


def _documents(*, task_digest: str = "task-v1", todo_digest: str = "todo-v1") -> dict[ComposeDocumentKind, object]:
    """构造当前文件摘要与 SQLite ref 一致的最小文档事实。"""
    from harness_agent.compose.readiness import DocumentReadinessFact

    return {
        ComposeDocumentKind.TASK: DocumentReadinessFact(
            kind=ComposeDocumentKind.TASK,
            current_digest=task_digest,
            recorded_digest=task_digest,
        ),
        ComposeDocumentKind.SPEC: DocumentReadinessFact(
            kind=ComposeDocumentKind.SPEC,
            current_digest="spec-v1",
            recorded_digest="spec-v1",
        ),
        ComposeDocumentKind.PLAN: DocumentReadinessFact(
            kind=ComposeDocumentKind.PLAN,
            current_digest="plan-v1",
            recorded_digest="plan-v1",
        ),
        ComposeDocumentKind.TODO: DocumentReadinessFact(
            kind=ComposeDocumentKind.TODO,
            current_digest=todo_digest,
            recorded_digest=todo_digest,
        ),
    }


def test_readiness_uses_current_digest_sets_not_a_mutable_stage() -> None:
    """Task→Spec→Plan/Todo 只由当前 Markdown 摘要和确认事实确定。"""
    from harness_agent.compose.readiness import ComposeReadinessResolver

    documents = _documents()
    resolver = ComposeReadinessResolver()
    readiness = resolver.resolve(
        documents,
        {
            "task": (frozenset({"task-v1"}),),
            "spec": (frozenset({"task-v1", "spec-v1"}),),
            "plan": (frozenset({"plan-v1", "todo-v1"}),),
        },
    )

    assert readiness.task_confirmed
    assert readiness.spec_confirmed
    assert readiness.plan_confirmed
    assert readiness.todo_executable
    assert not readiness.implementation_current
    assert readiness.next_action == "implement"


def test_readiness_external_edit_invalidates_all_downstream_facts() -> None:
    """手动修改上游 Markdown 后不会由 SQLite 旧摘要覆盖或继续执行。"""
    from harness_agent.compose.readiness import (
        ComposeReadinessResolver,
        DocumentReadinessFact,
    )

    documents = _documents(task_digest="task-v2")
    documents[ComposeDocumentKind.TASK] = DocumentReadinessFact(
        kind=ComposeDocumentKind.TASK,
        current_digest="task-v2",
        recorded_digest="task-v1",
    )
    readiness = ComposeReadinessResolver().resolve(
        documents,
        {
            "task": (frozenset({"task-v1"}),),
            "spec": (frozenset({"task-v1", "spec-v1"}),),
            "plan": (frozenset({"plan-v1", "todo-v1"}),),
        },
    )

    assert not readiness.task_confirmed
    assert not readiness.spec_confirmed
    assert not readiness.plan_confirmed
    assert not readiness.todo_executable
    assert readiness.next_action == "task"


def test_readiness_does_not_merge_digests_from_different_confirmations() -> None:
    """Task/Spec digest 必须来自同一次 typed confirmation，不能由历史并集拼接。"""
    from harness_agent.compose.readiness import ComposeReadinessResolver

    readiness = ComposeReadinessResolver().resolve(
        _documents(),
        {
            "task": (frozenset({"task-v1"}),),
            "spec": (frozenset({"task-v1"}), frozenset({"spec-v1"})),
            "plan": (frozenset({"plan-v1", "todo-v1"}),),
        },
    )

    assert readiness.task_confirmed
    assert not readiness.spec_confirmed
    assert readiness.next_action == "spec"


def test_readiness_missing_or_unconfirmed_document_fails_closed() -> None:
    """SQLite 不足以重建 Markdown；缺文档或确认时都不能跳过对应 gate。"""
    from harness_agent.compose.readiness import ComposeReadinessResolver

    readiness = ComposeReadinessResolver().resolve({}, {})

    assert not readiness.task_confirmed
    assert readiness.next_action == "task"


def test_readiness_invalidates_implementation_verification_review_and_report_on_workspace_change() -> None:
    """同一份文档在工作空间 revision 变化后，所有执行期事实都必须重新建立。"""
    from harness_agent.compose.readiness import (
        CompletionReadinessFact,
        ComposeReadinessResolver,
        DocumentReadinessFact,
        ReportReadinessFact,
        ReviewFreshnessFact,
        WorkspaceFreshnessFact,
    )

    documents = _documents()
    documents[ComposeDocumentKind.REPORT] = DocumentReadinessFact(
        kind=ComposeDocumentKind.REPORT,
        current_digest="report-v1",
        recorded_digest="report-v1",
    )
    source_digests = frozenset({"task-v1", "spec-v1", "plan-v1", "todo-v1"})
    implementation = WorkspaceFreshnessFact(
        workspace_revision="workspace-v1",
        document_digests=source_digests,
        evidence_digest="implementation-v1",
        execution_id="implement-1",
    )
    verification = WorkspaceFreshnessFact(
        workspace_revision="workspace-v1",
        document_digests=source_digests,
        evidence_digest="verification-v1",
        execution_id="verify-1",
    )
    review = ReviewFreshnessFact(
        requirement=WorkspaceFreshnessFact(
            workspace_revision="workspace-v1",
            document_digests=source_digests,
            evidence_digest="requirements-review-v1",
            execution_id="requirements-review-1",
        ),
        code=WorkspaceFreshnessFact(
            workspace_revision="workspace-v1",
            document_digests=source_digests,
            evidence_digest="code-review-v1",
            execution_id="code-review-1",
        ),
    )
    report = ReportReadinessFact(
        document_digest="report-v1",
        source_digests=source_digests
        | {
            "verification-v1",
            "requirements-review-v1",
            "code-review-v1",
        },
    )
    confirmations = {
        "task": (frozenset({"task-v1"}),),
        "spec": (frozenset({"task-v1", "spec-v1"}),),
        "plan": (frozenset({"plan-v1", "todo-v1"}),),
    }

    fresh = ComposeReadinessResolver().resolve(
        documents,
        confirmations,
        workspace_revision="workspace-v1",
        implementation=implementation,
        verification=verification,
        review=review,
        report=report,
        completion=CompletionReadinessFact(
            no_pending_effects=True,
            no_unknown_effects=True,
        ),
    )

    assert fresh.implementation_current
    assert fresh.verification_fresh
    assert fresh.review_fresh
    assert fresh.report_current
    assert fresh.complete

    stale = ComposeReadinessResolver().resolve(
        documents,
        confirmations,
        workspace_revision="workspace-v2",
        implementation=implementation,
        verification=verification,
        review=review,
        report=report,
        completion=CompletionReadinessFact(
            no_pending_effects=True,
            no_unknown_effects=True,
        ),
    )

    assert stale.todo_executable
    assert not stale.implementation_current
    assert not stale.verification_fresh
    assert not stale.review_fresh
    assert not stale.report_current
    assert not stale.complete
    assert stale.next_action == "implement"


def test_readiness_todo_checkmark_evolution_does_not_retrigger_plan_gate() -> None:
    """todo 勾选是计划的预期演进：plan_confirmed 不变，执行证据按新 digest 失效。"""
    from harness_agent.compose.readiness import (
        ComposeReadinessResolver,
        DocumentReadinessFact,
    )

    documents = _documents(todo_digest="todo-v2")
    readiness = ComposeReadinessResolver().resolve(
        documents,
        {
            "task": (frozenset({"task-v1"}),),
            "spec": (frozenset({"task-v1", "spec-v1"}),),
            "plan": (frozenset({"plan-v1", "todo-v1"}),),
        },
    )

    assert readiness.task_confirmed
    assert readiness.spec_confirmed
    assert readiness.plan_confirmed
    assert readiness.todo_executable
    assert readiness.next_action == "implement"
