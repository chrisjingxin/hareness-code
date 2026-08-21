"""DeferredToolMiddleware：deferred 工具按需 reveal 的模型可见性控制测试。"""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool

from harness_agent.runtime.deferred_tools import (
    DEFERRED_BUILTIN_TOOL_NAMES,
    RESIDENT_TOOL_NAMES,
    DeferredToolMiddleware,
)
from harness_agent.runtime.run_context import RunContext
from harness_agent.threads.context_lifecycle import (
    ContextAuthority,
    ContextBlock,
    ContextStability,
    RunContextSnapshot,
    _snapshot_id,
    sha256_text,
)
from harness_agent.threads.deferred_store import ThreadDeferredToolStore


def _tool(name: str) -> StructuredTool:
    def _impl(x: str) -> str:
        return x

    return StructuredTool.from_function(func=_impl, name=name, description=name)


RESIDENT = {"read_file", "tool_search"}
DEFERRED = {"lsp", "web_search"}
MCP = {"server_a_tool", "server_b_tool"}


def _make_request(tools: Sequence[object], runtime: object = None) -> ModelRequest:
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    return ModelRequest(model=model, messages=[], tools=list(tools), runtime=runtime)


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


def _make_test_snapshot(thread_id: str) -> RunContextSnapshot:
    block = ContextBlock(
        key="core-policy",
        authority=ContextAuthority.CORE_POLICY,
        stability=ContextStability.IMMUTABLE,
        content="test prompt",
    )
    prompt = block.content
    sid = _snapshot_id(
        project_fingerprint="test-proj",
        thread_id=thread_id,
        blocks=(block,),
        system_prompt=prompt,
        skill_snapshot_id=None,
        legacy=False,
    )
    return RunContextSnapshot(
        project_fingerprint="test-proj",
        thread_id=thread_id,
        snapshot_id=sid,
        blocks=(block,),
        system_prompt=prompt,
        system_fingerprint=sha256_text(prompt),
        created_at_ms=1,
    )


def test_wrap_tool_call_intercepts_tool_search_and_reveals():
    """wrap_tool_call 自动拦截 tool_search 结果并登记至当前 Thread。"""
    store = ThreadDeferredToolStore()
    middleware = DeferredToolMiddleware(resident=RESIDENT)

    snapshot = _make_test_snapshot("thread-test")
    context = RunContext(
        thread_id="thread-test",
        run_id="run-1",
        approval_mode="auto",
        context_snapshot=snapshot,
        deferred_tool_store=store,
    )
    runtime = type("Runtime", (), {"context": context})()

    request = ToolCallRequest(
        tool_call={"name": "tool_search", "args": {"query": "server_a"}, "id": "tc1"},
        tool=_tool("tool_search"),
        runtime=runtime,
        state={},
    )

    search_result_json = json.dumps({
        "results": [{"name": "server_a_tool", "description": "A tool"}]
    })

    def _handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content=search_result_json, tool_call_id="tc1")

    middleware.wrap_tool_call(request, _handler)

    # 验证 store 中登记了目标工具
    assert store.get_revealed("thread-test") == frozenset({"server_a_tool"})

    # 验证下一轮模型请求可见性
    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED, *MCP)]
    model_req = _make_request(tools, runtime=runtime)
    visible = _through_middleware(middleware, model_req)
    assert "server_a_tool" in visible
    assert "server_b_tool" not in visible


