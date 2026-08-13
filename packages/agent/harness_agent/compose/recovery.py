"""Compose Work Item 的启动恢复、effect 对账与 429 有界 retry。

该模块不拥有 SQLite 连接或 graph。RecoveryScanner 在 Host 重启后把遗留
running Activity 收敛为 interrupted 并枚举撕裂效果；OutcomeReconciler 依据
Tool adapter 声明的策略决定 REPLAY / RECEIPTED / BLOCKED / NOOP，任何
receipt 都只能来自真实对账（文件重读 Snapshot 等），模型输出无权补写；
BoundedProviderRetry 提供 429/Retry-After 的有界重试策略，供
ManagedAgentExecutor 在 stream round 边界使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from harness_agent.compose.models import ComposeEffect, ComposeEffectStatus
from harness_agent.runtime.managed_agent_executor import (
    ProviderRetryPolicy,
    is_provider_rate_limited,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStore,
    MarkComposeEffectUnknown,
    RecordComposeEffectReceipt,
)

_RETRY_AFTER = re.compile(r"(\d+)\s*(ms|s|min|h)?", re.IGNORECASE)


class ToolEffectPolicy(str, Enum):
    """Tool adapter 对一类外部副作用声明的恢复策略。"""

    RETRYABLE = "retryable"
    RECONCILABLE = "reconcilable"
    UNKNOWN = "unknown"


class VerificationOutcome(str, Enum):
    """一次真实对账的三种确定结果。"""

    EXECUTED = "executed"
    NOT_EXECUTED = "not_executed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EffectVerification:
    """对账结果：EXECUTED 必须携带真实 receipt。"""

    outcome: VerificationOutcome
    receipt: Mapping[str, object] | None = None


class EffectVerifier(Protocol):
    """Tool adapter 注入的副作用对账 seam。"""

    def classify(self, intent: Mapping[str, object]) -> ToolEffectPolicy: ...

    def verify(self, intent: Mapping[str, object]) -> EffectVerification: ...


class ReconcileAction(str, Enum):
    """reconcile 后的确定性动作。"""

    NOOP = "noop"
    RECEIPTED = "receipted"
    REPLAY = "replay"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """一次对账结果：动作与（可能已更新的）ledger 事实。"""

    action: ReconcileAction
    effect: ComposeEffect


@dataclass(frozen=True, slots=True)
class RecoveryScanReport:
    """启动恢复扫描的收敛事实汇总。"""

    interrupted_activities: int
    torn_effects: tuple[ComposeEffect, ...]


class OutcomeReconciler:
    """以 verifier 对账 intent 无 receipt 的 effect，绝不信任模型补 receipt。"""

    def __init__(self, verifier: EffectVerifier) -> None:
        self._verifier = verifier

    async def reconcile(
        self,
        store: ComposeWorkItemStore,
        effect: ComposeEffect,
        *,
        now_ms: int,
    ) -> ReconcileResult:
        """按策略与真实对账决定一个 effect 的下一步。

        已确认的效果永不重放；UNKNOWN 策略直接 blocked；RECONCILABLE 依据
        verify 三值收敛：EXECUTED 补 receipt、NOT_EXECUTED 重放、
        UNKNOWN 标记 unknown 并 blocked。
        """
        if effect.status is ComposeEffectStatus.CONFIRMED:
            return ReconcileResult(ReconcileAction.NOOP, effect)
        if effect.status is ComposeEffectStatus.UNKNOWN:
            return ReconcileResult(ReconcileAction.BLOCKED, effect)
        policy = self._verifier.classify(effect.intent)
        if policy is ToolEffectPolicy.RETRYABLE:
            return ReconcileResult(ReconcileAction.REPLAY, effect)
        if policy is ToolEffectPolicy.UNKNOWN:
            return await self._block(store, effect, now_ms)
        verification = self._verifier.verify(effect.intent)
        if verification.outcome is VerificationOutcome.EXECUTED:
            receipt = verification.receipt
            if not isinstance(receipt, Mapping) or not receipt:
                return await self._block(store, effect, now_ms)
            confirmed = await store.record_effect_receipt(
                RecordComposeEffectReceipt(
                    effect_key=effect.effect_key,
                    receipt=dict(receipt),
                    updated_at_ms=now_ms,
                )
            )
            return ReconcileResult(ReconcileAction.RECEIPTED, confirmed)
        if verification.outcome is VerificationOutcome.NOT_EXECUTED:
            return ReconcileResult(ReconcileAction.REPLAY, effect)
        return await self._block(store, effect, now_ms)

    async def _block(
        self,
        store: ComposeWorkItemStore,
        effect: ComposeEffect,
        now_ms: int,
    ) -> ReconcileResult:
        """把无法证明的结果标记 unknown，等待用户 typed decision。"""
        unknown = await store.mark_effect_unknown(
            MarkComposeEffectUnknown(
                effect_key=effect.effect_key,
                reason="effect outcome unknown",
                updated_at_ms=now_ms,
            )
        )
        return ReconcileResult(ReconcileAction.BLOCKED, unknown)


class RecoveryScanner:
    """Host 重启后的启动恢复扫描。"""

    async def scan(
        self,
        store: ComposeWorkItemStore,
        *,
        now_ms: int,
    ) -> RecoveryScanReport:
        """收敛遗留 running Activity 并枚举待对账 effect。"""
        interrupted = await store.mark_running_activities_interrupted(now_ms)
        torn = await store.load_torn_effects()
        return RecoveryScanReport(
            interrupted_activities=interrupted,
            torn_effects=torn,
        )


def _retry_after_seconds(error: BaseException) -> float | None:
    """从 Retry-After 属性或常见文本形状解析秒数；不可解析返回 None。"""
    raw = getattr(error, "retry_after_seconds", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    header = getattr(error, "retry_after", None)
    if isinstance(header, str):
        match = _RETRY_AFTER.search(header)
        if match is not None:
            value = float(match.group(1))
            unit = (match.group(2) or "s").lower()
            if unit == "ms":
                return value / 1000.0
            if unit == "min":
                return value * 60.0
            if unit == "h":
                return value * 3600.0
            return value
    return None


@dataclass(frozen=True, slots=True)
class BoundedProviderRetry:
    """429/Retry-After 的有界 retry 策略；非限流错误不重试。"""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("PROVIDER_RETRY_BUDGET_INVALID")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("PROVIDER_RETRY_DELAY_INVALID")

    def should_retry(self, attempt: int, error: BaseException) -> bool:
        """attempt 从 1 计数；达到预算或非限流错误不再重试。"""
        return attempt < self.max_attempts and is_provider_rate_limited(error)

    def retry_delay_seconds(self, error: BaseException) -> float:
        """优先 Retry-After，缺失用 base delay，上限 max delay。"""
        retry_after = _retry_after_seconds(error)
        if retry_after is None:
            return self.base_delay_seconds
        return min(retry_after, self.max_delay_seconds)
