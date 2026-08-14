"""目录信任 resume 后不再重复弹窗的端到端回归。

模拟真实链路：模型连续两次访问工作区外目录；第一次 interrupt 时按
allow_session 注册信任并以 approve resume；第二次同目录调用必须直接执行，
不能再产生 interrupt（否则 TUI 会反复弹目录信任卡片）。

目录信任只保留 allow_session / deny 两个决策。registry 的 once 作用域是
内部单次消费能力（非用户选项）：once 授权只放行当前这次调用，执行后即被
消费，同 run 内再次访问同目录必须重新询问（设计预期，不是缺陷）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


class _ToolCallingFakeChatModel(GenericFakeChatModel):
    """deepagents bind_tools 所需的最小假模型。"""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self


def _external_ls(path: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "ls", "args": {"path": path}, "id": call_id, "type": "tool_call"}],
    )


async def test_session_trust_prevents_second_interrupt(tmp_path: Path) -> None:
    from harness_agent.policy.workspace_roots import WorkspaceRootRegistry
    from harness_agent.runtime.agent import create_harness_agent

    workspace = tmp_path / "ws"
    external = tmp_path / "ext"
    workspace.mkdir()
    external.mkdir()
    (external / "file.txt").write_text("hi", encoding="utf-8")
    ext_str = str(external)

    registry = WorkspaceRootRegistry(workspace, load_persisted=False)
    model = _ToolCallingFakeChatModel(
        messages=iter([_external_ls(ext_str, "c1"), _external_ls(ext_str, "c2"), AIMessage(content="done")])
    )
    model.profile = {"max_input_tokens": 200000}

    agent = create_harness_agent(
        model,
        checkpointer=MemorySaver(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="default",
        workspace_root_registry=registry,
        cwd=str(workspace),
    )
    config = {"configurable": {"thread_id": "trust-repro"}}

    interrupts: list[Any] = []

    async def drain(stream_input: Any) -> None:
        async for event in agent.astream(stream_input, config=config, stream_mode="updates"):
            data = event[1] if isinstance(event, tuple) and len(event) == 2 else event
            if isinstance(data, dict) and data.get("__interrupt__"):
                interrupts.append(data["__interrupt__"][0])

    await drain({"messages": []})
    assert len(interrupts) == 1, "第一次外部访问应弹出目录信任 interrupt"
    first = interrupts[0]

    # 模拟用户选择「本会话信任」+ 批准：先注册信任，再 resume。
    registry.trust(ext_str, "session")
    await drain(Command(resume={first.id: {"decisions": [{"type": "approve"}]}}))

    assert len(interrupts) == 1, (
        f"session 信任后同目录第二次调用不应再 interrupt，实际 {len(interrupts)} 次"
    )


async def test_once_trust_is_single_use_within_same_run(tmp_path: Path) -> None:
    """registry once 作用域只放行当前调用；同 run 内再次访问同目录按设计重新询问。"""
    from harness_agent.policy.workspace_roots import WorkspaceRootRegistry
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    workspace = tmp_path / "ws"
    external = tmp_path / "ext"
    workspace.mkdir()
    external.mkdir()
    (external / "file.txt").write_text("hi", encoding="utf-8")
    ext_str = str(external)

    registry = WorkspaceRootRegistry(workspace, load_persisted=False)
    model = _ToolCallingFakeChatModel(
        messages=iter([_external_ls(ext_str, "c1"), _external_ls(ext_str, "c2"), AIMessage(content="done")])
    )
    model.profile = {"max_input_tokens": 200000}

    # shared_engine 链路才会携带 RunContext（run_id 是 once 作用域的匹配键）。
    agent = create_harness_agent(
        model,
        checkpointer=MemorySaver(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="default",
        workspace_root_registry=registry,
        cwd=str(workspace),
        shared_engine=True,
    )
    context = RunContext(
        thread_id="trust-once",
        run_id="run-once-1",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="trust-once",
            system_prompt="test",
            workspace=str(workspace),
            sandboxed=False,
            provider=None,
            approval_mode="default",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="default",
        workspace_root_registry=registry,
    )
    config = {"configurable": {"thread_id": "trust-once"}}

    interrupts: list[Any] = []

    async def drain(stream_input: Any) -> None:
        async for event in agent.astream(
            stream_input, config=config, stream_mode="updates", context=context
        ):
            data = event[1] if isinstance(event, tuple) and len(event) == 2 else event
            if isinstance(data, dict) and data.get("__interrupt__"):
                interrupts.append(data["__interrupt__"][0])

    await drain({"messages": []})
    assert len(interrupts) == 1, "第一次外部访问应弹出目录信任 interrupt"

    # 模拟用户选择「仅本次允许」+ 批准：注册 once 后 resume。
    registry.trust(ext_str, "once", run_id="run-once-1")
    await drain(Command(resume={interrupts[0].id: {"decisions": [{"type": "approve"}]}}))

    # 获批的当前调用直接执行；但 once 已被消费，同 run 第二次访问重新弹窗。
    assert len(interrupts) == 2, (
        "once 授权放行当前调用后应立即消费；第二次同目录访问必须重新询问，"
        f"实际 interrupt {len(interrupts)} 次"
    )
