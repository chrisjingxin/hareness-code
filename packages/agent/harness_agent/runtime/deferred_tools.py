"""deferred 工具按需 reveal 中间件（Phase 2，路线 C）。

模型绑定每轮由 langchain 动态执行 ``bind_tools(final_tools)``
（factory.py），本中间件在 ``wrap_model_call`` 中把本轮可见工具收敛为
**常驻 ∪ 已 reveal**：低频工具（D8 名单：lsp/monitor/task_output/task_stop/
web_search/web_fetch/memory_save/memory_search）与 MCP 工具初始不绑定模型，
模型经 ``tool_search`` 命中后 ``reveal``，下一轮请求自动包含其 schema。

执行入口（ToolNode）保持全量注册，审批与能力视图校验不受影响；本中间件
只控制模型可见性，不做任何执行侧判断。
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.tools import BaseTool


class DeferredToolMiddleware(AgentMiddleware):
    """把 deferred 工具从模型绑定中隐藏，经 tool_search 命中后按需 reveal。

    Args:
        resident: 常驻工具名集合（每轮都可见，见设计 D8 名单）。
            不在 resident 且未 reveal 的工具从模型请求中剔除。
    """

    def __init__(self, *, resident: frozenset[str]) -> None:
        """固定常驻名单；reveal 状态为可变集合，跨图线程共享时由锁保护。"""
        super().__init__()
        self._resident = resident
        self._revealed: set[str] = set()
        self._lock = threading.Lock()

    def reveal(self, names: Sequence[str]) -> None:
        """把命中的工具加入下一轮模型绑定（追加式，无需回滚）。

        没有外部 API 状态（对比 Qwen 的 setTools 失败回滚），因此只做加法；
        不 reveal 的轮次模型绑定保持稳定，前缀缓存不受影响。
        """
        with self._lock:
            self._revealed.update(name for name in names if name)

    @property
    def revealed(self) -> frozenset[str]:
        """当前已 reveal 的工具名快照。"""
        with self._lock:
            return frozenset(self._revealed)

    def _visible(self, tools: Sequence[object]) -> list[BaseTool]:
        """保持上游顺序，仅保留常驻与已 reveal 的工具。"""
        with self._lock:
            revealed = self._revealed
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
        """同步模型调用只暴露常驻与已 reveal 工具。"""
        return handler(request.override(tools=self._visible(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """异步模型调用复用同一可见集合。"""
        return await handler(request.override(tools=self._visible(request.tools)))


# D8 常驻名单：文件/执行原语 + 交互 + 模式切换 + 发现入口 + 编辑审批常用。
RESIDENT_TOOL_NAMES: frozenset[str] = frozenset({
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "task",
    "ask_user",
    "enter_plan_mode",
    "exit_plan_mode",
    "tool_search",
    "apply_patch",
    "delete_file",
})

# D8 deferred 名单：低频/场景特定内置工具，经 tool_search 发现后 reveal。
DEFERRED_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
    "lsp",
    "monitor",
    "task_output",
    "task_stop",
    "web_search",
    "web_fetch",
    "memory_save",
    "memory_search",
})
