"""za38 Agent 内核使用的 OpenAI 兼容网关适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import openai
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, BaseMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.callbacks.manager import AsyncCallbackManagerForLLMRun
from langchain_openai.chat_models.base import (
    _handle_openai_api_error,
    _handle_openai_bad_request,
)
try:  # pragma: no cover - packaging guard
    from langchain_openai import ChatOpenAI
except ImportError as exc:  # pragma: no cover - packaging guard
    raise RuntimeError(
        "OpenAI-compatible model support is not installed. "
        "Install the za38-agent runtime with its declared dependencies."
    ) from exc

from harness_agent.config.config import ModelSettings
from harness_agent.runtime.resource_lifecycle import (
    ResourceScope,
    ResourceState,
    SharedResourceHandle,
    SharedResourceLease,
)


class ProviderClientPool:
    """Sidecar 级 OpenAI-compatible 无认证 HTTP transport 池。

    transport 不携带 API Key、Header 或模型名；认证仍由每个 AgentEngine 的
    ChatOpenAI 适配器持有，避免不同 Profile 在共享连接上串用凭据。
    """

    def __init__(self) -> None:
        """初始化惰性连接表和串行创建锁。"""
        self._resources: dict[str, SharedResourceHandle[httpx.AsyncClient]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, settings: ModelSettings) -> SharedResourceLease[httpx.AsyncClient]:
        """按 endpoint 与超时取得带引用计数的 Host 级 transport 租约。"""
        key = self._transport_key(settings)
        async with self._lock:
            resource = self._resources.get(key)
            if resource is None or resource.state is not ResourceState.READY:
                client = httpx.AsyncClient(timeout=settings.timeout_seconds)
                resource = SharedResourceHandle(
                    name=f"provider-http:{key[:12]}",
                    scope=ResourceScope.HOST,
                    value=client,
                    close=client.aclose,
                )
                self._resources[key] = resource
        return await resource.acquire()

    async def aclose(self) -> None:
        """在 sidecar 退出时关闭所有共享 transport，失败不阻断其余关闭。"""
        async with self._lock:
            resources, self._resources = list(self._resources.values()), {}
        for resource in resources:
            await resource.begin_draining(reason="host_shutdown")
            try:
                await resource.close()
            except Exception:
                # Host 关闭顺序保证引擎租约已释放；这里仍以 force 作为最后的
                # shutdown 兜底，避免一个异常 transport 阻断其他 Host 资源释放。
                await resource.close(force=True)

    @staticmethod
    def _transport_key(settings: ModelSettings) -> str:
        """计算不含凭据、模型名或 Header 的共享 transport 身份。"""
        return hashlib.sha256(json.dumps({
            "base_url": settings.base_url,
            "timeout_seconds": settings.timeout_seconds,
        }, sort_keys=True).encode("utf-8")).hexdigest()


def create_openai_compatible_model(
    settings: ModelSettings,
    *,
    async_client: httpx.AsyncClient | None = None,
) -> BaseChatModel:
    """根据已解析的非秘密配置创建 v0.1 唯一支持的模型适配器。"""
    kwargs: dict[str, object] = {
        "model": settings.name,
        "base_url": settings.base_url,
        "api_key": settings.resolve_api_key(),
        "use_responses_api": False,
        "timeout": settings.timeout_seconds,
        "max_retries": settings.max_retries,
        "default_headers": settings.resolve_headers(),
    }
    if async_client is not None:
        kwargs["http_async_client"] = async_client
    if settings.reasoning is not None:
        # ChatOpenAI 的 reasoning_effort 是 Chat Completions 参数；不能传
        # Responses 专属 reasoning/output_version，以免兼容网关改走 /responses。
        kwargs["reasoning_effort"] = settings.reasoning.effort
    model = _ReasoningAwareChatOpenAI(**kwargs)
    # LangChain/DeepAgents 的预算中间件读取 profile；企业网关不会可靠地返回
    # 模型窗口，因此使用经配置校验后的保守显式值。
    model.profile = {"max_input_tokens": settings.context_window_tokens}
    return model


class _ReasoningAwareChatOpenAI(ChatOpenAI):
    """在 Chat Completions 流上保留私有 reasoning_content 的 ChatOpenAI 子类。

    openai SDK 的 pydantic chunk 模型会丢弃 ``delta.reasoning_content`` 等
    供应商私有字段。DeepSeek/MiMo 系网关把完整思维链放在该字段中；为了让
    TUI/Web 能展示“模型正在思考什么”，本类在 Chat Completions 流式路径上
    直接用 raw SSE 解析原始 chunk，把 ``reasoning_content`` 注入消息的
    ``additional_kwargs``，其余正文、工具调用、usage 转换全部复用父类逻辑。
    Responses API 与结构化输出路径保持父类原样。
    """

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        *,
        stream_usage: bool | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        kwargs["stream"] = True
        stream_usage = self._should_stream_usage(stream_usage, **kwargs)
        if stream_usage:
            kwargs["stream_options"] = {"include_usage": stream_usage}
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        # 结构化输出或 Responses 模式没有可确认的 reasoning_content 语义，
        # 保持父类原样，避免 raw 解析破坏这些路径。
        if "response_format" in payload or self._use_responses_api(payload):
            async for chunk in super()._astream(
                messages, stop=stop, run_manager=run_manager,
                stream_usage=stream_usage, **kwargs,
            ):
                yield chunk
            return
        default_chunk_class: type[BaseMessageChunk] = AIMessageChunk
        stream = await self.async_client.create(**payload)
        try:
            async for chunk_dict in _iter_sse_json(stream.response):
                    generation_chunk = self._convert_chunk_to_generation_chunk(
                        chunk_dict,
                        default_chunk_class,
                        {},
                    )
                    if generation_chunk is None:
                        continue
                    default_chunk_class = generation_chunk.message.__class__
                    reasoning = _delta_reasoning_content(chunk_dict)
                    if reasoning:
                        generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
                    if run_manager:
                        await run_manager.on_llm_new_token(
                            generation_chunk.text,
                            chunk=generation_chunk,
                            logprobs=(generation_chunk.generation_info or {}).get("logprobs"),
                        )
                    yield generation_chunk
        except openai.BadRequestError as e:
            _handle_openai_bad_request(e)
        except openai.APIError as e:
            _handle_openai_api_error(e)
        finally:
            await stream.close()


async def _iter_sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把 Chat Completions SSE 字节流逐行解析为原始 chunk dict。"""
    buffer = b""
    async for data in response.aiter_bytes():
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return
            if payload:
                yield json.loads(payload)


def _delta_reasoning_content(chunk: Mapping[str, Any]) -> str:
    """从原始 chunk dict 提取 Chat Completions 的 reasoning_content 增量。"""
    try:
        delta = chunk["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return ""
    value = delta.get("reasoning_content") if isinstance(delta, Mapping) else None
    return value if isinstance(value, str) and value else ""


def resolve_model(model: BaseChatModel) -> BaseChatModel:
    """保持 Agent 工厂接收模型对象的契约。

    v0.1 有意不支持字符串 Provider 解析；唯一模型来源是
    :mod:`harness_agent.config.config` 中的 OpenAI 兼容配置。
    """
    return model
