"""Compose Effect ledger、Activity 与 Evidence 的 SQLite 事实层测试（WP7）。

覆盖：Activity start/finish/restart CAS、启动扫描 running→interrupted 收敛、
EffectIntent 幂等、receipt 确认与冲突拒绝、outcome unknown 只允许从 intent 迁移、
evidence 幂等与按 kind 读取、Work Item 状态 CAS 与非法输入 fail closed。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeEffectStatus,
    ComposeWorkItemStatus,
    ThreadMode,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStoreError,
    CreateComposeWorkItem,
    FinishComposeActivity,
    MarkComposeEffectUnknown,
    RecordComposeEffectIntent,
    RecordComposeEffectReceipt,
    RecordComposeEvidence,
    RestartComposeActivity,
    SetComposeWorkItemStatus,
    StartComposeActivity,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

WORK_ITEM_ID = "wi-effect-store"
ACTIVITY_ID = "act-implement-1"
EFFECT_KEY = "file:wi-effect-store:call-1"


async def _persistence(tmp_path: Path) -> ThreadPersistence:
    """建立隔离 project/home 的真实 SQLite ThreadPersistence。"""
    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


async def _prepare_compose_thread(persistence: ThreadPersistence) -> None:
    """通过真实 Run 受理冻结 Compose mode，并创建唯一 active Work Item。"""
    await persistence.accept_run(
        AcceptRun(
            message="实现 Effect ledger",
            binding=make_test_binding("thread-effects", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_work_item_store()
    await store.create(
        CreateComposeWorkItem(
            thread_id="thread-effects",
            work_item_id=WORK_ITEM_ID,
            slug="effect-store",
            goal="实现 Effect ledger",
            created_at_ms=1_700_000_000_000,
        )
    )


def _start(activity_id: str = ACTIVITY_ID, *, kind: str = "implement") -> StartComposeActivity:
    """构造最小可恢复 Activity start 命令。"""
    return StartComposeActivity(
        activity_id=activity_id,
        work_item_id=WORK_ITEM_ID,
        run_id="run-1",
        kind=kind,
        started_at_ms=1_700_000_000_100,
    )


def _intent(effect_key: str = EFFECT_KEY) -> RecordComposeEffectIntent:
    """构造最小 EffectIntent 命令。"""
    return RecordComposeEffectIntent(
        effect_key=effect_key,
        work_item_id=WORK_ITEM_ID,
        activity_id=ACTIVITY_ID,
        intent={"tool": "write", "path": "/src/a.py"},
        created_at_ms=1_700_000_000_200,
    )


def _receipt(effect_key: str = EFFECT_KEY) -> RecordComposeEffectReceipt:
    """构造真实重读 Snapshot receipt 命令。"""
    return RecordComposeEffectReceipt(
        effect_key=effect_key,
        receipt={
            "backend_id": "local:tmp",
            "path": "/src/a.py",
            "digest": "a" * 64,
            "byte_length": 12,
        },
        updated_at_ms=1_700_000_000_300,
    )


async def test_activity_start_finish_and_restart_bump_attempt(tmp_path: Path) -> None:
    """Activity 生命周期：start→running、finish CAS→failed、restart 递增 attempt。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        activity = await store.start_activity(_start())
        assert activity.status is ComposeActivityStatus.RUNNING
        assert activity.attempt == 1
        assert activity.finished_at_ms is None

        finished = await store.finish_activity(
            FinishComposeActivity(
                activity_id=ACTIVITY_ID,
                status=ComposeActivityStatus.FAILED,
                finished_at_ms=1_700_000_000_400,
            )
        )
        assert finished.status is ComposeActivityStatus.FAILED
        assert finished.finished_at_ms == 1_700_000_000_400

        restarted = await store.restart_activity(
            RestartComposeActivity(
                activity_id=ACTIVITY_ID,
                run_id="run-2",
                started_at_ms=1_700_000_000_500,
            )
        )
        assert restarted.status is ComposeActivityStatus.RUNNING
        assert restarted.attempt == 2
        assert restarted.finished_at_ms is None
        assert restarted.run_id == "run-2"
    finally:
        await persistence.close()


