"""ThreadPersistence 测试用的最小 Run 受理辅助函数。"""

from __future__ import annotations

from harness_agent.runtime.execution_binding import (
    RunExecutionBinding,
    SafeModelProfile,
    SelectionOrigin,
    ThreadExecutionSelection,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence


def test_binding(thread_id: str, run_id: str) -> RunExecutionBinding:
    """构造不依赖真实配置的最小不可变 Run binding。"""
    return RunExecutionBinding(
        thread_id=thread_id,
        run_id=run_id,
        requested_selection=ThreadExecutionSelection("test"),
        actual_primary=SafeModelProfile(
            profile_id="test",
            model="test-model",
            provider_label="Test",
            context_window_tokens=128_000,
            capabilities=("streaming", "tool-calling"),
            is_default=True,
            available=True,
            unavailable_reason=None,
            source="test",
        ),
        selection_origin=SelectionOrigin.REQUEST,
        runtime_profile_id="test-runtime",
        created_at_ms=1,
    )


async def accept_thread(
    store: ThreadPersistence,
    thread_id: str,
    message: str,
    *,
    run_id: str | None = None,
) -> None:
    """通过唯一的 Run 生命周期入口创建测试 Thread 索引。"""
    await store.accept_run(
        AcceptRun(
            message=message,
            binding=test_binding(thread_id, run_id or f"fixture-{thread_id}-{message}"),
        )
    )
