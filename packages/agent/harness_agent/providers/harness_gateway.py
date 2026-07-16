"""za38 Agent 内核使用的 OpenAI 兼容网关适配器。"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from harness_agent.config import ModelSettings


def create_openai_compatible_model(settings: ModelSettings) -> BaseChatModel:
    """根据已解析的非秘密配置创建 v0.1 唯一支持的模型适配器。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "OpenAI-compatible model support is not installed. "
            "Install the za38-agent runtime with its declared dependencies."
        ) from exc

    return ChatOpenAI(
        model=settings.name,
        base_url=settings.base_url,
        api_key=settings.resolve_api_key(),
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        default_headers=settings.resolve_headers(),
    )


def resolve_model(model: BaseChatModel) -> BaseChatModel:
    """保持 Agent 工厂接收模型对象的契约。

    v0.1 有意不支持字符串 Provider 解析；唯一模型来源是
    :mod:`harness_agent.config` 中的 OpenAI 兼容配置。
    """
    return model