def test_tool_search_reveal_ignores_empty_names_and_persists_across_runs():
    """空工具名不登记；同一 Thread 的 reveal 跨 Run（新 RunContext）保持可见。"""
    store = ThreadDeferredToolStore()
    middleware = DeferredToolMiddleware(resident=RESIDENT)

    snapshot = _make_test_snapshot("thread-test")
    context = RunContext(
        thread_id="thread-test",
        run_id="run-1",
        approval_mode="auto",
        context_snapshot=snapshot,
        deferred_tool_store=store,
    )
    runtime = type("Runtime", (), {"context": context})()

    request = ToolCallRequest(
        tool_call={"name": "tool_search", "args": {"query": "server_a"}, "id": "tc1"},
        tool=_tool("tool_search"),
        runtime=runtime,
        state={},
    )
    middleware.wrap_tool_call(
        request,
        lambda r: ToolMessage(
            content=json.dumps({"results": [{"name": "server_a_tool"}, {"name": ""}]}),
            tool_call_id="tc1",
        ),
    )

    # 空名被过滤，只登记真实命中的工具。
    assert store.get_revealed("thread-test") == frozenset({"server_a_tool"})

    # 模拟同 Thread 的下一个 Run：RunContext 更新，store 不变。
    next_context = RunContext(
        thread_id="thread-test",
        run_id="run-2",
        approval_mode="auto",
        context_snapshot=snapshot,
        deferred_tool_store=store,
    )
    next_runtime = type("Runtime", (), {"context": next_context})()
    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED, *MCP)]
    visible = _through_middleware(middleware, _make_request(tools, runtime=next_runtime))
    assert "server_a_tool" in visible
    assert "server_b_tool" not in visible


def test_deferred_tools_isolation_across_threads():
    """共享同一个 DeferredToolMiddleware 实例时，不同 Thread 的 reveal 严格隔离。"""
    store = ThreadDeferredToolStore()
    middleware = DeferredToolMiddleware(resident=RESIDENT)

    snapshot_a = _make_test_snapshot("thread-A")
    context_a = RunContext(
        thread_id="thread-A",
        run_id="run-A",
        approval_mode="auto",
        context_snapshot=snapshot_a,
        deferred_tool_store=store,
    )
    runtime_a = type("Runtime", (), {"context": context_a})()

    snapshot_b = _make_test_snapshot("thread-B")
    context_b = RunContext(
        thread_id="thread-B",
        run_id="run-B",
        approval_mode="auto",
        context_snapshot=snapshot_b,
        deferred_tool_store=store,
    )
    runtime_b = type("Runtime", (), {"context": context_b})()

    # Thread A 搜索并 reveal server_a_tool
    req_a = ToolCallRequest(
        tool_call={"name": "tool_search", "args": {"query": "server_a"}, "id": "tc_a"},
        tool=_tool("tool_search"),
        runtime=runtime_a,
        state={},
    )
    middleware.wrap_tool_call(
        req_a,
        lambda r: ToolMessage(
            content=json.dumps({"results": [{"name": "server_a_tool"}]}),
            tool_call_id="tc_a",
        ),
    )

    tools = [_tool(n) for n in (*RESIDENT, *DEFERRED, *MCP)]

    # Thread A 能看到 server_a_tool
    visible_a = _through_middleware(middleware, _make_request(tools, runtime=runtime_a))
    assert "server_a_tool" in visible_a

    # Thread B 绝对看不到 server_a_tool
    visible_b = _through_middleware(middleware, _make_request(tools, runtime=runtime_b))
    assert "server_a_tool" not in visible_b
    assert sorted(visible_b) == sorted(RESIDENT)


def test_resident_and_deferred_lists_match_design():
    """常驻与 deferred 名单互斥，且覆盖设计 D8 的全部内置工具。"""
    assert not (RESIDENT_TOOL_NAMES & DEFERRED_BUILTIN_TOOL_NAMES)
    assert "tool_search" in RESIDENT_TOOL_NAMES
    assert "apply_patch" not in RESIDENT_TOOL_NAMES | DEFERRED_BUILTIN_TOOL_NAMES
    assert {"lsp", "web_search", "web_fetch",
            "memory_save", "memory_search"} <= DEFERRED_BUILTIN_TOOL_NAMES
    assert {"monitor", "task_output", "task_stop"}.isdisjoint(DEFERRED_BUILTIN_TOOL_NAMES)
