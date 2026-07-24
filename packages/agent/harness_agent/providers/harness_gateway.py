"""za38 Agent 内核使用的 OpenAI 兼容网关适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
from langchain_core.language_models.chat_models import BaseChatModel

from harness_agent.config import ModelSettings


class ProviderClientPool:
    """Sidecar 级 OpenAI-compatible 无认证 HTTP transport 池。

    transport 不携带 API Key、Header 或模型名；认证仍由每个 Runtime 的
    ChatOpenAI 适配器持有，避免不同 Profile 在共享连接上串用凭据。
    """

    def __init__(self) -> None:
        """初始化惰性连接表和串行创建锁。"""
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def get_async_client(self, settings: ModelSettings) -> httpx.AsyncClient:
        """按 endpoint 与超时复用无默认 Header 的异步 HTTP transport。"""
        key = hashlib.sha256(json.dumps({
            "base_url": settings.base_url,
            "timeout_seconds": settings.timeout_seconds,
        }, sort_keys=True).encode("utf-8")).hexdigest()
        async with self._lock:
            client = self._clients.get(key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(timeout=settings.timeout_seconds)
                self._clients[key] = client
            return client

    async def aclose(self) -> None:
        """在 sidecar 退出时关闭所有共享 transport，失败不阻断其余关闭。"""
        async with self._lock:
            clients, self._clients = list(self._clients.values()), {}
        results = await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            # 关闭阶段只需确保其余 transport 继续释放；调用方会记录生命周期日志。
            return


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
    model = ChatOpenAI(**kwargs)
    # LangChain/DeepAgents 的预算中间件读取 profile；企业网关不会可靠地返回
    # 模型窗口，因此使用经配置校验后的保守显式值。
    model.profile = {"max_input_tokens": settings.context_window_tokens}
    return model


def resolve_model(model: BaseChatModel) -> BaseChatModel:
    """保持 Agent 工厂接收模型对象的契约。

    v0.1 有意不支持字符串 Provider 解析；唯一模型来源是
    :mod:`harness_agent.config` 中的 OpenAI 兼容配置。
    """
    return model
