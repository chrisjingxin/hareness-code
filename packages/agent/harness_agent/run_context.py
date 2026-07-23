"""单次 Agent Run 的显式上下文和动态 PromptEpoch 注入。

本模块只承载一次调用的 thread、run、提示词与取消状态。它不会写入
LangGraph checkpoint，也不会被 Agent 图长期持有，因此同一编译图可安全
服务多个 thread。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Mapping

from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from harness_agent.approval_mode import ApprovalMode
from harness_agent.prompting import PromptEpoch


class RunContextError(ValueError):
    """Run Context 缺失、归属不一致或提示词不合法时抛出。"""


@dataclass(slots=True)
class RunCancellationToken:
    """一次 run 的协作式取消标记，供后续 scheduler 或 worker 安全观察。"""

    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def cancel(self) -> None:
        """标记取消；实际 Agent task 仍由 server 立即取消以保持现有语义。"""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """返回当前 run 是否已收到取消请求。"""
        return self._event.is_set()

    async def wait(self) -> None:
        """等待取消信号，供未来后台 worker 在安全边界处退出。"""
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class RunContext:
    """传给单次图调用的 thread 私有状态，不属于共享 AgentRuntime。"""

    thread_id: str
    run_id: str
    prompt_epoch: PromptEpoch
    approval_mode: ApprovalMode
    profile_key: str | None = None
    agent_role: str = "primary"
    cancellation_token: RunCancellationToken = field(default_factory=RunCancellationToken)

    def __post_init__(self) -> None:
        """在执行前验证 thread 与 epoch 的绑定，阻止跨 thread 前缀注入。"""
        if not self.thread_id or not self.run_id:
            raise RunContextError("RUN_CONTEXT_ID_INVALID")
        if self.prompt_epoch.thread_id != self.thread_id:
            raise RunContextError("RUN_CONTEXT_PROMPT_EPOCH_THREAD_MISMATCH")
        if not self.agent_role:
            raise RunContextError("RUN_CONTEXT_AGENT_ROLE_INVALID")


def require_run_context(runtime: object) -> RunContext:
    """从 LangGraph runtime 读取已验证的 RunContext，缺失时 fail closed。"""
    context = getattr(runtime, "context", None)
    if not isinstance(context, RunContext):
        raise RunContextError("RUN_CONTEXT_REQUIRED")
    return context


def thread_id_for_runtime(runtime: object) -> str | None:
    """从显式 Context 优先取 thread ID，并校验 configurable 不会串线。"""
    context = getattr(runtime, "context", None)
    if isinstance(context, RunContext):
        config = getattr(runtime, "config", {})
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        configured_thread = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
        if configured_thread is not None and str(configured_thread) != context.thread_id:
            raise RunContextError("RUN_CONTEXT_CONFIG_THREAD_MISMATCH")
        return context.thread_id
    return None


class PromptEpochMiddleware(AgentMiddleware):
    """在模型调用边界按 RunContext 注入 thread 私有的不可变 PromptEpoch。"""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """保留 DeepAgents 基础提示词，并在其前拼接当前 run 的稳定前缀。"""
        context = require_run_context(request.runtime)
        base_prompt = _system_message_text(request.system_message)
        prompt = context.prompt_epoch.system_prompt
        system_prompt = f"{prompt}\n\n{base_prompt}" if base_prompt else prompt
        return await handler(request.override(system_message=SystemMessage(content=system_prompt)))


def _system_message_text(message: object | None) -> str:
    """将 DeepAgents 生成的基础 system message 转为文本，保持现有顺序。"""
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        values: list[str] = []
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                values.append(str(item["text"]))
        return "".join(values).strip()
    return str(content).strip()