async def test_activity_start_rejects_duplicate_identity(tmp_path: Path) -> None:
    """同一 activity_id 不能创建第二行；恢复必须走 restart。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.start_activity(_start())
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.start_activity(_start())
        assert str(excinfo.value) == "COMPOSE_ACTIVITY_ID_CONFLICT"
    finally:
        await persistence.close()


async def test_finish_activity_cas_rejects_non_running_status(tmp_path: Path) -> None:
    """已完成 Activity 不能再次 finish；CAS 只接受 running/waiting_user。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.start_activity(_start())
        await store.finish_activity(
            FinishComposeActivity(
                activity_id=ACTIVITY_ID,
                status=ComposeActivityStatus.COMPLETED,
                finished_at_ms=1_700_000_000_400,
            )
        )
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.finish_activity(
                FinishComposeActivity(
                    activity_id=ACTIVITY_ID,
                    status=ComposeActivityStatus.FAILED,
                    finished_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_ACTIVITY_STATUS_CONFLICT"
    finally:
        await persistence.close()


async def test_restart_activity_rejects_terminal_and_running(tmp_path: Path) -> None:
    """restart 只接受 interrupted/retryable_failed/failed/cancelled 当前状态。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.start_activity(_start())
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.restart_activity(
                RestartComposeActivity(
                    activity_id=ACTIVITY_ID,
                    run_id="run-2",
                    started_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_ACTIVITY_RESTART_INVALID"
    finally:
        await persistence.close()


async def test_startup_scan_converges_running_activities_to_interrupted(tmp_path: Path) -> None:
    """恢复扫描把遗留 running 收敛为 interrupted，其余状态保持不变。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.start_activity(_start("act-run", kind="verify"))
        await store.start_activity(_start("act-done", kind="implement"))
        await store.finish_activity(
            FinishComposeActivity(
                activity_id="act-done",
                status=ComposeActivityStatus.COMPLETED,
                finished_at_ms=1_700_000_000_400,
            )
        )
        converged = await store.mark_running_activities_interrupted(
            now_ms=1_700_000_000_600
        )
        assert converged == 1
        interrupted = await store.load_activity("act-run")
        assert interrupted.status is ComposeActivityStatus.INTERRUPTED
        assert interrupted.finished_at_ms == 1_700_000_000_600
        completed = await store.load_activity("act-done")
        assert completed.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_effect_intent_recording_is_idempotent(tmp_path: Path) -> None:
    """同一 effect_key 的 intent 只能记录一次，重试返回同一行且不覆盖。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        first = await store.record_effect_intent(_intent())
        assert first.status is ComposeEffectStatus.INTENT
        assert first.receipt is None
        second = await store.record_effect_intent(_intent())
        assert second.status is ComposeEffectStatus.INTENT
        assert second.created_at_ms == first.created_at_ms
        loaded = await store.load_effects(WORK_ITEM_ID)
        assert len(loaded) == 1
    finally:
        await persistence.close()


async def test_effect_receipt_confirms_and_rejects_conflicting_receipt(tmp_path: Path) -> None:
    """receipt 确认 intent；相同 receipt 幂等；不同 receipt 必须冲突拒绝。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.record_effect_intent(_intent())
        confirmed = await store.record_effect_receipt(_receipt())
        assert confirmed.status is ComposeEffectStatus.CONFIRMED
        assert confirmed.receipt is not None
        assert confirmed.receipt["digest"] == "a" * 64
        same = await store.record_effect_receipt(_receipt())
        assert same.status is ComposeEffectStatus.CONFIRMED
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.record_effect_receipt(
                RecordComposeEffectReceipt(
                    effect_key=EFFECT_KEY,
                    receipt={"backend_id": "local:tmp", "path": "/src/a.py", "digest": "b" * 64},
                    updated_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_EFFECT_RECEIPT_CONFLICT"
    finally:
        await persistence.close()


async def test_effect_unknown_only_transitions_from_intent(tmp_path: Path) -> None:
    """outcome unknown 只允许从 intent 迁移；已确认效果不能被标记未知。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.record_effect_intent(_intent())
        unknown = await store.mark_effect_unknown(
            MarkComposeEffectUnknown(
                effect_key=EFFECT_KEY,
                reason="verify 无法证明执行结果",
                updated_at_ms=1_700_000_000_400,
            )
        )
        assert unknown.status is ComposeEffectStatus.UNKNOWN

        await store.record_effect_intent(_intent("file:wi-effect-store:call-2"))
        await store.record_effect_receipt(_receipt("file:wi-effect-store:call-2"))
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.mark_effect_unknown(
                MarkComposeEffectUnknown(
                    effect_key="file:wi-effect-store:call-2",
                    reason="不应发生",
                    updated_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_EFFECT_OUTCOME_UNKNOWN"
    finally:
        await persistence.close()


async def test_load_torn_effects_returns_intents_without_receipts(tmp_path: Path) -> None:
    """恢复扫描可枚举全部 intent 无 receipt 的撕裂效果，已确认的不进入。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        await store.record_effect_intent(_intent("file:wi-effect-store:torn"))
        await store.record_effect_intent(_intent("file:wi-effect-store:done"))
        await store.record_effect_receipt(_receipt("file:wi-effect-store:done"))
        torn = await store.load_torn_effects()
        assert [effect.effect_key for effect in torn] == ["file:wi-effect-store:torn"]
    finally:
        await persistence.close()


async def test_evidence_recording_is_idempotent_and_filtered_by_kind(tmp_path: Path) -> None:
    """evidence 按稳定 identity 幂等；按 kind 读取；冲突 payload 拒绝。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        command = RecordComposeEvidence(
            evidence_id="ev-verify-1",
            work_item_id=WORK_ITEM_ID,
            evidence_kind="verification",
            content_digest="c" * 64,
            payload={"exit_code": 0, "command": "pytest"},
            created_at_ms=1_700_000_000_300,
        )
        first = await store.record_evidence(command)
        assert first.content_digest == "c" * 64
        second = await store.record_evidence(command)
        assert second.payload["exit_code"] == 0
        await store.record_evidence(
            RecordComposeEvidence(
                evidence_id="ev-review-1",
                work_item_id=WORK_ITEM_ID,
                evidence_kind="review",
                content_digest="d" * 64,
                payload={"axis": "code"},
                created_at_ms=1_700_000_000_400,
            )
        )
        verification = await store.load_evidence(WORK_ITEM_ID, "verification")
        assert [evidence.evidence_id for evidence in verification] == ["ev-verify-1"]
        assert len(await store.load_evidence(WORK_ITEM_ID)) == 2
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.record_evidence(
                RecordComposeEvidence(
                    evidence_id="ev-verify-1",
                    work_item_id=WORK_ITEM_ID,
                    evidence_kind="verification",
                    content_digest="e" * 64,
                    payload={"exit_code": 1},
                    created_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_EVIDENCE_CONFLICT"
    finally:
        await persistence.close()


async def test_work_item_status_cas_allows_nonterminal_transitions(tmp_path: Path) -> None:
    """Work Item 状态 CAS：active→blocked→waiting_user；terminal 与陈旧 revision 拒绝。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        item = await store.load_active("thread-effects")
        assert item is not None
        blocked = await store.set_status(
            SetComposeWorkItemStatus(
                work_item_id=WORK_ITEM_ID,
                expected_revision=item.revision,
                status=ComposeWorkItemStatus.BLOCKED,
                updated_at_ms=1_700_000_000_300,
            )
        )
        assert blocked.status is ComposeWorkItemStatus.BLOCKED
        assert blocked.revision == item.revision + 1
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.set_status(
                SetComposeWorkItemStatus(
                    work_item_id=WORK_ITEM_ID,
                    expected_revision=item.revision,
                    status=ComposeWorkItemStatus.WAITING_USER,
                    updated_at_ms=1_700_000_000_400,
                )
            )
        assert str(excinfo.value) == "COMPOSE_WORK_ITEM_REVISION_CONFLICT"
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.set_status(
                SetComposeWorkItemStatus(
                    work_item_id=WORK_ITEM_ID,
                    expected_revision=blocked.revision,
                    status=ComposeWorkItemStatus.COMPLETED,
                    updated_at_ms=1_700_000_000_500,
                )
            )
        assert str(excinfo.value) == "COMPOSE_WORK_ITEM_STATUS_INVALID"
    finally:
        await persistence.close()


async def test_invalid_activity_and_effect_inputs_fail_closed(tmp_path: Path) -> None:
    """空 identity、非法 kind、越界 payload 一律拒绝，不产生部分写入。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.start_activity(_start("", kind="implement"))
        assert str(excinfo.value) == "COMPOSE_ACTIVITY_START_INVALID"
        with pytest.raises(ComposeWorkItemStoreError) as excinfo:
            await store.record_effect_intent(
                RecordComposeEffectIntent(
                    effect_key="bad key!",
                    work_item_id=WORK_ITEM_ID,
                    activity_id=ACTIVITY_ID,
                    intent={},
                    created_at_ms=1,
                )
            )
        assert str(excinfo.value) == "COMPOSE_EFFECT_INTENT_INVALID"
        assert await store.load_activities(WORK_ITEM_ID) == ()
    finally:
        await persistence.close()
