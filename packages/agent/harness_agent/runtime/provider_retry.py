"""模型 provider 的单一有界重试策略与错误分类。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import openai


_RETRY_AFTER = re.compile(r"(\d+)\s*(ms|s|min|h)?", re.IGNORECASE)
_RATE_LIMIT_CODES = frozenset(
    {"RATE_LIMITED", "PROVIDER_RATE_LIMITED", "429", "THROTTLED"}
)
_T = TypeVar("_T")


def provider_status_code(error: BaseException) -> int | None:
    """读取 OpenAI-compatible 异常的稳定 HTTP 状态，不读取正文。"""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_provider_rate_limited(error: BaseException) -> bool:
    """判断异常是否携带稳定 provider 限流事实。"""
    status = provider_status_code(error)
    if status is not None:
        # 明确 HTTP 状态码优先于异常文本，避免 400/401/403 的错误文案中
        # 偶然出现 “rate limit” 时被扩大成可重试请求。
        return status == 429
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.upper() in _RATE_LIMIT_CODES:
        return True
    message = str(error)
    return "429" in message or "rate limit" in message.lower()


def is_provider_transient(error: BaseException) -> bool:
    """只识别可恢复的 provider 响应/传输故障。"""
    if isinstance(error, asyncio.CancelledError):
        return False
    if getattr(error, "code", None) == "MALFORMED_TOOL_CALL":
        return True
    status = provider_status_code(error)
    if isinstance(status, int) and (status == 429 or 500 <= status <= 599):
        return True
    if is_provider_rate_limited(error):
        return True
    transport_types = (
        TimeoutError,
        ConnectionError,
        asyncio.TimeoutError,
        httpx.TimeoutException,
        httpx.NetworkError,
        openai.APIConnectionError,
        openai.APITimeoutError,
    )
    return isinstance(error, transport_types)


def is_provider_error(error: BaseException) -> bool:
    """判断异常是否应由 executor 收敛为稳定 provider 终态。"""
    status = provider_status_code(error)
    if isinstance(status, int) and 400 <= status <= 599:
        return True
    if is_provider_transient(error):
        return True
    return isinstance(error, (httpx.HTTPError, openai.APIError))


def retry_after_seconds(error: BaseException) -> float | None:
    """读取异常属性、HTTP response header 或文本形式的 Retry-After。"""
    raw = getattr(error, "retry_after_seconds", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    header = getattr(error, "retry_after", None)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if header is None and isinstance(headers, Mapping):
        header = headers.get("retry-after") or headers.get("Retry-After")
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
    """所有模型调用方共享的线性、有上限 provider retry 策略。"""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        """拒绝无界或不合理的 retry 配置。"""
        if self.max_attempts < 1:
            raise ValueError("PROVIDER_RETRY_BUDGET_INVALID")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("PROVIDER_RETRY_DELAY_INVALID")

    def should_retry(self, attempt: int, error: BaseException) -> bool:
        """attempt 从 1 计数；只在预算内重试临时 provider 错误。"""
        return attempt < self.max_attempts and is_provider_transient(error)

    def retry_delay_seconds(self, error: BaseException, attempt: int) -> float:
        """优先尊重 Retry-After，否则按 attempt 线性退避并封顶。"""
        retry_after = retry_after_seconds(error)
        if retry_after is None:
            return min(
                self.base_delay_seconds * max(1, attempt),
                self.max_delay_seconds,
            )
        return min(retry_after, self.max_delay_seconds)


async def run_with_provider_retry(
    operation: Callable[[], Awaitable[_T]],
    policy: Any | None,
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> _T:
    """为 managed executor 之外的直接异步模型调用复用同一 retry owner。"""
    attempt = 1
    while True:
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - policy 决定是否重试
            if policy is None or not policy.should_retry(attempt, error):
                raise
            await sleep(policy.retry_delay_seconds(error, attempt))
            attempt += 1


def run_with_provider_retry_sync(
    operation: Callable[[], _T],
    policy: Any | None,
    *,
    sleep: Callable[[float], object] = time.sleep,
) -> _T:
    """为同步分类器模型调用复用同一 bounded retry 语义。"""
    attempt = 1
    while True:
        try:
            return operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - policy 决定是否重试
            if policy is None or not policy.should_retry(attempt, error):
                raise
            sleep(policy.retry_delay_seconds(error, attempt))
            attempt += 1
