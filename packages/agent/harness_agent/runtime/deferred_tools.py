"""deferred 工具按需 reveal 中间件（Phase 2，路线 C，HC-160 Thread 隔离版）。

模型绑定每轮由 langchain 动态执行 ``bind_tools(final_tools)``
（factory.py），本中间件在 ``wrap_model_call`` 中把本轮可见工具收敛为
**常驻 ∪ 当前 Thread 已 reveal**：低频工具（D8 名单：lsp/
web_search/web_fetch/memory_save/memory_search）与 MCP 工具初始不绑定模型，
模型经 ``tool_search`` 命中后通过中间件在当前 Thread 上登记 ``reveal``，
下一轮请求自动包含其 schema。

执行入口（ToolNode）保持全量注册，审批与能力视图校验不受影响；本中间件
只控制模型可见性，不做任何执行侧判断。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.tools import BaseTool

from harness_agent.threads.deferred_store import ThreadDeferredToolStore
from harness_agent.tools.file_tool_catalog import RESIDENT_FILE_TOOL_NAMES


class DeferredToolMiddleware(AgentMiddleware):
    """把 deferred 工具从模型绑定中按 Thread 隐藏，经 tool_search 命中后按需 reveal。

    Args:
        resident: 常驻工具名集合（每轮都可见，见设计 D8 名单）。
            不在 resident 且未 reveal 的工具从模型请求中剔除。
        fallback_store: 当无法从 RunContext 解析 store 时使用的内存存储。
    """

    def __init__(
        self,
        *,
        resident: frozenset[str],
        fallback_store: ThreadDeferredToolStore | None = None,
    ) -> None:
        """固定常驻名单；reveal 状态完全委托给按 Thread 隔离的 store。"""
        super().__init__()
        self._resident = resident
        self._fallback_store = fallback_store or ThreadDeferredToolStore()

    def _resolve_store_and_thread(self, runtime: object) -> tuple[ThreadDeferredToolStore, str]:
        """从运行时 runtime 解析当前 Thread 的 store 与 thread_id。"""
        context = getattr(runtime, "context", None)
        if context is not None:
            thread_id = str(getattr(context, "thread_id", "") or "")
            store = getattr(context, "deferred_tool_store", None)
            if isinstance(store, ThreadDeferredToolStore):
                return store, thread_id
            return self._fallback_store, thread_id

        # 从 execution_info 中读取 thread_id（LangGraph Runtime/ToolRuntime 标准字段）
        exec_info = getattr(runtime, "execution_info", None)
        if exec_info is not None:
            exec_tid = getattr(exec_info, "thread_id", None)
            if exec_tid is not None and str(exec_tid):
                return self._fallback_store, str(exec_tid)

        # 从 config.configurable 或 config.metadata 中读取 thread_id
        config = getattr(runtime, "config", {})
        if isinstance(config, dict):
            configurable = config.get("configurable")
            if isinstance(configurable, dict):
                configured_thread = configurable.get("thread_id")
                if configured_thread is not None and str(configured_thread):
                    return self._fallback_store, str(configured_thread)
            metadata = config.get("metadata")
            if isinstance(metadata, dict):
                meta_thread = metadata.get("thread_id")
                if meta_thread is not None and str(meta_thread):
                    return self._fallback_store, str(meta_thread)

        return self._fallback_store, ""

    def _visible(self, tools: Sequence[object], revealed: frozenset[str]) -> list[BaseTool]:
        """保持上游顺序，仅保留常驻与已 reveal 的工具。"""
        return [
            tool
            for tool in tools
            if isinstance(tool, BaseTool)
            and (tool.name in self._resident or tool.name in revealed)
        ]

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """同步模型调用只暴露常驻与当前 Thread 已 reveal 的工具。"""
        store, thread_id = self._resolve_store_and_thread(request.runtime)
        revealed = store.get_revealed(thread_id)
        return handler(request.override(tools=self._visible(request.tools, revealed)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """异步模型调用复用同一 Thread 隔离的可见集合。"""
        store, thread_id = self._resolve_store_and_thread(request.runtime)
        revealed = store.get_revealed(thread_id)
        return await handler(request.override(tools=self._visible(request.tools, revealed)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步工具调用拦截 tool_search 结果并登记至当前 Thread。"""
        response = handler(request)
        if request.tool_call.get("name") == "tool_search":
            self._record_reveal_from_response(request.runtime, response)
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步工具调用拦截 tool_search 结果并登记至当前 Thread。"""
        response = await handler(request)
        if request.tool_call.get("name") == "tool_search":
            self._record_reveal_from_response(request.runtime, response)
        return response

    def _record_reveal_from_response(self, runtime: object, response: Any) -> None:
        """从 tool_search 的执行结果中提取命中的工具名并更新当前 Thread。"""
        content = getattr(response, "content", response)
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    hit_names = [
                        item["name"]
                        for item in data.get("results", [])
                        if isinstance(item, dict) and item.get("name")
                    ]
                    if hit_names:
                        store, thread_id = self._resolve_store_and_thread(runtime)
                        store.reveal(thread_id, hit_names)
            except Exception:
                pass


# D8 常驻名单：文件/执行原语 + 交互 + 模式切换 + 发现入口 + 编辑审批常用。
RESIDENT_TOOL_NAMES: frozenset[str] = frozenset({
    *RESIDENT_FILE_TOOL_NAMES,
    "execute",
    "write_todos",
    "task",
    "ask_user",
    "enter_plan_mode",
    "exit_plan_mode",
    "tool_search",
})

# D8 deferred 名单：低频/场景特定内置工具，经 tool_search 发现后 reveal。
DEFERRED_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
    "lsp",
    "web_search",
    "web_fetch",
    "memory_save",
    "memory_search",
})
