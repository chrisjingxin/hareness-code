"""HC-171 Run 级动态审批状态的 TDD 回归。"""

from __future__ import annotations

import pytest

from harness_agent.runtime.run_context import ApprovalModeState, current_approval_mode


def test_approval_mode_state_increments_revision_only_on_change() -> None:
    """相同切换幂等，实际 mode 改变才推进单调 revision。"""
    state = ApprovalModeState("default")
    assert state.snapshot() == ("default", 0)
    assert state.set("default") == ("default", 0)
    assert state.set("yolo") == ("yolo", 1)
    assert state.set("auto") == ("auto", 2)


def test_current_approval_mode_uses_bounded_provider_view() -> None:
    """子 Run 可读取父状态的动态求交视图，而不复制旧 mode。"""
    state = ApprovalModeState("default")
    view = type(
        "Context",
        (),
        {
            "approval_mode": "default",
            "approval_state": state,
            "approval_mode_provider": lambda self: "auto-edit",
        },
    )()
    assert current_approval_mode(view) == "auto-edit"
    state.set("yolo")
    assert current_approval_mode(view) == "auto-edit"


def test_approval_mode_state_rejects_invalid_mode() -> None:
    """状态对象不能接受协议枚举之外的权限语义。"""
    with pytest.raises(ValueError, match="APPROVAL_MODE_INVALID"):
        ApprovalModeState("unknown")  # type: ignore[arg-type]


def test_inline_child_prompt_uses_current_parent_approval_mode() -> None:
    """Inline child 的 system prompt 不能继续携带构图时的旧审批档位。"""
    from types import SimpleNamespace

    from langchain_core.messages import SystemMessage

    from harness_agent.runtime.run_context import ApprovalModePromptMiddleware

    state = ApprovalModeState("default")
    context = SimpleNamespace(
        approval_mode="auto-edit",
        approval_state=state,
        approval_mode_provider=lambda: "auto-edit",
        plan_constraint=SimpleNamespace(active=False),
    )
    captured: list[str] = []

    async def handler(request):
        captured.append(str(request.system_message.content))
        return SimpleNamespace()

    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        system_message=SystemMessage(content="child prompt\n\n## 审批模式：自动编辑\n\n旧事实"),
        override=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    import asyncio

    asyncio.run(ApprovalModePromptMiddleware("auto-edit").awrap_model_call(request, handler))
    assert captured[-1].count("## 审批模式：") == 1
    assert "审批模式：自动编辑" in captured[-1]

    context.approval_mode_provider = lambda: "yolo"
    asyncio.run(ApprovalModePromptMiddleware("auto-edit").awrap_model_call(request, handler))
    assert captured[-1].count("## 审批模式：") == 1
    assert "审批模式：YOLO" in captured[-1]
    assert "审批模式：自动编辑" not in captured[-1]
