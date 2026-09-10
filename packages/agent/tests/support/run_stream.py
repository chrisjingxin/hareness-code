"""测试用 Run stream 辅助。

生产路径只走 execution_stream；测试需要把翻译结果绑到 RunState 的 session。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from harness_agent.host.run_execution import (
    _capture_transcript_on_session,
    _run_error,
    _stream_session_for,
    _to_host_interaction,
)
from harness_agent.runtime.execution_stream import (
    ExecutionStreamError,
    StreamInteractionRequest,
    extract_interaction as stream_extract_interaction,
    message_text,
    resume_value as stream_resume_value,
    translate_stream_event,
    truncate_text,
)


def capture_transcript_message(run: Any, chunk: object) -> bool:
    """把 chunk 写入 Run 的 pending transcript。"""
    return _capture_transcript_on_session(run, _stream_session_for(run), chunk)


def translate_run_stream_event(
    event: tuple[Any, ...], run: Any
) -> list[tuple[str, dict[str, object]]]:
    """返回 (type, payload) 列表，供既有 host 测试断言。"""
    session = _stream_session_for(run)
    try:
        return [
            (signal.type, dict(signal.payload))
            for signal in translate_stream_event(
                event, session, content_visibility="passthrough"
            )
        ]
    except ExecutionStreamError as exc:
        raise _run_error(exc.code, exc.message) from exc


def extract_host_interaction(
    event: tuple[Any, ...],
    *,
    needs_user_decision: Callable[[str, Mapping[str, object]], bool] | None = None,
) -> tuple[Any, dict[str, object] | None]:
    """返回 Host InteractionRequest 或 None。"""
    request, auto = stream_extract_interaction(
        event, needs_user_decision=needs_user_decision
    )
    if request is None:
        return None, auto
    return _to_host_interaction(request), auto


def resume_host_value(spec: Any, response: object) -> dict[str, object]:
    """接受 Host 或 stream InteractionRequest。"""
    if isinstance(spec, StreamInteractionRequest):
        return stream_resume_value(spec, response)
    stream_spec = StreamInteractionRequest(
        request_id=spec.request_id,
        type=spec.type,
        payload=spec.payload,
        interrupt_id=spec.interrupt_id,
        questions=spec.questions,
        action_count=spec.action_count,
        serial_context=spec.serial_context,
    )
    return stream_resume_value(stream_spec, response)


__all__ = [
    "capture_transcript_message",
    "extract_host_interaction",
    "message_text",
    "resume_host_value",
    "translate_run_stream_event",
    "truncate_text",
]
