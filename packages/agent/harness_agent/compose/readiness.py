"""Compose Work Item 的纯文档 readiness 计算。

Readiness 不保存或读取一个只能向前推进的 ``current_stage``。它只消费本次
inspect 得到的 Workspace Markdown digest、SQLite 已记录的 digest，以及确认
时冻结的 digest 集合；任何外部编辑都会在下一次计算中自然使下游失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from harness_agent.compose.models import ComposeDocumentKind

ConfirmationGroups = tuple[frozenset[str], ...]
"""同一个 gate 的历史 typed confirmation；每组 digest 必须保持原子边界。"""

_IMPLEMENTATION_DOCUMENTS = (
    ComposeDocumentKind.TASK,
    ComposeDocumentKind.SPEC,
    ComposeDocumentKind.PLAN,
    ComposeDocumentKind.TODO,
)


@dataclass(frozen=True, slots=True)
class DocumentReadinessFact:
    """一份 Markdown 的当前 digest 与 SQLite 最近记录 digest。"""

    kind: ComposeDocumentKind
    current_digest: str
    recorded_digest: str

    @property
    def current(self) -> bool:
        """当前工作空间文件是否仍等于上次 runtime 记录的内容。"""
        return bool(self.current_digest) and self.current_digest == self.recorded_digest


@dataclass(frozen=True, slots=True)
class WorkspaceFreshnessFact:
    """一次执行证据绑定的文档集合、workspace revision 与稳定摘要。"""

    workspace_revision: str
    document_digests: frozenset[str]
    evidence_digest: str
    execution_id: str
    passed: bool = True

    def is_fresh(
        self,
        *,
        workspace_revision: str | None,
        document_digests: frozenset[str] | None,
    ) -> bool:
        """只有完整输入集合和工作区 revision 都匹配时才复用执行证据。"""
        return (
            self.passed
            and isinstance(workspace_revision, str)
            and bool(workspace_revision)
            and workspace_revision == self.workspace_revision
            and document_digests is not None
            and self.document_digests == document_digests
            and bool(self.evidence_digest)
            and bool(self.execution_id)
        )


@dataclass(frozen=True, slots=True)
class ReviewFreshnessFact:
    """Requirement/Code 两次独立 Review 的已校验执行事实。"""

    requirement: WorkspaceFreshnessFact
    code: WorkspaceFreshnessFact
    no_required_findings: bool = True

    def is_fresh(
        self,
        *,
        workspace_revision: str | None,
        document_digests: frozenset[str] | None,
    ) -> bool:
        """两个不同 execution 均通过且没有 Required finding 才视为 fresh。"""
        return (
            self.no_required_findings
            and self.requirement.execution_id != self.code.execution_id
            and self.requirement.is_fresh(
                workspace_revision=workspace_revision,
                document_digests=document_digests,
            )
            and self.code.is_fresh(
                workspace_revision=workspace_revision,
                document_digests=document_digests,
            )
        )


@dataclass(frozen=True, slots=True)
class ReportReadinessFact:
    """报告正文与其引用的当前文档、验证和 Review 摘要。"""

    document_digest: str
    source_digests: frozenset[str]

    def is_current(
        self,
        *,
        document_digest: str,
        source_digests: frozenset[str],
    ) -> bool:
        """报告只有仍是当前文件且引用集合完全一致时才不 stale。"""
        return (
            bool(self.document_digest)
            and self.document_digest == document_digest
            and self.source_digests == source_digests
        )


@dataclass(frozen=True, slots=True)
class CompletionReadinessFact:
    """Guard Kernel 提供的无 pending/unknown effect 事实，不在 resolver 内写状态。"""

    no_pending_effects: bool
    no_unknown_effects: bool

    @property
    def ready(self) -> bool:
        """仅当所有 effect 都已有确定 receipt 时允许完成投影。"""
        return self.no_pending_effects and self.no_unknown_effects


@dataclass(frozen=True, slots=True)
class ComposeReadiness:
    """文档依赖链的可观察计算结果；后续 Activity 只消费这些布尔事实。"""

    task_confirmed: bool
    spec_confirmed: bool
    plan_confirmed: bool
    todo_executable: bool
    implementation_current: bool = False
    verification_fresh: bool = False
    review_fresh: bool = False
    report_current: bool = False
    complete: bool = False

    @property
    def next_action(self) -> str:
        """返回确定性的下一项 gate/Activity，不根据历史 stage 猜测。"""
        if not self.task_confirmed:
            return ComposeDocumentKind.TASK.value
        if not self.spec_confirmed:
            return ComposeDocumentKind.SPEC.value
        if not self.plan_confirmed:
            return ComposeDocumentKind.PLAN.value
        if not self.todo_executable:
            return ComposeDocumentKind.TODO.value
        if not self.implementation_current:
            return "implement"
        if not self.verification_fresh:
            return "verify"
        if not self.review_fresh:
            return "review"
        if not self.report_current:
            return ComposeDocumentKind.REPORT.value
        return "complete" if not self.complete else "completed"


class ComposeReadinessResolver:
    """根据当前文档和 confirmation digest set 计算完整依赖链。"""

    def resolve(
        self,
        documents: Mapping[ComposeDocumentKind, DocumentReadinessFact],
        confirmations: Mapping[str, ConfirmationGroups],
        *,
        workspace_revision: str | None = None,
        implementation: WorkspaceFreshnessFact | None = None,
        verification: WorkspaceFreshnessFact | None = None,
        review: ReviewFreshnessFact | None = None,
        report: ReportReadinessFact | None = None,
        completion: CompletionReadinessFact | None = None,
    ) -> ComposeReadiness:
        """纯函数式解析整个依赖链；缺任一文档、证据或 revision 均保守拒绝。"""
        task = documents.get(ComposeDocumentKind.TASK)
        spec = documents.get(ComposeDocumentKind.SPEC)
        plan = documents.get(ComposeDocumentKind.PLAN)
        todo = documents.get(ComposeDocumentKind.TODO)

        task_confirmed = _confirmed_group(
            (task,),
            confirmations.get("task", ()),
        )
        spec_confirmed = task_confirmed and _confirmed_group(
            (task, spec),
            confirmations.get("spec", ()),
        )
        plan_confirmed = spec_confirmed and _confirmed_group(
            (plan, todo),
            confirmations.get("plan", ()),
        )
        source_digests = _current_document_digests(documents, _IMPLEMENTATION_DOCUMENTS)
        todo_executable = plan_confirmed and source_digests is not None
        implementation_current = todo_executable and _workspace_fact_is_fresh(
            implementation,
            workspace_revision=workspace_revision,
            document_digests=source_digests,
        )
        verification_fresh = implementation_current and _workspace_fact_is_fresh(
            verification,
            workspace_revision=workspace_revision,
            document_digests=source_digests,
        )
        review_fresh = verification_fresh and _review_fact_is_fresh(
            review,
            workspace_revision=workspace_revision,
            document_digests=source_digests,
        )
        report_document = documents.get(ComposeDocumentKind.REPORT)
        report_current = review_fresh and _report_is_current(
            report_document,
            report,
            source_digests=source_digests,
            verification=verification,
            review=review,
        )
        complete = report_current and completion is not None and completion.ready
        return ComposeReadiness(
            task_confirmed=task_confirmed,
            spec_confirmed=spec_confirmed,
            plan_confirmed=plan_confirmed,
            todo_executable=todo_executable,
            implementation_current=implementation_current,
            verification_fresh=verification_fresh,
            review_fresh=review_fresh,
            report_current=report_current,
            complete=complete,
        )


def _confirmed_group(
    documents: tuple[DocumentReadinessFact | None, ...],
    confirmation_groups: ConfirmationGroups,
) -> bool:
    """确认一组相互依赖的当前文档均已作为同一次 gate 事实被冻结。"""
    required_digests = frozenset(
        document.current_digest
        for document in documents
        if document is not None and document.current
    )
    return bool(documents) and len(required_digests) == len(documents) and any(
        required_digests <= confirmation_group
        for confirmation_group in confirmation_groups
    )


def _current_document_digests(
    documents: Mapping[ComposeDocumentKind, DocumentReadinessFact],
    kinds: tuple[ComposeDocumentKind, ...],
) -> frozenset[str] | None:
    """汇总一组仍与 SQLite 记录一致的文档摘要，缺失或陈旧时返回 ``None``。"""
    facts = tuple(documents.get(kind) for kind in kinds)
    if any(fact is None or not fact.current for fact in facts):
        return None
    digests = frozenset(fact.current_digest for fact in facts if fact is not None)
    return digests if len(digests) == len(kinds) else None


def _workspace_fact_is_fresh(
    fact: WorkspaceFreshnessFact | None,
    *,
    workspace_revision: str | None,
    document_digests: frozenset[str] | None,
) -> bool:
    """处理可缺省的执行事实；缺 record 一律不能向后推进。"""
    return fact is not None and fact.is_fresh(
        workspace_revision=workspace_revision,
        document_digests=document_digests,
    )


def _review_fact_is_fresh(
    fact: ReviewFreshnessFact | None,
    *,
    workspace_revision: str | None,
    document_digests: frozenset[str] | None,
) -> bool:
    """处理可缺省双 Reviewer 事实，避免一个 reviewer 或旧执行放行。"""
    return fact is not None and fact.is_fresh(
        workspace_revision=workspace_revision,
        document_digests=document_digests,
    )


def _report_is_current(
    document: DocumentReadinessFact | None,
    report: ReportReadinessFact | None,
    *,
    source_digests: frozenset[str] | None,
    verification: WorkspaceFreshnessFact | None,
    review: ReviewFreshnessFact | None,
) -> bool:
    """报告正文和其全部输入摘要必须一起保持当前，避免旧报告宣告完成。"""
    if (
        document is None
        or not document.current
        or report is None
        or source_digests is None
        or verification is None
        or review is None
    ):
        return False
    expected_sources = source_digests | {
        verification.evidence_digest,
        review.requirement.evidence_digest,
        review.code.evidence_digest,
    }
    return report.is_current(
        document_digest=document.current_digest,
        source_digests=expected_sources,
    )
