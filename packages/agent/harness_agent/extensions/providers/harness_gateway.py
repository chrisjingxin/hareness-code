"""za38 Agent 内核使用的 OpenAI 兼容网关适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
from langchain_core.language_models.chat_models import BaseChatModel

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
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "OpenAI-compatible model support is not installed. "
            "Install the za38-agent runtime with its declared dependencies."
        ) from exc

    kwargs: dict[str, object] = {
        "model": settings.name,
        "base_url": settings.base_url,
        "api_key": settings.resolve_api_key(),
        "timeout": settings.timeout_seconds,
        "max_retries": settings.max_retries,
        "default_headers": settings.resolve_headers(),
    }
    if async_client is not None:
        kwargs["http_async_client"] = async_client
    if settings.reasoning is not None:
        # 通过 canonical ModelSettings 显式选择 Responses block；不使用
        # extra_body，避免 reasoning 选项脱离 AgentEngine Profile 身份。
        kwargs["reasoning"] = settings.reasoning.to_payload()
        kwargs["output_version"] = "responses/v1"
    model = ChatOpenAI(**kwargs)
    # LangChain/DeepAgents 的预算中间件读取 profile；企业网关不会可靠地返回
    # 模型窗口，因此使用经配置校验后的保守显式值。
    model.profile = {"max_input_tokens": settings.context_window_tokens}
    return model


def resolve_model(model: BaseChatModel) -> BaseChatModel:
    """保持 Agent 工厂接收模型对象的契约。

    v0.1 有意不支持字符串 Provider 解析；唯一模型来源是
    :mod:`harness_agent.config.config` 中的 OpenAI 兼容配置。
    """
    return model
