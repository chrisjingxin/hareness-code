#!/usr/bin/env python3
"""用 LangChain 假模型验证修补后的 DeepAgents 运行时入口。"""

from __future__ import annotations

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


def main() -> int:
    """验证显式模型可构建，隐式供应商选择被拒绝。"""

    model = FakeMessagesListChatModel(responses=[AIMessage(content="dependency-smoke")])
    create_deep_agent(model=model, name="dependency-smoke")
    try:
        create_deep_agent(name="implicit-provider-smoke")
    except ValueError as error:
        if "explicit model" not in str(error):
            raise RuntimeError(f"unexpected implicit-model error: {error}") from error
    else:
        raise RuntimeError("DeepAgents accepted an implicit provider-specific model")
    print("DeepAgents runtime smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
