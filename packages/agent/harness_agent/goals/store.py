"""借用 ThreadPersistence 事务边界的 project-scoped GoalStore。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, replace
from typing import Any, Literal

from harness_agent.goals.models import (
    GOAL_DEFAULT_MAX_ITERATIONS,
    GOAL_MAX_ASSUMPTIONS,
    GOAL_MAX_ASSUMPTION_CHARS,
    Goal,
    GoalActivity,
    GoalApplyResult,
    GoalContinuation,
    GoalCriterion,
    GoalGrader,
    GoalInspection,
    GoalMutationResult,
    GoalPending,
    GoalReconcileResult,
    GoalRequestResult,
    GoalStoreError,
    goal_to_wire,
    pending_to_wire,
    validate_goal_items,
    validate_goal_payload,
    validate_goal_text,
)


class GoalStore:
    """保存 Goal/current/pending/audit；连接与锁由 ThreadPersistence 拥有。"""

    def __init__(self, connection: Any, *, project_fingerprint: str, lock: asyncio.Lock) -> None:
        if not project_fingerprint:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock

    async def inspect(self, thread_id: str) -> GoalInspection:
        """在同一读事务中恢复 current、pending、最近 evaluation 和 50 条 activity。"""
        try:
            async with self._lock:
                await self._connection.execute("BEGIN")
                try:
                    goal = await self._load_current(thread_id)
                    pending = await self._load_pending(thread_id)
                    latest = await self._load_latest_evaluation(thread_id)
                    activities = await self._load_activities(thread_id)
                    await self._connection.commit()
                except BaseException:
                    await self._connection.rollback()
                    raise
            return GoalInspection(goal, pending, latest, activities)
        except GoalStoreError:
            raise
        except Exception as exc:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE") from exc

    async def request(
        self,
        *,
        thread_id: str,
        request_id: str,
        kind: Literal["create", "replace", "amend"],
        input_text: str,
        expected_goal_id: str | None,
        expected_revision: int | None,
        ready: bool,
        now_ms: int,
    ) -> GoalRequestResult:
        """幂等保存用户 Goal 意图；首次 create 同事务建立空 Thread 索引。"""
        if not thread_id or not request_id or kind not in {"create", "replace", "amend"}:
            raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
        text = validate_goal_text(input_text)
        canonical = {
            "thread_id": thread_id,
            "request_id": request_id,
            "kind": kind,
            "input_text": text,
            "expected_goal_id": expected_goal_id,
            "expected_revision": expected_revision,
        }
        digest = _digest(canonical)
        try:
            async with self._lock:
                await self._connection.execute("BEGIN IMMEDIATE")
                try:
                    replay = await self._load_operation(request_id)
                    if replay is not None:
                        if replay[0] != digest:
                            raise GoalStoreError("GOAL_REQUEST_ID_CONFLICT")
                        result = _request_result(json.loads(replay[1]))
                        await self._connection.commit()
                        return result
                    if (
                        await self._load_pending(thread_id) is not None
                        or await self._load_queued_mutation(thread_id) is not None
                    ):
                        raise GoalStoreError("GOAL_OPERATION_IN_PROGRESS")
                    current = await self._load_current(thread_id)
                    if kind == "create":
                        if current is not None or expected_goal_id is not None or expected_revision is not None:
                            raise GoalStoreError("GOAL_REVISION_CONFLICT")
                        await self._ensure_empty_thread(thread_id, now_ms)
                        base_goal_id = None
                        base_revision = None
                    else:
                        if current is None:
                            raise GoalStoreError("GOAL_NOT_FOUND")
                        if current.goal_id != expected_goal_id or current.revision != expected_revision:
                            raise GoalStoreError("GOAL_REVISION_CONFLICT")
                        base_goal_id = current.goal_id
                        base_revision = current.revision
                    status = "ready" if ready else "queued"
                    pending = GoalPending(
                        request_id=request_id,
                        kind=kind,
                        status=status,
                        base_goal_id=base_goal_id,
                        base_revision=base_revision,
                        input_text=text,
                        proposed_objective=None,
                        proposed_assumptions=(),
                        proposed_criteria=(),
                        created_at_ms=now_ms,
                        updated_at_ms=now_ms,
                        error_code=None,
                    )
                    await self._insert_pending(thread_id, pending)
                    await self._insert_activity(
                        thread_id,
                        GoalActivity(request_id, "proposal", "目标请求已就绪" if ready else "目标请求已排队", now_ms),
                    )
                    result = GoalRequestResult(status, pending)
                    await self._connection.execute(
                        """
                        INSERT INTO harness_goal_operations
                            (project_fingerprint, operation_id, thread_id, operation_kind,
                             params_digest, result_json, created_at_ms)
                        VALUES (?, ?, ?, 'request', ?, ?, ?)
                        """,
                        (
                            self._project_fingerprint,
                            request_id,
                            thread_id,
                            digest,
                            _json(_request_result_wire(result)),
                            now_ms,
                        ),
                    )
                    await self._connection.commit()
                    return result
                except BaseException:
                    await self._connection.rollback()
                    raise
        except GoalStoreError:
            raise
        except Exception as exc:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE") from exc

    async def mutate(
        self,
        *,
        thread_id: str,
        operation_id: str,
        expected_goal_id: str | None,
        expected_revision: int | None,
        action: Literal["pause", "resume", "clear", "cancel_pending"],
        apply_now: bool,
        now_ms: int,
    ) -> GoalMutationResult:
        """以幂等 CAS 应用或排队 lifecycle mutation。"""
        if not thread_id or not operation_id or action not in {"pause", "resume", "clear", "cancel_pending"}:
            raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
        canonical = {
            "thread_id": thread_id,
            "operation_id": operation_id,
            "expected_goal_id": expected_goal_id,
            "expected_revision": expected_revision,
            "action": action,
        }
        digest = _digest(canonical)
        try:
            async with self._lock:
                await self._connection.execute("BEGIN IMMEDIATE")
                try:
                    replay = await self._load_operation(operation_id)
                    if replay is not None:
                        if replay[0] != digest:
                            raise GoalStoreError("GOAL_REQUEST_ID_CONFLICT")
                        result = _mutation_result(json.loads(replay[1]))
                        await self._connection.commit()
                        return result

                    pending = await self._load_pending(thread_id)
                    queued = await self._load_queued_mutation(thread_id)
                    if queued is not None:
                        raise GoalStoreError("GOAL_OPERATION_IN_PROGRESS")
                    if pending is not None and action not in {"clear", "cancel_pending"}:
                        raise GoalStoreError("GOAL_OPERATION_IN_PROGRESS")
                    current = await self._load_current(thread_id)
                    self._validate_mutation_base(
                        current,
                        action=action,
                        expected_goal_id=expected_goal_id,
                        expected_revision=expected_revision,
                        pending=pending,
                    )
                    if not apply_now:
                        if pending is not None:
                            raise GoalStoreError("GOAL_OPERATION_IN_PROGRESS")
                        result = GoalMutationResult("queued", current, pending, None, False)
                        await self._insert_operation(
                            operation_id=operation_id,
                            thread_id=thread_id,
                            operation_kind="mutation_queued",
                            params_digest=digest,
                            result={
                                **_mutation_result_wire(result),
                                "params": canonical,
                            },
                            now_ms=now_ms,
                        )
                        await self._insert_activity(
                            thread_id,
                            GoalActivity(operation_id, "lifecycle", "目标操作已排队", now_ms),
                        )
                        await self._connection.commit()
                        return result

                    result = await self._apply_mutation_unlocked(
                        thread_id=thread_id,
                        action=action,
                        current=current,
                        pending=pending,
                        now_ms=now_ms,
                    )
                    await self._insert_operation(
                        operation_id=operation_id,
                        thread_id=thread_id,
                        operation_kind="mutation_applied",
                        params_digest=digest,
                        result=_mutation_result_wire(result),
                        now_ms=now_ms,
                    )
                    await self._insert_activity(
                        thread_id,
                        GoalActivity(operation_id, "lifecycle", _mutation_summary(action, result.changed), now_ms),
                    )
                    await self._connection.commit()
                    return result
                except BaseException:
                    await self._connection.rollback()
                    raise
        except GoalStoreError:
            raise
        except Exception as exc:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE") from exc

    async def reconcile(self, thread_id: str, *, now_ms: int) -> GoalReconcileResult:
        """在确认 Thread 空闲后恢复 proposal 或只应用一个 queued mutation。"""
        try:
            async with self._lock:
                await self._connection.execute("BEGIN IMMEDIATE")
                try:
                    current = await self._load_current(thread_id)
                    pending = await self._load_pending(thread_id)
                    queued = await self._load_queued_mutation(thread_id)
                    if queued is not None:
                        operation_id, _params_digest, stored = queued
                        params = stored.get("params")
                        if not isinstance(params, dict):
                            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
                        action = params.get("action")
                        if action not in {"pause", "resume", "clear", "cancel_pending"}:
                            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
                        self._validate_mutation_base(
                            current,
                            action=action,
                            expected_goal_id=params.get("expected_goal_id"),
                            expected_revision=params.get("expected_revision"),
                            pending=pending,
                        )
                        applied = await self._apply_mutation_unlocked(
                            thread_id=thread_id,
                            action=action,
                            current=current,
                            pending=pending,
                            now_ms=now_ms,
                        )
                        await self._connection.execute(
                            """
                            UPDATE harness_goal_operations
                            SET operation_kind = 'mutation_applied', result_json = ?
                            WHERE project_fingerprint = ? AND operation_id = ?
                            """,
                            (_json(_mutation_result_wire(applied)), self._project_fingerprint, operation_id),
                        )
                        await self._insert_activity(
                            thread_id,
                            GoalActivity(
                                f"{operation_id}-applied",
                                "lifecycle",
                                _mutation_summary(action, applied.changed),
                                now_ms,
                            ),
                        )
                        await self._connection.commit()
                        return GoalReconcileResult(
                            goal=applied.goal,
                            pending=applied.pending,
                            continuation=applied.continuation,
                            changed=applied.changed,
                            reason=action,
                        )

                    if pending is not None and pending.status in {"queued", "drafting", "clarifying"}:
                        await self._connection.execute(
                            """
                            UPDATE harness_goal_pending
                            SET status = 'ready', updated_at_ms = ?, error_code = NULL,
                                proposed_objective = NULL,
                                proposed_assumptions_json = '[]', proposed_criteria_json = '[]'
                            WHERE project_fingerprint = ? AND request_id = ?
                            """,
                            (now_ms, self._project_fingerprint, pending.request_id),
                        )
                        await self._insert_activity(
                            thread_id,
                            GoalActivity(
                                f"reconcile-{pending.request_id}",
                                "proposal",
                                "目标请求已恢复",
                                now_ms,
                            ),
                        )
                        pending = await self._load_pending(thread_id)
                        await self._connection.commit()
                        return GoalReconcileResult(
                            goal=current,
                            pending=pending,
                            proposal_ready=True,
                            changed=True,
                            reason="proposal_ready",
                        )
                    await self._connection.commit()
                    return GoalReconcileResult(goal=current, pending=pending)
                except BaseException:
                    await self._connection.rollback()
                    raise
        except GoalStoreError:
            raise
        except Exception as exc:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE") from exc

    async def save_proposal(
        self,
        *,
        request_id: str,
        objective: str,
        assumptions: tuple[str, ...],
        criteria: tuple[str, ...],
        now_ms: int,
    ) -> GoalPending:
        """保存 Criteria Agent 的结构化草案，等待 interaction.goal。"""
        objective = validate_goal_text(objective)
        assumptions = validate_goal_items(
            assumptions,
            allow_empty=True,
            max_items=GOAL_MAX_ASSUMPTIONS,
            max_chars=GOAL_MAX_ASSUMPTION_CHARS,
        )
        criteria = validate_goal_items(criteria, allow_empty=False)
        validate_goal_payload(objective, assumptions, criteria)
        async with self._lock:
            cursor = await self._connection.execute(
                """
                UPDATE harness_goal_pending
                SET status = 'reviewing', proposed_objective = ?, proposed_assumptions_json = ?,
                    proposed_criteria_json = ?, updated_at_ms = ?
                WHERE project_fingerprint = ? AND request_id = ?
                """,
                (
                    objective,
                    _json(assumptions),
                    _json(criteria),
                    now_ms,
                    self._project_fingerprint,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                await self._connection.rollback()
                raise GoalStoreError("GOAL_NOT_FOUND")
            await self._connection.commit()
        pending = await self._pending_by_request(request_id)
        if pending is None:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
        return pending

    async def set_proposal_status(
        self,
        request_id: str,
        *,
        status: Literal["ready", "drafting", "clarifying", "failed"],
        now_ms: int,
        error_code: str | None = None,
    ) -> GoalPending:
        """持久化 proposal 运行阶段；中断恢复只信任 request 与完整 review。"""
        if status not in {"ready", "drafting", "clarifying", "failed"}:
            raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
        if status == "failed" and not error_code:
            raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
        async with self._lock:
            cursor = await self._connection.execute(
                """
                UPDATE harness_goal_pending
                SET status = ?, updated_at_ms = ?, error_code = ?
                WHERE project_fingerprint = ? AND request_id = ?
                """,
                (
                    status,
                    now_ms,
                    error_code,
                    self._project_fingerprint,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                await self._connection.rollback()
                raise GoalStoreError("GOAL_NOT_FOUND")
            await self._connection.commit()
        pending = await self._pending_by_request(request_id)
        if pending is None:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
        return pending

    async def apply_proposal(
        self,
        *,
        request_id: str,
        criteria: tuple[str, ...],
        now_ms: int,
        expected_goal_id: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalApplyResult:
        """重新校验 review 并以 base identity CAS 激活 Goal。"""
        criteria = validate_goal_items(criteria, allow_empty=False)
        try:
            async with self._lock:
                await self._connection.execute("BEGIN IMMEDIATE")
                try:
                    pending = await self._pending_by_request_unlocked(request_id)
                    if pending is None:
                        raise GoalStoreError("GOAL_NOT_FOUND")
                    if expected_goal_id is not None or expected_revision is not None:
                        if (
                            pending.base_goal_id != expected_goal_id
                            or pending.base_revision != expected_revision
                        ):
                            raise GoalStoreError("GOAL_REVISION_CONFLICT")
                    current = await self._load_current(pending_thread := await self._pending_thread(request_id))
                    if pending.kind == "create" and current is not None:
                        raise GoalStoreError("GOAL_REVISION_CONFLICT")
                    if pending.kind != "create" and (
                        current is None
                        or current.goal_id != pending.base_goal_id
                        or current.revision != pending.base_revision
                    ):
                        raise GoalStoreError("GOAL_REVISION_CONFLICT")
                    if pending.proposed_objective is None:
                        raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
                    validate_goal_payload(
                        pending.proposed_objective,
                        pending.proposed_assumptions,
                        criteria,
                    )
                    objective = validate_goal_text(pending.proposed_objective or pending.input_text)
                    goal_id = current.goal_id if pending.kind == "amend" and current else f"goal-{uuid.uuid4().hex}"
                    revision = current.revision + 1 if pending.kind == "amend" and current else 1
                    stable_criteria = tuple(
                        GoalCriterion(f"criterion-{index}", text)
                        for index, text in enumerate(criteria, start=1)
                    )
                    goal = Goal(
                        goal_id=goal_id,
                        revision=revision,
                        status="active",
                        objective=objective,
                        assumptions=pending.proposed_assumptions,
                        criteria=stable_criteria,
                        note=None,
                        prior_blocker=None,
                        grader=current.grader if current and pending.kind == "amend" else GoalGrader(),
                        max_iterations=current.max_iterations if current and pending.kind == "amend" else GOAL_DEFAULT_MAX_ITERATIONS,
                        created_at_ms=current.created_at_ms if current and pending.kind == "amend" else now_ms,
                        updated_at_ms=now_ms,
                        completed_at_ms=None,
                    )
                    await self._insert_goal(pending_thread, goal)
                    await self._connection.execute(
                        """
                        INSERT INTO harness_goal_current
                            (project_fingerprint, thread_id, goal_id, revision)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                            goal_id = excluded.goal_id, revision = excluded.revision
                        """,
                        (self._project_fingerprint, pending_thread, goal_id, revision),
                    )
                    await self._connection.execute(
                        "DELETE FROM harness_goal_pending WHERE project_fingerprint = ? AND request_id = ?",
                        (self._project_fingerprint, request_id),
                    )
                    continuation = GoalContinuation(
                        continuation_id=f"continuation-{uuid.uuid4().hex}",
                        goal_id=goal_id,
                        goal_revision=revision,
                        reason="amended" if pending.kind == "amend" else "accepted",
                    )
                    await self._insert_activity(
                        pending_thread,
                        GoalActivity(continuation.continuation_id, "lifecycle", "目标已激活", now_ms),
                    )
                    await self._connection.commit()
                    return GoalApplyResult(goal, continuation)
                except BaseException:
                    await self._connection.rollback()
                    raise
        except GoalStoreError:
            raise
        except Exception as exc:
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE") from exc

    async def cancel_proposal(self, request_id: str, *, now_ms: int) -> None:
        """取消 pending proposal；已激活 Goal 不受影响。"""
        async with self._lock:
            thread_id = await self._pending_thread(request_id)
            await self._connection.execute(
                "DELETE FROM harness_goal_pending WHERE project_fingerprint = ? AND request_id = ?",
                (self._project_fingerprint, request_id),
            )
            await self._insert_activity(
                thread_id,
                GoalActivity(
                    f"proposal-cancelled-{request_id}",
                    "proposal",
                    "目标审核已取消",
                    now_ms,
                ),
            )
            await self._connection.commit()

    @staticmethod
    def _validate_mutation_base(
        current: Goal | None,
        *,
        action: str,
        expected_goal_id: object,
        expected_revision: object,
        pending: GoalPending | None,
    ) -> None:
        """在事务内校验 current/pending 与客户端冻结 identity。"""
        if action == "cancel_pending":
            if pending is None:
                raise GoalStoreError("GOAL_NOT_FOUND")
            return
        if action == "clear" and current is None:
            if pending is None:
                raise GoalStoreError("GOAL_NOT_FOUND")
            if expected_goal_id is not None or expected_revision is not None:
                raise GoalStoreError("GOAL_REVISION_CONFLICT")
            return
        if current is None:
            raise GoalStoreError("GOAL_NOT_FOUND")
        if current.goal_id != expected_goal_id or current.revision != expected_revision:
            raise GoalStoreError("GOAL_REVISION_CONFLICT")

    async def _apply_mutation_unlocked(
        self,
        *,
        thread_id: str,
        action: str,
        current: Goal | None,
        pending: GoalPending | None,
        now_ms: int,
    ) -> GoalMutationResult:
        """只在持有 GoalStore transaction lock 时应用一个已校验 mutation。"""
        if action == "cancel_pending":
            assert pending is not None
            await self._connection.execute(
                "DELETE FROM harness_goal_pending WHERE project_fingerprint = ? AND thread_id = ?",
                (self._project_fingerprint, thread_id),
            )
            return GoalMutationResult("applied", current, None, None, True)
        if action == "clear":
            await self._connection.execute(
                "DELETE FROM harness_goal_current WHERE project_fingerprint = ? AND thread_id = ?",
                (self._project_fingerprint, thread_id),
            )
            await self._connection.execute(
                "DELETE FROM harness_goal_pending WHERE project_fingerprint = ? AND thread_id = ?",
                (self._project_fingerprint, thread_id),
            )
            return GoalMutationResult("applied", None, None, None, current is not None or pending is not None)

        assert current is not None
        if current.status == "complete":
            raise GoalStoreError("GOAL_ALREADY_COMPLETE")
        if action == "pause":
            if current.status == "paused":
                return GoalMutationResult("applied", current, pending, None, False)
            updated = replace(
                current,
                revision=current.revision + 1,
                status="paused",
                updated_at_ms=now_ms,
            )
            continuation = None
        elif action == "resume":
            if current.status == "active":
                return GoalMutationResult("applied", current, pending, None, False)
            updated = replace(
                current,
                revision=current.revision + 1,
                status="active",
                note=None,
                prior_blocker=current.note if current.status == "blocked" else current.prior_blocker,
                updated_at_ms=now_ms,
                completed_at_ms=None,
            )
            continuation = GoalContinuation(
                continuation_id=f"continuation-{uuid.uuid4().hex}",
                goal_id=updated.goal_id,
                goal_revision=updated.revision,
                reason="resumed",
            )
        else:
            raise GoalStoreError("GOAL_OBJECTIVE_INVALID")
        await self._insert_goal(thread_id, updated)
        await self._connection.execute(
            """
            UPDATE harness_goal_current
            SET goal_id = ?, revision = ?
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (updated.goal_id, updated.revision, self._project_fingerprint, thread_id),
        )
        return GoalMutationResult("applied", updated, pending, continuation, True)

    async def _insert_operation(
        self,
        *,
        operation_id: str,
        thread_id: str,
        operation_kind: str,
        params_digest: str,
        result: dict[str, object],
        now_ms: int,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO harness_goal_operations
                (project_fingerprint, operation_id, thread_id, operation_kind,
                 params_digest, result_json, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                operation_id,
                thread_id,
                operation_kind,
                params_digest,
                _json(result),
                now_ms,
            ),
        )

    async def _ensure_empty_thread(self, thread_id: str, now_ms: int) -> None:
        await self._connection.execute(
            """
            INSERT INTO harness_threads
                (project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                 first_message, latest_message, message_count)
            VALUES (?, ?, ?, ?, '', '', 0)
            ON CONFLICT(project_fingerprint, thread_id) DO NOTHING
            """,
            (self._project_fingerprint, thread_id, now_ms, now_ms),
        )

    async def _insert_pending(self, thread_id: str, pending: GoalPending) -> None:
        await self._connection.execute(
            """
            INSERT INTO harness_goal_pending
                (project_fingerprint, thread_id, request_id, kind, status, base_goal_id,
                 base_revision, input_text, proposed_objective, proposed_assumptions_json,
                 proposed_criteria_json, created_at_ms, updated_at_ms, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                thread_id,
                pending.request_id,
                pending.kind,
                pending.status,
                pending.base_goal_id,
                pending.base_revision,
                pending.input_text,
                pending.proposed_objective,
                _json(pending.proposed_assumptions),
                _json(pending.proposed_criteria),
                pending.created_at_ms,
                pending.updated_at_ms,
                pending.error_code,
            ),
        )

    async def _insert_goal(self, thread_id: str, goal: Goal) -> None:
        await self._connection.execute(
            """
            INSERT INTO harness_goals
                (project_fingerprint, thread_id, goal_id, revision, status, objective,
                 assumptions_json, criteria_json, note, prior_blocker, grader_selection,
                 configured_profile_id, actual_profile_id, max_iterations, created_at_ms,
                 updated_at_ms, completed_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                thread_id,
                goal.goal_id,
                goal.revision,
                goal.status,
                goal.objective,
                _json(goal.assumptions),
                _json([asdict(item) for item in goal.criteria]),
                goal.note,
                goal.prior_blocker,
                goal.grader.selection,
                goal.grader.configured_profile_id,
                goal.grader.actual_profile_id,
                goal.max_iterations,
                goal.created_at_ms,
                goal.updated_at_ms,
                goal.completed_at_ms,
            ),
        )

    async def _insert_activity(self, thread_id: str, activity: GoalActivity) -> None:
        await self._connection.execute(
            """
            INSERT OR IGNORE INTO harness_goal_activities
                (project_fingerprint, thread_id, activity_id, kind, summary, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                thread_id,
                activity.activity_id,
                activity.kind,
                activity.summary,
                activity.created_at_ms,
            ),
        )

    async def _load_current(self, thread_id: str) -> Goal | None:
        cursor = await self._connection.execute(
            """
            SELECT goals.* FROM harness_goal_current AS current
            JOIN harness_goals AS goals
              ON goals.project_fingerprint = current.project_fingerprint
             AND goals.goal_id = current.goal_id AND goals.revision = current.revision
            WHERE current.project_fingerprint = ? AND current.thread_id = ?
            """,
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _goal(row) if row is not None else None

    async def _load_pending(self, thread_id: str) -> GoalPending | None:
        cursor = await self._connection.execute(
            "SELECT * FROM harness_goal_pending WHERE project_fingerprint = ? AND thread_id = ?",
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _pending(row) if row is not None else None

    async def _pending_by_request(self, request_id: str) -> GoalPending | None:
        async with self._lock:
            return await self._pending_by_request_unlocked(request_id)

    async def _pending_by_request_unlocked(self, request_id: str) -> GoalPending | None:
        cursor = await self._connection.execute(
            "SELECT * FROM harness_goal_pending WHERE project_fingerprint = ? AND request_id = ?",
            (self._project_fingerprint, request_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _pending(row) if row is not None else None

    async def _pending_thread(self, request_id: str) -> str:
        cursor = await self._connection.execute(
            "SELECT thread_id FROM harness_goal_pending WHERE project_fingerprint = ? AND request_id = ?",
            (self._project_fingerprint, request_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise GoalStoreError("GOAL_NOT_FOUND")
        return str(row["thread_id"])

    async def _load_operation(self, operation_id: str) -> tuple[str, str] | None:
        cursor = await self._connection.execute(
            "SELECT params_digest, result_json FROM harness_goal_operations WHERE project_fingerprint = ? AND operation_id = ?",
            (self._project_fingerprint, operation_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (str(row["params_digest"]), str(row["result_json"])) if row else None

    async def _load_queued_mutation(
        self,
        thread_id: str,
    ) -> tuple[str, str, dict[str, object]] | None:
        cursor = await self._connection.execute(
            """
            SELECT operation_id, params_digest, result_json
            FROM harness_goal_operations
            WHERE project_fingerprint = ? AND thread_id = ?
              AND operation_kind = 'mutation_queued'
            ORDER BY created_at_ms, operation_id LIMIT 1
            """,
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        value = json.loads(str(row["result_json"]))
        if not isinstance(value, dict):
            raise GoalStoreError("GOAL_STORE_UNAVAILABLE")
        return str(row["operation_id"]), str(row["params_digest"]), value

    async def _load_latest_evaluation(self, thread_id: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            """
            SELECT projection_json FROM harness_goal_evaluations
            WHERE project_fingerprint = ? AND thread_id = ?
            ORDER BY created_at_ms DESC, evaluation_id DESC LIMIT 1
            """,
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return json.loads(str(row["projection_json"])) if row else None

    async def _load_activities(self, thread_id: str) -> tuple[GoalActivity, ...]:
        cursor = await self._connection.execute(
            """
            SELECT activity_id, kind, summary, created_at_ms
            FROM (
                SELECT activity_id, kind, summary, created_at_ms
                FROM harness_goal_activities
                WHERE project_fingerprint = ? AND thread_id = ?
                ORDER BY created_at_ms DESC, activity_id DESC LIMIT 50
            ) ORDER BY created_at_ms, activity_id
            """,
            (self._project_fingerprint, thread_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(
            GoalActivity(str(row["activity_id"]), str(row["kind"]), str(row["summary"]), int(row["created_at_ms"]))
            for row in rows
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _pending(row: Any) -> GoalPending:
    return GoalPending(
        request_id=str(row["request_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        base_goal_id=row["base_goal_id"],
        base_revision=int(row["base_revision"]) if row["base_revision"] is not None else None,
        input_text=str(row["input_text"]),
        proposed_objective=row["proposed_objective"],
        proposed_assumptions=tuple(json.loads(str(row["proposed_assumptions_json"]))),
        proposed_criteria=tuple(json.loads(str(row["proposed_criteria_json"]))),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        error_code=row["error_code"],
    )


def _goal(row: Any) -> Goal:
    criteria = tuple(GoalCriterion(**item) for item in json.loads(str(row["criteria_json"])))
    return Goal(
        goal_id=str(row["goal_id"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        objective=str(row["objective"]),
        assumptions=tuple(json.loads(str(row["assumptions_json"]))),
        criteria=criteria,
        note=row["note"],
        prior_blocker=row["prior_blocker"],
        grader=GoalGrader(str(row["grader_selection"]), row["configured_profile_id"], row["actual_profile_id"]),
        max_iterations=int(row["max_iterations"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        completed_at_ms=int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
    )


def _request_result_wire(result: GoalRequestResult) -> dict[str, object]:
    return {"disposition": result.disposition, "pending": asdict(result.pending)}


def _request_result(value: dict[str, Any]) -> GoalRequestResult:
    pending = value["pending"]
    pending["proposed_assumptions"] = tuple(pending["proposed_assumptions"])
    pending["proposed_criteria"] = tuple(pending["proposed_criteria"])
    return GoalRequestResult(value["disposition"], GoalPending(**pending))


def _mutation_result_wire(result: GoalMutationResult) -> dict[str, object]:
    return {
        "disposition": result.disposition,
        "goal": goal_to_wire(result.goal),
        "pending": pending_to_wire(result.pending),
        "continuation": asdict(result.continuation) if result.continuation is not None else None,
        "changed": result.changed,
    }


def _mutation_result(value: dict[str, Any]) -> GoalMutationResult:
    raw_continuation = value.get("continuation")
    continuation = (
        GoalContinuation(**raw_continuation)
        if isinstance(raw_continuation, dict)
        else None
    )
    return GoalMutationResult(
        disposition=value["disposition"],
        goal=_goal_from_wire(value.get("goal")),
        pending=_pending_from_wire(value.get("pending")),
        continuation=continuation,
        changed=bool(value.get("changed")),
    )


def _goal_from_wire(value: object) -> Goal | None:
    if not isinstance(value, dict):
        return None
    raw = dict(value)
    raw["assumptions"] = tuple(raw["assumptions"])
    raw["criteria"] = tuple(GoalCriterion(**item) for item in raw["criteria"])
    raw["grader"] = GoalGrader(**raw["grader"])
    return Goal(**raw)


def _pending_from_wire(value: object) -> GoalPending | None:
    if not isinstance(value, dict):
        return None
    raw = dict(value)
    raw["proposed_assumptions"] = tuple(raw["proposed_assumptions"])
    raw["proposed_criteria"] = tuple(raw["proposed_criteria"])
    return GoalPending(**raw)


def _mutation_summary(action: str, changed: bool) -> str:
    labels = {
        "pause": "目标已暂停",
        "resume": "目标已恢复",
        "clear": "目标已清除",
        "cancel_pending": "待处理目标操作已取消",
    }
    return labels.get(action, "目标操作已应用") if changed else "目标状态未变化"
