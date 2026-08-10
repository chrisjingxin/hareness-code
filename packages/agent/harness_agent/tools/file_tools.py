"""Harness-owned 文件工具 interposition seam。

DeepAgents 0.6.8 的 ``FilesystemMiddleware`` 是受保护的构图脚手架，不能从图中
排除。本模块不新增同名工具，而是在模型请求阶段去重替换 schema，并在 ToolNode
阶段交给注入的唯一文件 contract 执行，因而 builtin handler 不会收到已接管调用。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from harness_agent.tools.file_tool_catalog import (
    BUILTIN_FILE_TOOL_NAMES,
    HARNESS_FILE_TOOL_NAMES,
)


class FileToolContractError(RuntimeError):
    """文件 contract 接管或 dispatch 失败。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存可被模型恢复逻辑判断的稳定 code。"""
        self.code = code
        super().__init__(message or code)


@runtime_checkable
class FileToolContract(Protocol):
    """可注入的 Harness 文件 schema 与执行 contract。"""

    @property
    def tool_definitions(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回模型请求中使用的 canonical definitions。"""

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回不会落入 DeepAgents builtin handler 的工具名。"""

    @property
    def registration_tools(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回必须注册到 ToolNode 的非 builtin 工具。"""

    def dispatch(self, request: ToolCallRequest) -> ToolMessage | Any:
        """同步接管一个文件工具调用。"""

    async def adispatch(self, request: ToolCallRequest) -> ToolMessage | Any:
        """异步接管一个文件工具调用。"""


def _tool_name(tool: object) -> str:
    """读取 BaseTool、OpenAI function schema 或普通 mapping 的稳定名称。"""
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return str(function["name"])
        return ""
    return str(getattr(tool, "name", ""))


class HarnessFileToolsMiddleware(AgentMiddleware):
    """替换模型可见文件 schema，并短路执行到注入的 Harness contract。"""

    def __init__(self, contract: FileToolContract) -> None:
        """校验 contract 的名称唯一性；本 middleware 不注册 ``tools`` 属性。"""
        super().__init__()
        definitions = tuple(contract.tool_definitions)
        names = tuple(_tool_name(tool) for tool in definitions)
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("FILE_TOOL_CONTRACT_SCHEMA_DUPLICATE")
        handled = frozenset(contract.handled_tool_names)
        if handled != frozenset(names):
            raise ValueError("FILE_TOOL_CONTRACT_NAMES_MISMATCH")
        self._contract = contract
        self._definitions = definitions
        self._definitions_by_name = dict(zip(names, definitions, strict=True))
        self._handled_tool_names = handled

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回当前 middleware 接管的工具集合，供架构测试观察。"""
        return self._handled_tool_names

    def _replace_file_tools(self, tools: Sequence[object]) -> list[BaseTool | dict[str, Any]]:
        """保留上游顺序并把每个已接管名称压成恰好一个 definition。"""
        replaced: list[BaseTool | dict[str, Any]] = []
        emitted: set[str] = set()
        for tool in tools:
            name = _tool_name(tool)
            if name not in self._handled_tool_names:
                replaced.append(tool)  # type: ignore[arg-type]
                continue
            if name not in emitted:
                replaced.append(self._definitions_by_name[name])
                emitted.add(name)
        return replaced

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """同步模型调用只接收当前上游已授权的 Harness file definitions。"""
        return handler(request.override(tools=self._replace_file_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """异步模型调用复用同一去重逻辑。"""
        return await handler(request.override(tools=self._replace_file_tools(request.tools)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """已接管调用直接进入 contract，绝不调用 DeepAgents builtin handler。"""
        name = str(request.tool_call.get("name", ""))
        if name not in self._handled_tool_names:
            return handler(request)
        try:
            result = self._contract.dispatch(request)
            if inspect.isawaitable(result):
                return self._error(request, "FILE_TOOL_ASYNC_REQUIRED")
            return result
        except Exception as exc:  # noqa: BLE001 - 工具边界必须转成稳定 ToolMessage
            return self._error(request, _error_code(exc))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步已接管调用直接进入 contract，builtin handler 不可达。"""
        name = str(request.tool_call.get("name", ""))
        if name not in self._handled_tool_names:
            return await handler(request)
        try:
            result = self._contract.adispatch(request)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:  # noqa: BLE001 - 工具边界必须转成稳定 ToolMessage
            return self._error(request, _error_code(exc))

    @staticmethod
    def _error(request: ToolCallRequest, code: str) -> ToolMessage:
        """构造不泄露源码或凭据的稳定错误结果。"""
        name = str(request.tool_call.get("name", "filesystem"))
        return ToolMessage(
            content=f"Harness 文件工具接管失败：{code}。",
            name=name,
            tool_call_id=str(request.tool_call.get("id") or "file-tool"),
            status="error",
        )


def _error_code(exc: Exception) -> str:
    """从受控错误读取 code，普通异常统一归类不泄露内部信息。"""
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else "FILE_TOOL_EXECUTION_FAILED"


__all__ = [
    "BUILTIN_FILE_TOOL_NAMES",
    "FileToolContract",
    "FileToolContractError",
    "HARNESS_FILE_TOOL_NAMES",
    "HarnessFileToolsMiddleware",
]
