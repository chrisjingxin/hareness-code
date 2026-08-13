"""Effect ledger 恢复、outcome reconciliation 与 429 有界 retry 测试（WP7）。

覆盖：启动扫描 running→interrupted 收敛与撕裂效果枚举；崩溃四个切点
（intent 前 / intent 后 / effect 后 / receipt 后）的重放结果；outcome unknown
必须 blocked 且模型不能补 receipt；文件 effect 使用真实重读 Snapshot 对账；
429/Retry-After 有界 retry 与 executor stream round 重试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from harness_agent.compose.models import (
    ComposeEffectStatus,
    ThreadMode,
)
from harness_agent.compose.recovery import (
    BoundedProviderRetry,
    EffectVerification,
    OutcomeReconciler,
    ReconcileAction,
    RecoveryScanReport,
    RecoveryScanner,
    ToolEffectPolicy,
    VerificationOutcome,
    is_provider_rate_limited,
)
from harness_agent.threads.compose_work_item_store import (
    CreateComposeWorkItem,
    RecordComposeEffectIntent,
    RecordComposeEffectReceipt,
    StartComposeActivity,
)
from harness_agent.threads.text_backend import LocalTextMutationBackend, TextDocument
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from harness_agent.tools.file_tools import (
    FileMutationEffectVerifier,
    make_file_effect_receipt,
)
from tests.support.thread_fixtures import test_binding as make_test_binding

WORK_ITEM_ID = "wi-recovery"
THREAD_ID = "thread-recovery"
EFFECT_KEY = "file:wi-recovery:call-1"


async def _prepare(tmp_path: Path) -> tuple[ThreadPersistence, Any]:
    """建立真实 SQLite、冻结 Compose mode 并创建唯一 active Work Item。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    await persistence.accept_run(
        AcceptRun(
            message="恢复测试",
            binding=make_test_binding(THREAD_ID, "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_work_item_store()
    await store.create(
        CreateComposeWorkItem(
            thread_id=THREAD_ID,
            work_item_id=WORK_ITEM_ID,
            slug="recovery",
            goal="恢复测试",
            created_at_ms=1_700_000_000_000,
        )
    )
    return persistence, store


def _intent(intent: dict[str, object]) -> RecordComposeEffectIntent:
    """构造绑定当前 Work Item 的 EffectIntent 命令。"""
    return RecordComposeEffectIntent(
        effect_key=EFFECT_KEY,
        work_item_id=WORK_ITEM_ID,
        activity_id="act-1",
        intent=intent,
        created_at_ms=1_700_000_000_100,
    )


class _Verifier:
    """按脚本返回受控 verification；记录 classify/verify 调用。"""

    def __init__(self, outcomes: list[EffectVerification], *, policy: ToolEffectPolicy) -> None:
        self.outcomes = list(outcomes)
        self.policy = policy
        self.classified: list[dict[str, object]] = []
        self.verified: list[dict[str, object]] = []

    def classify(self, intent: dict[str, object]) -> ToolEffectPolicy:
        self.classified.append(intent)
        return self.policy

    def verify(self, intent: dict[str, object]) -> EffectVerification:
        self.verified.append(intent)
        return self.outcomes.pop(0)


# ---------- 启动扫描与崩溃切点 ----------


async def test_crash_before_intent_leaves_nothing_to_reconcile(tmp_path: Path) -> None:
    """intent 前崩溃：ledger 无任何 effect 事实，后续执行无需对账。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.start_activity(
            StartComposeActivity(
                activity_id="act-1",
                work_item_id=WORK_ITEM_ID,
                run_id="run-1",
                kind="implement",
                started_at_ms=1_700_000_000_050,
            )
        )
        report = await RecoveryScanner().scan(store, now_ms=1_700_000_000_200)
        assert report == RecoveryScanReport(interrupted_activities=1, torn_effects=())
        assert await store.load_torn_effects() == ()
    finally:
        await persistence.close()


async def test_crash_after_intent_replays_when_provably_not_executed(tmp_path: Path) -> None:
    """intent 后 effect 前崩溃：verifier 证明未执行，返回 REPLAY 且不写 receipt。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "write", "path": "/src/a.py"})
        )
        verifier = _Verifier(
            [EffectVerification(VerificationOutcome.NOT_EXECUTED)],
            policy=ToolEffectPolicy.RECONCILABLE,
        )
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.REPLAY
        loaded = await store.load_effect(EFFECT_KEY)
        assert loaded is not None
        assert loaded.status is ComposeEffectStatus.INTENT
        assert loaded.receipt is None
    finally:
        await persistence.close()


