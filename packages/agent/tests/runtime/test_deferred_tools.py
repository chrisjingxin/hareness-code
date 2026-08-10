"""DeferredToolMiddleware：deferred 工具按需 reveal 的模型可见性控制测试。"""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool

from harness_agent.runtime.deferred_tools import (
    DEFERRED_BUILTIN_TOOL_NAMES,
    RESIDENT_TOOL_NAMES,
    DeferredToolMiddleware,
)


def _tool(name: str) -> StructuredTool:
    def _impl(x: str) -> str:
        return x

    return StructuredTool.from_function(func=_impl, name=name, description=name)


RESIDENT = {"read_file", "tool_search"}
DEFERRED = {"lsp", "monitor"}
MCP = {"server_a_tool", "server_b_tool"}


def _make_request(tools: Sequence[object]) -> ModelRequest:
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    return ModelRequest(model=model, messages=[], tools=list(tools))


def _through_middleware(middleware: DeferredToolMiddleware, request: ModelRequest) -> list[str]:
    """执行 wrap_model_call 链，返回 handler 实际收到的工具名。"""
    captured: dict[str, list[str]] = {}

    def _handler(req: ModelRequest) -> ModelResponse:
        captured["tools"] = [getattr(t, "name", str(t)) for t in req.tools]
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(request, _handler)
    return captured["tools"]


def test_initial_visible_is_resident_only():
    """初始模型绑定只含常驻工具，deferred 与 MCP 工具隐藏。"""
    middleware = DeferredToolMiddleware(resident=RESIDENT)
    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED, *MCP)]

    visible = _through_middleware(middleware, _make_request(tools))

    assert sorted(visible) == sorted(RESIDENT)


def test_reveal_adds_tool_to_next_binding():
    """reveal 后下一轮绑定包含目标工具，未 reveal 的保持隐藏。"""
    middleware = DeferredToolMiddleware(resident=RESIDENT)
    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED, *MCP)]

    first = _through_middleware(middleware, _make_request(tools))
    middleware.reveal(["server_a_tool"])
    second = _through_middleware(middleware, _make_request(tools))

    assert "server_a_tool" not in first
    assert "server_a_tool" in second
    assert "server_b_tool" not in second


def test_reveal_ignores_empty_names():
    """reveal 忽略空名，不引入未知工具。"""
    middleware = DeferredToolMiddleware(resident=RESIDENT)
    middleware.reveal(["", "server_a_tool"])

    assert middleware.revealed == frozenset({"server_a_tool"})


def test_revealed_property_snapshot():
    """revealed 属性返回不可变快照。"""
    middleware = DeferredToolMiddleware(resident=RESIDENT)
    middleware.reveal(["a", "b"])

    snapshot = middleware.revealed
    assert snapshot == frozenset({"a", "b"})
    with pytest.raises(AttributeError):
        snapshot.add("c")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_async_wrap_uses_same_visibility():
    """异步模型调用与同步入口使用同一可见集合。"""
    middleware = DeferredToolMiddleware(resident=RESIDENT)
    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED)]
    captured: dict[str, list[str]] = {}

    async def _handler(req: ModelRequest) -> ModelResponse:
        captured["tools"] = [getattr(t, "name", str(t)) for t in req.tools]
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(_make_request(tools), _handler)
    assert sorted(captured["tools"]) == sorted(RESIDENT)


def test_resident_and_deferred_lists_match_design():
    """常驻与 deferred 名单互斥，且覆盖设计 D8 的全部内置工具。"""
    assert not (RESIDENT_TOOL_NAMES & DEFERRED_BUILTIN_TOOL_NAMES)
    assert "tool_search" in RESIDENT_TOOL_NAMES
    assert {"lsp", "monitor", "task_output", "task_stop", "web_search", "web_fetch",
            "memory_save", "memory_search"} <= DEFERRED_BUILTIN_TOOL_NAMES
