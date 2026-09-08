"""HC-170 模型输出保护与重试分类的行为测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage

from harness_agent.runtime.model_output_guard import (
    MalformedToolCallError,
    ModelOutputGuardMiddleware,
)
from harness_agent.runtime.provider_retry import (
    BoundedProviderRetry,
    is_provider_rate_limited,
    is_provider_transient,
    run_with_provider_retry,
    run_with_provider_retry_sync,
)


def _response(message: AIMessage) -> ModelResponse:
    """把一条模型消息包装成 middleware handler 的返回值。"""
    return ModelResponse(result=[message])


async def _async_response(message: AIMessage) -> ModelResponse:
    """返回异步 guard 测试所需的 middleware response。"""
    return _response(message)


def test_guard_rejects_blank_tool_name_before_tool_node() -> None:
    """缺少工具名的响应必须在进入 ToolNode 前被标成稳定畸形错误。"""
    middleware = ModelOutputGuardMiddleware()
    message = AIMessage(
        content="<tool_call>",
        tool_calls=[{"id": None, "name": "  ", "args": {}}],
    )

    with pytest.raises(MalformedToolCallError) as error:
        middleware.wrap_model_call(
            SimpleNamespace(),
            lambda _request: _response(message),
        )

    assert error.value.code == "MALFORMED_TOOL_CALL"
    assert "<tool_call>" not in str(error.value)


@pytest.mark.asyncio
async def test_guard_assigns_one_id_to_assistant_response() -> None:
    """合法 name/args 缺 ID 时，assistant 消息本身先得到可复用的 ID。"""
    middleware = ModelOutputGuardMiddleware()
    message = AIMessage(
        content="",
        tool_calls=[{"id": None, "name": "read_file", "args": {"file_path": "/a"}}],
    )

    async def handler(_request: object) -> ModelResponse:
        return _response(message)

    response = await middleware.awrap_model_call(SimpleNamespace(), handler)

    call = response.result[0].tool_calls[0]
    assert isinstance(call["id"], str)
    assert call["id"]
    assert response.result[0].tool_calls[0]["id"] == call["id"]


def test_guard_does_not_reclassify_legal_unknown_tool() -> None:
    """未知/未授权工具是策略层业务错误，不是 provider 畸形响应。"""
    middleware = ModelOutputGuardMiddleware()
    original_id = "  provider-call-1  "
    message = AIMessage(
        content="",
        tool_calls=[{"id": original_id, "name": "not_registered", "args": {}}],
    )

    response = middleware.wrap_model_call(
        SimpleNamespace(),
        lambda _request: _response(message),
    )

    assert response.result[0].tool_calls[0]["id"] == original_id


def test_guard_allows_literal_tool_call_text_without_tool_calls() -> None:
    """普通回答引用协议标签时不能被误判为畸形工具调用。"""
    middleware = ModelOutputGuardMiddleware()
    message = AIMessage(content="请解释 `<tool_call>` 标签的用途。")

    response = middleware.wrap_model_call(
        SimpleNamespace(),
        lambda _request: _response(message),
    )

    assert response.result[0].content == message.content


@pytest.mark.asyncio
async def test_guard_async_allows_literal_tool_call_text_without_tool_calls() -> None:
    """异步模型返回代码示例时也必须保留普通正文。"""
    middleware = ModelOutputGuardMiddleware()
    message = AIMessage(content="代码中可以写出 `<tool_call>`，但没有调用工具。")

    response = await middleware.awrap_model_call(
        SimpleNamespace(),
        lambda _request: _async_response(message),
    )

    assert response.result[0].content == message.content


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SimpleNamespace(status_code=500), True),
        (SimpleNamespace(status_code=502), True),
        (SimpleNamespace(status_code=429), True),
        (TimeoutError("gateway timeout"), True),
        (ConnectionError("connection reset"), True),
        (SimpleNamespace(status_code=400), False),
        (SimpleNamespace(status_code=401), False),
        (SimpleNamespace(status_code=403), False),
        (asyncio.CancelledError(), False),
        (RuntimeError("policy rejected"), False),
    ],
)
def test_provider_retry_classifies_only_transient_errors(error: BaseException, expected: bool) -> None:
    """重试分类不能把确定性错误、取消和策略错误扩大为 provider retry。"""
    assert is_provider_transient(error) is expected


def test_provider_status_code_wins_over_rate_limit_text() -> None:
    """400 错误即使正文提到限流，也不能被文本启发式升级为 retry。"""

    class _BadRequest(RuntimeError):
        status_code = 400

    error = _BadRequest("rate limit policy rejected this request")

    assert not is_provider_rate_limited(error)
    assert not is_provider_transient(error)


def test_provider_retry_is_bounded_and_honors_retry_after() -> None:
    """同一策略同时覆盖 5xx/429/畸形响应，并严格受 attempt budget 限制。"""
    policy = BoundedProviderRetry(
        max_attempts=3,
        base_delay_seconds=0.25,
        max_delay_seconds=2.0,
    )
    error = SimpleNamespace(status_code=503, retry_after_seconds=5.0)

    assert policy.should_retry(1, error)
    assert policy.should_retry(2, error)
    assert not policy.should_retry(3, error)
    assert policy.retry_delay_seconds(error, attempt=1) == 2.0
    assert policy.should_retry(1, MalformedToolCallError("malformed response"))


def test_provider_retry_backoff_scales_with_attempt_and_caps() -> None:
    """无 Retry-After 时退避按 attempt 线性增长，并受上限约束。"""
    policy = BoundedProviderRetry(
        max_attempts=5,
        base_delay_seconds=0.5,
        max_delay_seconds=1.25,
    )
    error = SimpleNamespace(status_code=503)

    assert policy.retry_delay_seconds(error, attempt=1) == 0.5
    assert policy.retry_delay_seconds(error, attempt=2) == 1.0
    assert policy.retry_delay_seconds(error, attempt=3) == 1.25


@pytest.mark.asyncio
async def test_direct_async_model_call_reuses_bounded_retry_owner() -> None:
    """不经过 ManagedAgentExecutor 的异步调用也只按同一预算重试。"""

    class _Transient(RuntimeError):
        status_code = 503

    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _Transient("gateway unavailable")
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await run_with_provider_retry(
        operation,
        BoundedProviderRetry(max_attempts=2, base_delay_seconds=0),
        sleep=sleep,
    )

    assert result == "ok"
    assert attempts == 2
    assert delays == [0]


def test_direct_sync_model_call_does_not_retry_deterministic_request_error() -> None:
    """同步直接调用遇到 401 时保持单次调用并原样抛出。"""

    class _Unauthorized(RuntimeError):
        status_code = 401

    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise _Unauthorized("unauthorized")

    with pytest.raises(_Unauthorized):
        run_with_provider_retry_sync(
            operation,
            BoundedProviderRetry(max_attempts=4, base_delay_seconds=0),
            sleep=lambda _delay: None,
        )
    assert attempts == 1