async def test_crash_after_effect_backfills_receipt_from_real_verification(tmp_path: Path) -> None:
    """effect 后 receipt 前崩溃：verifier 证明已执行，receipt 由对账补写而非模型。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "write", "path": "/src/a.py"})
        )
        receipt = {
            "backend_id": "local:tmp",
            "path": "/src/a.py",
            "digest": "a" * 64,
            "byte_length": 12,
        }
        verifier = _Verifier(
            [EffectVerification(VerificationOutcome.EXECUTED, receipt)],
            policy=ToolEffectPolicy.RECONCILABLE,
        )
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.RECEIPTED
        loaded = await store.load_effect(EFFECT_KEY)
        assert loaded is not None
        assert loaded.status is ComposeEffectStatus.CONFIRMED
        assert loaded.receipt == receipt
        assert await store.load_torn_effects() == ()
    finally:
        await persistence.close()


async def test_crash_after_receipt_is_never_replayed(tmp_path: Path) -> None:
    """receipt 后崩溃：已确认效果绝不重放，即使 verifier 声称未执行。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "write", "path": "/src/a.py"})
        )
        await store.record_effect_receipt(
            RecordComposeEffectReceipt(
                effect_key=EFFECT_KEY,
                receipt={
                    "backend_id": "local:tmp",
                    "path": "/src/a.py",
                    "digest": "a" * 64,
                    "byte_length": 12,
                },
                updated_at_ms=1_700_000_000_200,
            )
        )
        verifier = _Verifier(
            [EffectVerification(VerificationOutcome.NOT_EXECUTED)],
            policy=ToolEffectPolicy.RECONCILABLE,
        )
        reconciler = OutcomeReconciler(verifier)
        confirmed = await store.load_effect(EFFECT_KEY)
        assert confirmed is not None
        result = await reconciler.reconcile(store, confirmed, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.NOOP
        assert not verifier.verified
        loaded = await store.load_effect(EFFECT_KEY)
        assert loaded is not None
        assert loaded.status is ComposeEffectStatus.CONFIRMED
    finally:
        await persistence.close()


async def test_unknown_outcome_marks_effect_unknown_and_blocks(tmp_path: Path) -> None:
    """结果未知：效果标记 unknown，动作 BLOCKED，等待用户决策而不是重放。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "shell", "command": "deploy"})
        )
        verifier = _Verifier(
            [EffectVerification(VerificationOutcome.UNKNOWN)],
            policy=ToolEffectPolicy.RECONCILABLE,
        )
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.BLOCKED
        loaded = await store.load_effect(EFFECT_KEY)
        assert loaded is not None
        assert loaded.status is ComposeEffectStatus.UNKNOWN
    finally:
        await persistence.close()


async def test_unknown_policy_blocks_without_verification(tmp_path: Path) -> None:
    """adapter 声明 UNKNOWN 策略的效果直接 blocked，不调用 verify。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "mcp", "name": "remote-action"})
        )
        verifier = _Verifier([], policy=ToolEffectPolicy.UNKNOWN)
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.BLOCKED
        assert not verifier.verified
    finally:
        await persistence.close()


async def test_retryable_policy_replays_without_verification(tmp_path: Path) -> None:
    """adapter 声明 RETRYABLE 的效果直接 REPLAY，ledger 保持 intent。"""
    persistence, store = await _prepare(tmp_path)
    try:
        await store.record_effect_intent(
            _intent({"tool": "read", "path": "/src/a.py"})
        )
        verifier = _Verifier([], policy=ToolEffectPolicy.RETRYABLE)
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.REPLAY
        assert not verifier.verified
    finally:
        await persistence.close()


# ---------- 文件 effect 真实 Snapshot 对账 ----------


