"""HarnessFileToolsMiddleware 的 schema 去重和 builtin 短路回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool


def _tool(name: str) -> StructuredTool:
    """创建测试用非文件工具。"""

    def implementation(value: str = "ok") -> str:
        return value

    return StructuredTool.from_function(func=implementation, name=name, description=name)


class _Contract:
    """记录 dispatch 次数的最小可注入 contract。"""

    def __init__(self) -> None:
        self.tool_definitions = (_tool("read_file"), _tool("write_file"))
        self.handled_tool_names = frozenset({"read_file", "write_file"})
        self.registration_tools: tuple[StructuredTool, ...] = ()
        self.calls = 0

    def dispatch(self, request: Any) -> ToolMessage:
        self.calls += 1
        return ToolMessage(
            content="handled",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    async def adispatch(self, request: Any) -> ToolMessage:
        return self.dispatch(request)


class _ToolCallingModel(GenericFakeChatModel):
    """让真实 Harness graph 消费预置工具调用而不访问模型服务。"""

    def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
        """保留自身，测试只关心 middleware/ToolNode 路径。"""
        return self


def test_model_request_contains_one_schema_per_interposed_name() -> None:
    """重复 builtin/schema 输入经 seam 后每个文件工具只保留一个定义。"""
    from harness_agent.tools.file_tools import HarnessFileToolsMiddleware

    contract = _Contract()
    middleware = HarnessFileToolsMiddleware(contract)
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    request = ModelRequest(
        model=model,
        messages=[],
        tools=[_tool("read_file"), _tool("read_file"), _tool("write_file"), _tool("other")],
    )
    captured: dict[str, list[str]] = {}

    def handler(next_request: ModelRequest) -> ModelResponse:
        captured["names"] = [str(getattr(tool, "name", "")) for tool in next_request.tools]
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(request, handler)
    assert captured["names"].count("read_file") == 1
    assert captured["names"].count("write_file") == 1
    assert captured["names"].count("other") == 1


def test_interposed_tool_does_not_call_builtin_handler() -> None:
    """接管的写调用只触发 contract，传入的 builtin handler 永远不执行。"""
    from harness_agent.tools.file_tools import HarnessFileToolsMiddleware

    contract = _Contract()
    middleware = HarnessFileToolsMiddleware(contract)
    request = SimpleNamespace(
        tool_call={"name": "write_file", "id": "call-1", "args": {"file_path": "/a", "content": "x"}}
    )
    builtin_calls = 0

    def builtin_handler(_request: Any) -> object:
        nonlocal builtin_calls
        builtin_calls += 1
        return object()

    result = middleware.wrap_tool_call(request, builtin_handler)
    assert isinstance(result, ToolMessage)
    assert contract.calls == 1
    assert builtin_calls == 0


async def test_real_agent_uses_injected_contract_for_write(tmp_path) -> None:
    """真实主图的 write_file 不落入 DeepAgents builtin，而进入注入 contract。"""
    from harness_agent.runtime.agent import create_harness_agent

    contract = _Contract()
    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": str(tmp_path / "new.txt"),
                                "content": "should-not-write",
                            },
                            "id": "call-contract",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        file_tool_contract=contract,
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="create file")]},
        config={"configurable": {"thread_id": "contract-main"}},
    )
    assert contract.calls == 1
    assert not (tmp_path / "new.txt").exists()
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
