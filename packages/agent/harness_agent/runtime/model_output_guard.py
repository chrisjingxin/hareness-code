"""模型输出保护：在 LangGraph ToolNode 前校验并规范化 tool_calls。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage


class MalformedToolCallError(RuntimeError):
    """模型返回的工具调用无法安全交给 ToolNode 时抛出的稳定错误。"""

    code = "MALFORMED_TOOL_CALL"

    def __init__(self, _message: str = "model response contained malformed tool call") -> None:
        """只保留稳定摘要，不把 prompt、参数或上游正文写入错误。"""
        super().__init__("model response contained malformed tool call")


class ModelOutputGuardMiddleware(AgentMiddleware):
    """在模型响应离开 middleware 前完成 tool-call 结构门禁。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """同步模型调用只放行可安全执行的消息。"""
        return _normalize_model_result(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """异步模型调用只放行可安全执行的消息。"""
        return _normalize_model_result(await handler(request))


def _normalize_model_result(result: Any) -> Any:
    """保留 LangChain middleware 返回形状，同时替换规范化后的 AIMessage。"""
    if isinstance(result, ModelResponse):
        messages = _normalize_messages(result.result)
        if messages == result.result:
            return result
        return ModelResponse(
            result=messages,
            structured_response=result.structured_response,
        )
    if isinstance(result, ExtendedModelResponse):
        response = result.model_response
        messages = _normalize_messages(response.result)
        if messages == response.result:
            return result
        return ExtendedModelResponse(
            model_response=ModelResponse(
                result=messages,
                structured_response=response.structured_response,
            ),
            command=result.command,
        )
    if isinstance(result, AIMessage):
        return _normalize_ai_message(result, message_index=0)
    return result


def _normalize_messages(messages: list[object]) -> list[object]:
    """按响应顺序校验消息；生成 ID 的序号只在本响应生命周期内使用。"""
    normalized: list[object] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            normalized.append(_normalize_ai_message(message, message_index=message_index))
        else:
            normalized.append(message)
    return normalized


def _normalize_ai_message(message: AIMessage, *, message_index: int) -> AIMessage:
    """校验一条 AIMessage，并在 assistant 侧补齐缺失调用 ID。"""
    invalid_tool_calls = getattr(message, "invalid_tool_calls", None) or ()
    if invalid_tool_calls:
        # provider 已经判定这些调用无法解析；放行会让 ToolNode 拿到残缺
        # 调用并产生无法关联的 ToolMessage，所以直接失败。
        raise MalformedToolCallError()

    raw_calls = getattr(message, "tool_calls", None) or ()
    if not raw_calls:
        return message

    normalized: list[dict[str, object]] = []
    used_ids: set[str] = set()
    changed = False
    for call_index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, Mapping):
            raise MalformedToolCallError()
        name = raw_call.get("name")
        args = raw_call.get("args")
        if not isinstance(name, str) or not name.strip() or not isinstance(args, Mapping):
            raise MalformedToolCallError()

        raw_id = raw_call.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise MalformedToolCallError()
        # 合法原始 ID 逐字保留；只有空白字符串才按缺失 ID 处理。这样
        # unknown/unauthorized ToolMessage 与 assistant history 仍是一一对应。
        call_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else ""
        if not call_id:
            call_id = _generated_tool_call_id(message, message_index, call_index, used_ids)
            changed = True
        if call_id in used_ids:
            raise MalformedToolCallError()
        used_ids.add(call_id)
        normalized_call = dict(raw_call)
        normalized_call["id"] = call_id
        normalized_call["name"] = name.strip()
        normalized_call["args"] = dict(args)
        normalized_call.setdefault("type", "tool_call")
        if normalized_call != dict(raw_call):
            changed = True
        normalized.append(normalized_call)

    if not changed:
        return message
    return message.model_copy(update={"tool_calls": normalized})


def _generated_tool_call_id(
    message: AIMessage,
    message_index: int,
    call_index: int,
    used_ids: set[str],
) -> str:
    """生成可重复规范化的 ID，避免同一响应内的 assistant/tool 失配。"""
    prefix = str(getattr(message, "id", None) or "response")
    candidate = f"tool-{prefix}-{message_index}-{call_index}"
    suffix = 1
    while candidate in used_ids:
        candidate = f"tool-{prefix}-{message_index}-{call_index}-{suffix}"
        suffix += 1
    return candidate
