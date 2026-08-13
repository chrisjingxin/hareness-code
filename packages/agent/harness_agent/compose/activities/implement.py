"""Implement Activity：按 Todo 顺序执行 TDD 单项并回写证据。

Plan/Todo 联合确认后才可执行本 Activity。每个未完成 Todo 项使用 fresh
ImplementItemContext（当前项、confirmed 文档摘要、上次失败），由 driver
按原版 test-driven-development 执行；行为变更必须携带 fail-before/pass-after
证据，确实不适合测试的文档/配置项由 driver 记录理由，不能伪造 RED。每完成
一项即在 todo.md 勾选（新 revision 落盘）。全部完成后写入一条绑定当前
Task/Spec/Plan/Todo digest 与 workspace revision 的 implementation 证据，
`implementation_current` 只由该证据与 readiness resolver 判定；任何上游或
工作区变化都使旧证据 stale。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from harness_agent.compose.document_store import (
    ComposeDocumentSnapshot,
    ComposeDocumentStore,
    ComposeDocumentStoreError,
    DocumentCommit,
)
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    ComposeWorkItem,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStore,
    ComposeWorkItemStoreError,
    FinishComposeActivity,
    RecordComposeEvidence,
    RestartComposeActivity,
    StartComposeActivity,
    UpsertComposeDocumentReference,
)

IMPLEMENT_ACTIVITY_KIND = "implement"
"""implement Activity 的稳定 ledger kind。"""

MAX_ITEMS_PER_RUN = 4
"""单个 Run 最多实现的 Todo 项数；超出收敛 waiting 并等待下一 Turn。"""


class ImplementActivityError(RuntimeError):
    """Implement Activity 的稳定错误码；上层映射为可恢复投影或 blocked。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ImplementItemOutcome(str, Enum):
    """单个 Todo 项的实现收敛结果。"""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ImplementItemContext:
    """driver 执行一个 Todo 项的受限上下文。"""

    goal: str
    item_id: str
    item_title: str
    task_digest: str
    spec_digest: str
    plan_digest: str
    todo_digest: str
    previous_failure: str = ""


@dataclass(frozen=True, slots=True)
class ImplementItemResult:
    """一个 Todo 项的 driver 结果与证据。"""

    outcome: ImplementItemOutcome
    fail_before: str = ""
    pass_after: str = ""
    changed_paths: tuple[str, ...] = ()
    reason: str = ""
    blocked_message: str = ""
    execution_id: str = ""


class ImplementDriver(Protocol):
    """implement 模型/工具回合 seam；生产实现绑定原版 test-driven-development。"""

    async def implement_item(self, context: ImplementItemContext) -> ImplementItemResult: ...


class DiagnoseDriver(Protocol):
    """按失败条件加载的 diagnosing-bugs seam。"""

    async def diagnose(self, context: ImplementItemContext, failure: str) -> str: ...


@dataclass(frozen=True, slots=True)
class TodoItemRef:
    """todo.md 中一个可执行条目的行级投影。"""

    index: int
    line: str
    checked: bool


@dataclass(frozen=True, slots=True)
class ImplementResult:
    """Implement Activity 一次执行的收敛结果。"""

    outcome: ImplementItemOutcome
    pending: str | None
    completed_items: int