async def test_file_verifier_backfills_receipt_from_real_snapshot_re_read(tmp_path: Path) -> None:
    """文件 effect 用真实重读 digest 对账：已执行补 receipt、未执行重放、漂移未知。"""
    persistence, store = await _prepare(tmp_path)
    try:
        backend = LocalTextMutationBackend(tmp_path / "workspace")
        verifier = FileMutationEffectVerifier(backend)
        created: TextDocument = backend.create_text_document("/src/a.py", "v1")
        base = created.identity.digest
        replaced: TextDocument = backend.compare_and_replace_text(
            "/src/a.py", created.identity, "v2"
        )
        expected = replaced.identity.digest

        intent = {"tool": "write", "path": "/src/a.py", "base_digest": base,
                  "expected_digest": expected,
                  "expected_byte_length": replaced.identity.byte_length}
        assert verifier.classify(intent) is ToolEffectPolicy.RECONCILABLE
        executed = verifier.verify(intent)
        assert executed.outcome is VerificationOutcome.EXECUTED
        assert executed.receipt is not None
        assert executed.receipt["digest"] == expected
        assert executed.receipt["byte_length"] == replaced.identity.byte_length

        # 证明已执行：对账后 receipt 写入 ledger，不再撕裂。
        await store.record_effect_intent(_intent(intent))
        reconciler = OutcomeReconciler(verifier)
        effect = (await store.load_torn_effects())[0]
        result = await reconciler.reconcile(store, effect, now_ms=1_700_000_000_300)
        assert result.action is ReconcileAction.RECEIPTED

        # 未执行：目标文件仍是 base digest。
        untouched: TextDocument = backend.create_text_document("/src/untouched.py", "v1")
        not_executed = verifier.verify({
            "tool": "write", "path": "/src/untouched.py",
            "base_digest": untouched.identity.digest,
            "expected_digest": "f" * 64, "expected_byte_length": 3,
        })
        assert not_executed.outcome is VerificationOutcome.NOT_EXECUTED

        # 漂移：文件被外部改成未知内容。
        backend.compare_and_replace_text("/src/a.py", replaced.identity, "external")
        unknown = verifier.verify({
            "tool": "write", "path": "/src/a.py", "base_digest": base,
            "expected_digest": expected,
            "expected_byte_length": replaced.identity.byte_length,
        })
        assert unknown.outcome is VerificationOutcome.UNKNOWN
    finally:
        await persistence.close()


async def test_file_verifier_create_and_delete_reconcile_with_absence(tmp_path: Path) -> None:
    """create 未出现证明未执行；delete 已消失证明已执行；缺失 write 保持未知。"""
    persistence, _ = await _prepare(tmp_path)
    try:
        backend = LocalTextMutationBackend(tmp_path / "workspace")
        verifier = FileMutationEffectVerifier(backend)
        create_intent = {
            "tool": "create", "path": "/new.py", "base_digest": "",
            "expected_digest": "c" * 64, "expected_byte_length": 5,
        }
        not_executed = verifier.verify(create_intent)
        assert not_executed.outcome is VerificationOutcome.NOT_EXECUTED

        document: TextDocument = backend.create_text_document("/gone.py", "x")
        delete_intent = {
            "tool": "delete", "path": "/gone.py",
            "base_digest": document.identity.digest,
        }
        assert verifier.verify(delete_intent).outcome is VerificationOutcome.NOT_EXECUTED
        backend.delete_if_unchanged("/gone.py", document.identity)
        executed = verifier.verify(delete_intent)
        assert executed.outcome is VerificationOutcome.EXECUTED
        assert executed.receipt is not None
        assert executed.receipt.get("deleted") is True

        # 缺失路径上的 write 无法证明结果，保持未知。
        unknown = verifier.verify({
            "tool": "write", "path": "/missing.py", "base_digest": "b" * 64,
            "expected_digest": "d" * 64, "expected_byte_length": 1,
        })
        assert unknown.outcome is VerificationOutcome.UNKNOWN
    finally:
        await persistence.close()


def test_make_file_effect_receipt_uses_document_identity(tmp_path: Path) -> None:
    """receipt 只携带真实重读 Snapshot 事实，不接受模型自述内容。"""
    backend = LocalTextMutationBackend(tmp_path / "workspace")
    document = backend.create_text_document("/src/a.py", "hello")
    receipt = make_file_effect_receipt(document, backend_id=backend.backend_id)
    assert receipt == {
        "backend_id": backend.backend_id,
        "path": "/src/a.py",
        "digest": document.identity.digest,
        "byte_length": document.identity.byte_length,
    }