class ImplementActivity:
    """TDD 单项执行 + todo 勾选 + implementation 证据。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        driver: ImplementDriver,
        workspace_revision: Callable[[], str | None] | None,
        now_ms: Callable[[], int | None] | None = None,
        diagnose: DiagnoseDriver | None = None,
        max_items_per_run: int = MAX_ITEMS_PER_RUN,
    ) -> None:
        self._store = store
        self._documents = documents
        self._driver = driver
        self._workspace_revision = workspace_revision
        self._now_ms_port = now_ms
        self._diagnose = diagnose
        self._max_items = max_items_per_run

    async def run(
        self,
        item: ComposeWorkItem,
        *,
        run_id: str,
    ) -> ImplementResult:
        """执行或恢复 Implement Activity；所有退出路径收敛 Activity ledger。"""
        activity_id = f"implement:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_items(item, activity_id)
        except asyncio.CancelledError:
            raise
        except ImplementActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise ImplementActivityError("COMPOSE_IMPLEMENT_EXECUTION_FAILED") from exc

    async def _ensure_activity_running(
        self,
        activity_id: str,
        item: ComposeWorkItem,
        run_id: str,
    ) -> None:
        """start 或从可恢复状态 restart 本 Activity；running 直接继续。"""
        try:
            existing = await self._store.load_activity(activity_id)
            if existing is None:
                await self._store.start_activity(
                    StartComposeActivity(
                        activity_id=activity_id,
                        work_item_id=item.work_item_id,
                        run_id=run_id,
                        kind=IMPLEMENT_ACTIVITY_KIND,
                        started_at_ms=self._now_ms(),
                    )
                )
                return
            if existing.status is ComposeActivityStatus.RUNNING:
                return
            await self._store.restart_activity(
                RestartComposeActivity(
                    activity_id=activity_id,
                    run_id=run_id,
                    started_at_ms=self._now_ms(),
                )
            )
        except ImplementActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise ImplementActivityError("COMPOSE_IMPLEMENT_LEDGER_FAILED") from exc

    async def _finish(
        self,
        activity_id: str,
        status: ComposeActivityStatus,
    ) -> None:
        """收敛 Activity 终态；已结束的 Activity 不重复收敛。"""
        try:
            await self._store.finish_activity(
                FinishComposeActivity(
                    activity_id=activity_id,
                    status=status,
                    finished_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            if str(exc).startswith("COMPOSE_ACTIVITY_STATUS_CONFLICT"):
                return
            raise ImplementActivityError("COMPOSE_IMPLEMENT_LEDGER_FAILED") from exc

    async def _run_items(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> ImplementResult:
        task = await self._read_snapshot(item, ComposeDocumentKind.TASK)
        spec = await self._read_snapshot(item, ComposeDocumentKind.SPEC)
        plan = await self._read_snapshot(item, ComposeDocumentKind.PLAN)
        todo = await self._read_snapshot(item, ComposeDocumentKind.TODO)
        if any(snapshot is None for snapshot in (task, spec, plan, todo)):
            raise ImplementActivityError("COMPOSE_IMPLEMENT_DOCUMENTS_MISSING")
        completed = 0
        failures = 0
        previous_failure = ""
        while completed < self._max_items:
            todo = await self._read_snapshot(item, ComposeDocumentKind.TODO)
            if todo is None:
                raise ImplementActivityError("COMPOSE_IMPLEMENT_DOCUMENTS_MISSING")
            pending_items = [
                ref for ref in _parse_todo_items(todo.content) if not ref.checked
            ]
            if not pending_items:
                break
            target = pending_items[0]
            context = ImplementItemContext(
                goal=item.goal,
                item_id=str(target.index),
                item_title=target.line.strip(),
                task_digest=task.digest,
                spec_digest=spec.digest,
                plan_digest=plan.digest,
                todo_digest=todo.digest,
                previous_failure=previous_failure,
            )
            try:
                result = await self._driver.implement_item(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ImplementActivityError("COMPOSE_IMPLEMENT_EXECUTION_FAILED") from exc
            if result.outcome is ImplementItemOutcome.BLOCKED:
                await self._finish(activity_id, ComposeActivityStatus.BLOCKED)
                return ImplementResult(
                    ImplementItemOutcome.BLOCKED,
                    pending="implement-handoff",
                    completed_items=completed,
                )
            if result.outcome is ImplementItemOutcome.FAILED:
                failures += 1
                if failures >= self._max_items:
                    await self._finish(
                        activity_id, ComposeActivityStatus.RETRYABLE_FAILED
                    )
                    raise ImplementActivityError("COMPOSE_IMPLEMENT_ITEM_FAILED")
                previous_failure = await self._diagnose_failure(context, result)
                continue
            if not _evidence_valid(result):
                raise ImplementActivityError("COMPOSE_IMPLEMENT_EVIDENCE_INVALID")
            await self._mark_item_checked(item, todo, target)
            await self._record_item_evidence(item, target, result)
            completed += 1
            failures = 0
            previous_failure = ""
        todo = await self._read_snapshot(item, ComposeDocumentKind.TODO)
        if todo is None:
            raise ImplementActivityError("COMPOSE_IMPLEMENT_DOCUMENTS_MISSING")
        if any(not ref.checked for ref in _parse_todo_items(todo.content)):
            await self._finish(activity_id, ComposeActivityStatus.WAITING_USER)
            return ImplementResult(
                ImplementItemOutcome.COMPLETED,
                pending="implement-more",
                completed_items=completed,
            )
        await self._record_implementation_evidence(item, task, spec, plan, todo)
        await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
        return ImplementResult(
            ImplementItemOutcome.COMPLETED,
            pending=None,
            completed_items=completed,
        )

    async def _diagnose_failure(
        self,
        context: ImplementItemContext,
        result: ImplementItemResult,
    ) -> str:
        """FAILED 时按条件走 diagnosing-bugs；无 diagnose seam 时保持原失败。"""
        failure = result.reason or result.fail_before or result.pass_after or "执行失败"
        if self._diagnose is None:
            return failure
        try:
            return await self._diagnose.diagnose(context, failure)
        except asyncio.CancelledError:
            raise
        except Exception:
            return failure

    async def _mark_item_checked(
        self,
        item: ComposeWorkItem,
        todo: ComposeDocumentSnapshot,
        target: TodoItemRef,
    ) -> None:
        """在 todo.md 中勾选完成项并提交新 revision。"""
        lines = todo.content.splitlines()
        lines[target.index] = target.line.replace("- [ ]", "- [x]", 1)
        updated = "\n".join(lines)
        if not updated.endswith("\n"):
            updated += "\n"
        await self._commit_todo(item, todo, updated)

    async def _record_item_evidence(
        self,
        item: ComposeWorkItem,
        target: TodoItemRef,
        result: ImplementItemResult,
    ) -> None:
        """记录单个 Todo 项的 RED/GREEN 审计证据。"""
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=(
                        f"implement-item:{item.work_item_id}:{target.index}:"
                        f"{self._now_ms()}"
                    ),
                    work_item_id=item.work_item_id,
                    evidence_kind="implementation_item",
                    content_digest=_digest_of(
                        f"{target.line}|{result.fail_before}|{result.pass_after}"
                    ),
                    payload={
                        "item": target.line.strip(),
                        "fail_before": result.fail_before,
                        "pass_after": result.pass_after,
                        "changed_paths": list(result.changed_paths),
                        "reason": result.reason,
                        "execution_id": result.execution_id,
                    },
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise ImplementActivityError(
                "COMPOSE_IMPLEMENT_EVIDENCE_WRITE_FAILED"
            ) from exc

    async def _record_implementation_evidence(
        self,
        item: ComposeWorkItem,
        task: ComposeDocumentSnapshot,
        spec: ComposeDocumentSnapshot,
        plan: ComposeDocumentSnapshot,
        todo: ComposeDocumentSnapshot,
    ) -> None:
        """全部完成时写入绑定当前文档与 workspace revision 的总结证据。"""
        revision = (
            self._workspace_revision()
            if self._workspace_revision is not None
            else None
        )
        if not isinstance(revision, str) or not revision:
            raise ImplementActivityError(
                "COMPOSE_IMPLEMENT_WORKSPACE_REVISION_MISSING"
            )
        digests = frozenset({task.digest, spec.digest, plan.digest, todo.digest})
        payload = {
            "workspace_revision": revision,
            "document_digests": sorted(digests),
            "passed": True,
            "execution_id": f"implement:{item.work_item_id}:{self._now_ms()}",
        }
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=f"implementation:{item.work_item_id}:{self._now_ms()}",
                    work_item_id=item.work_item_id,
                    evidence_kind="implementation",
                    content_digest=_digest_of(
                        revision + "|" + "|".join(sorted(digests))
                    ),
                    payload=payload,
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise ImplementActivityError(
                "COMPOSE_IMPLEMENT_EVIDENCE_WRITE_FAILED"
            ) from exc

    async def _read_snapshot(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
    ) -> ComposeDocumentSnapshot | None:
        try:
            return await self._documents.inspect(item.work_item_id, item.slug, kind)
        except ComposeDocumentStoreError as exc:
            raise ImplementActivityError("COMPOSE_IMPLEMENT_DOCUMENT_INVALID") from exc

    async def _commit_todo(
        self,
        item: ComposeWorkItem,
        expected: ComposeDocumentSnapshot,
        content: str,
    ) -> None:
        try:
            snapshot = await self._documents.commit(
                DocumentCommit(
                    work_item_id=item.work_item_id,
                    slug=item.slug,
                    kind=ComposeDocumentKind.TODO,
                    content=content,
                    expected=expected,
                )
            )
            await self._store.upsert_document_reference(
                UpsertComposeDocumentReference(
                    work_item_id=item.work_item_id,
                    kind=ComposeDocumentKind.TODO,
                    relative_path=snapshot.relative_path,
                    content_digest=snapshot.digest,
                    revision=snapshot.revision,
                    updated_at_ms=self._now_ms(),
                )
            )
        except ComposeDocumentStoreError as exc:
            raise ImplementActivityError("COMPOSE_IMPLEMENT_TODO_WRITE_FAILED") from exc
        except ComposeWorkItemStoreError as exc:
            raise ImplementActivityError("COMPOSE_IMPLEMENT_TODO_WRITE_FAILED") from exc

    def _now_ms(self) -> int:
        now = self._now_ms_port() if self._now_ms_port is not None else None
        return int(now) if now is not None else int(time.time() * 1000)


def _parse_todo_items(content: str) -> tuple[TodoItemRef, ...]:
    """按行解析 todo.md 条目；非条目行保持原样。"""
    refs: list[TodoItemRef] = []
    for index, line in enumerate(content.splitlines()):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            refs.append(TodoItemRef(index=index, line=line, checked=False))
        elif stripped.startswith("- [x]"):
            refs.append(TodoItemRef(index=index, line=line, checked=True))
    return tuple(refs)


def _evidence_valid(result: ImplementItemResult) -> bool:
    """行为变更必须有 RED/GREEN；无测试项必须记录理由，不能伪造。"""
    if result.fail_before.strip() and result.pass_after.strip():
        return True
    return bool(result.reason.strip())


def _digest_of(text: str) -> str:
    """生成稳定 SHA-256 摘要，供证据 content_digest 绑定。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