# ---------- 429 有界 retry ----------


class _RateLimitedError(Exception):
    """携带 provider 429 事实的最小错误。"""

    def __init__(self, *, retry_after_seconds: float | None = None) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.retry_after_seconds = retry_after_seconds


def test_provider_retry_policy_respects_budget_and_retry_after() -> None:
    """429 有界 retry：预算内允许、Retry-After 封顶、非 429 不重试。"""
    policy = BoundedProviderRetry(
        max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=30.0
    )
    limited = _RateLimitedError(retry_after_seconds=2.0)
    assert policy.should_retry(attempt=1, error=limited)
    assert policy.should_retry(attempt=2, error=limited)
    assert not policy.should_retry(attempt=3, error=limited)
    assert policy.retry_delay_seconds(limited) == 2.0
    capped = _RateLimitedError(retry_after_seconds=999.0)
    assert policy.retry_delay_seconds(capped) == 30.0
    fallback = _RateLimitedError(retry_after_seconds=None)
    assert policy.retry_delay_seconds(fallback) == 1.0
    assert not policy.should_retry(attempt=1, error=RuntimeError("boom"))
    assert is_provider_rate_limited(limited)
    assert not is_provider_rate_limited(RuntimeError("boom"))


class _ScriptedRetryPolicy:
    """控制 executor retry 次数与延迟的脚本化策略。"""

    def __init__(self, allowed: int, *, delay: float = 0.0) -> None:
        self.allowed = allowed
        self.delay = delay
        self.calls = 0

    def should_retry(self, attempt: int, error: BaseException) -> bool:
        self.calls += 1
        return self.calls <= self.allowed

    def retry_delay_seconds(self, error: BaseException) -> float:
        return self.delay


class _FlakyAgent:
    """先抛出 N 次 429，之后产出正常事件。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def astream(self, stream_input: object, **kwargs: object):
        self.calls += 1
        if self.calls <= self.failures:
            raise _RateLimitedError()
        yield ("messages", (AIMessageChunk(content="ok"), {}))


class _Runtime:
    """executor 测试用最小 runtime。"""

    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.run_context = None

    def graph_config(self, namespace: str) -> dict[str, object]:
        return {"configurable": {"thread_id": namespace}}

    async def release(self) -> None:
        return None


class _Observer:
    """满足 executor observer 契约的最小实现。"""

    def on_model_round(self) -> None:
        return None

    async def on_execution_complete(self, _result: object) -> None:
        return None

    def emit(self, _signal: object) -> None:
        return None

    async def interact(self, _request: object) -> object:
        return {}

    async def observe_message(self, _chunk: object, _session: object) -> bool:
        return False

    async def after_tool_boundary(self) -> None:
        return None

    def on_stream_event(self) -> None:
        return None


async def test_executor_retries_rate_limited_stream_round_within_budget() -> None:
    """executor 在 429 预算内重试 stream round；预算耗尽后归一化错误。"""
    from harness_agent.runtime.managed_agent_executor import (
        ManagedAgentExecutionError,
        ManagedAgentExecutor,
        ManagedAgentRequest,
    )

    executor = ManagedAgentExecutor()

    async def run_with(failures: int, allowed: int) -> Any:
        agent = _FlakyAgent(failures)
        policy = _ScriptedRetryPolicy(allowed)
        runtime = _Runtime(agent)

        async def acquire() -> _Runtime:
            return runtime

        request = ManagedAgentRequest(
            execution_ref="exec-1",
            parent_execution_ref=None,
            run_id="run-1",
            input="继续",
            checkpoint_namespace="ns-1",
            output_policy="passthrough",
            runtime_provider=acquire,
            is_cancelled=lambda: False,
            idempotency_key="key-1",
            provider_retry=policy,
        )
        return await executor.execute(request, _Observer())

    result = await run_with(failures=2, allowed=2)
    assert result.used_agent is True
    assert result.final_content == "ok"

    with pytest.raises(ManagedAgentExecutionError):
        await run_with(failures=3, allowed=2)
