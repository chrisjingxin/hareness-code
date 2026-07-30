"""工具并发安全分类、异步读写锁与并发守卫中间件回归测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.concurrency import (
    AsyncRWLock,
    is_concurrency_safe,
    is_shell_command_read_only,
)
from harness_agent.concurrency_guard import ConcurrencyGuardMiddleware


# ---------------------------------------------------------------------------
# is_concurrency_safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "glob", "grep"],
)
def test_read_only_tools_are_concurrency_safe(tool_name: str):
    """只读工具始终可安全并行。"""
    assert is_concurrency_safe(tool_name, {}) is True


@pytest.mark.parametrize(
    "tool_name",
    ["write_file", "edit_file"],
)
def test_write_tools_are_not_concurrency_safe(tool_name: str):
    """写工具始终不可并行。"""
    assert is_concurrency_safe(tool_name, {}) is False


def test_subagent_tool_is_not_concurrency_safe():
    """默认子 Agent 可能写同一工作区，task 必须独占。"""
    assert is_concurrency_safe("task", {"prompt": "do something"}) is False


def test_execute_with_read_only_command_is_safe():
    """execute 工具携带只读命令时可并行。"""
    assert is_concurrency_safe("execute", {"command": "git status"}) is True


def test_execute_with_write_command_is_not_safe():
    """execute 工具携带写命令时不可并行。"""
    assert is_concurrency_safe("execute", {"command": "rm -rf /tmp"}) is False


def test_unknown_tool_fails_closed():
    """未知工具保守返回 False（fail-closed）。"""
    assert is_concurrency_safe("some_unknown_tool", {}) is False


# ---------------------------------------------------------------------------
# is_shell_command_read_only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff",
        "ls -la",
        "cat file.txt",
        "grep -r pattern .",
        "head -20 file",
        "tail -f log",
        "pwd",
        "whoami",
        "find . -name '*.py'",
        "wc -l file",
    ],
)
def test_read_only_shell_commands(command: str):
    """白名单内的只读 shell 命令判定为安全。"""
    assert is_shell_command_read_only(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/x",
        "mv a b",
        "git push origin main",
        "git commit -m 'msg'",
        "git reset --hard",
        "mkdir new_dir",
        "echo hello > file.txt",
        "cat a >> b",
        "sed -i 's/a/b/' file",
        "find . -delete",
        "git branch -D feature",
    ],
)
def test_write_shell_commands(command: str):
    """包含写操作或破坏性参数的命令判定为不安全。"""
    assert is_shell_command_read_only(command) is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status && ls", True),
        ("git status && rm file", False),
        ("ls; pwd", True),
    ],
)
def test_compound_shell_commands(command: str, expected: bool):
    """复合命令要求所有子命令均为只读才判定安全。"""
    assert is_shell_command_read_only(command) is expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat file | grep pattern", True),
        ("cat file | tee output", False),
    ],
)
def test_pipe_shell_commands(command: str, expected: bool):
    """管道命令要求每一段均为只读才判定安全。"""
    assert is_shell_command_read_only(command) is expected


@pytest.mark.parametrize(
    "command",
    ["", "   ", "unknown_cmd", "git 'unterminated"],
)
def test_edge_case_shell_commands_fail_closed(command: str):
    """空字符串、未知命令和畸形引号保守返回 False。"""
    assert is_shell_command_read_only(command) is False


# ---------------------------------------------------------------------------
# AsyncRWLock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rwlock_multiple_readers_concurrent():
    """多个读者可同时持有读锁。"""
    rwlock = AsyncRWLock()
    active_readers = 0
    max_readers = 0
    lock = asyncio.Lock()

    async def reader() -> None:
        nonlocal active_readers, max_readers
        await rwlock.acquire_read()
        try:
            async with lock:
                active_readers += 1
                if active_readers > max_readers:
                    max_readers = active_readers
            await asyncio.sleep(0.02)
        finally:
            async with lock:
                active_readers -= 1
            await rwlock.release_read()

    await asyncio.gather(*(reader() for _ in range(4)))
    assert max_readers == 4


@pytest.mark.asyncio
async def test_rwlock_writer_blocks_until_readers_release():
    """写者等待所有读者释放后才能获取写锁。"""
    rwlock = AsyncRWLock()
    events: list[str] = []

    async def reader() -> None:
        await rwlock.acquire_read()
        events.append("reader_acquired")
        await asyncio.sleep(0.03)
        await rwlock.release_read()
        events.append("reader_released")

    async def writer() -> None:
        # 稍后启动确保读者先获取锁
        await asyncio.sleep(0.01)
        await rwlock.acquire_write()
        events.append("writer_acquired")
        await rwlock.release_write()

    await asyncio.gather(reader(), writer())
    # 写者获取锁必须在读者释放之后
    assert events.index("writer_acquired") > events.index("reader_released")


@pytest.mark.asyncio
async def test_rwlock_writer_is_exclusive():
    """第二个写者必须等待第一个写者释放。"""
    rwlock = AsyncRWLock()
    active_writers = 0
    max_writers = 0
    lock = asyncio.Lock()

    async def writer() -> None:
        nonlocal active_writers, max_writers
        await rwlock.acquire_write()
        try:
            async with lock:
                active_writers += 1
                if active_writers > max_writers:
                    max_writers = active_writers
            await asyncio.sleep(0.02)
        finally:
            async with lock:
                active_writers -= 1
            await rwlock.release_write()

    await asyncio.gather(writer(), writer(), writer())
    assert max_writers == 1


# ---------------------------------------------------------------------------
# ConcurrencyGuardMiddleware
# ---------------------------------------------------------------------------


def _make_request(tool_name: str, args: dict | None = None) -> SimpleNamespace:
    """构造模拟 ToolCallRequest。"""
    return SimpleNamespace(
        tool_call={"name": tool_name, "id": "test-call", "args": args or {}}
    )


@pytest.mark.asyncio
async def test_middleware_read_tool_handler_called():
    """只读工具通过读锁执行 handler 并返回结果。"""
    middleware = ConcurrencyGuardMiddleware(AsyncRWLock())
    request = _make_request("read_file", {"path": "a.txt"})

    async def handler(req: Any) -> str:
        return "read_result"

    result = await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert result == "read_result"


@pytest.mark.asyncio
async def test_middleware_write_tool_handler_called():
    """写工具通过写锁执行 handler 并返回结果。"""
    middleware = ConcurrencyGuardMiddleware(AsyncRWLock())
    request = _make_request("write_file", {"path": "b.txt", "content": "x"})

    async def handler(req: Any) -> str:
        return "write_result"

    result = await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert result == "write_result"


@pytest.mark.asyncio
async def test_middleware_multiple_reads_concurrent():
    """多个只读工具可并发执行（读锁共享）。"""
    middleware = ConcurrencyGuardMiddleware(AsyncRWLock())
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def handler(req: Any) -> str:
        nonlocal active, max_active
        async with lock:
            active += 1
            if active > max_active:
                max_active = active
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return "ok"

    requests = [_make_request("read_file", {"path": f"f{i}"}) for i in range(4)]
    await asyncio.gather(
        *(middleware.awrap_tool_call(req, handler) for req in requests)  # type: ignore[arg-type]
    )
    assert max_active == 4


@pytest.mark.asyncio
async def test_middleware_write_blocks_concurrent_reads():
    """写工具持有写锁期间阻塞只读工具。"""
    middleware = ConcurrencyGuardMiddleware(AsyncRWLock())
    events: list[str] = []

    async def write_handler(req: Any) -> str:
        events.append("write_start")
        await asyncio.sleep(0.04)
        events.append("write_end")
        return "written"

    async def read_handler(req: Any) -> str:
        events.append("read_start")
        return "read"

    write_req = _make_request("write_file", {"path": "x"})
    read_req = _make_request("read_file", {"path": "y"})

    # 写先启动，读稍后启动但应被阻塞到写完成
    write_task = asyncio.create_task(
        middleware.awrap_tool_call(write_req, write_handler)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    read_task = asyncio.create_task(
        middleware.awrap_tool_call(read_req, read_handler)  # type: ignore[arg-type]
    )
    await asyncio.gather(write_task, read_task)

    # 读操作必须在写操作结束之后才能开始
    assert events.index("read_start") > events.index("write_end")


@pytest.mark.asyncio
async def test_shared_middleware_lock_allows_cross_graph_reads():
    """两张图注入同一把锁时，只读工具仍可并行执行。"""
    shared_lock = AsyncRWLock()
    first = ConcurrencyGuardMiddleware(shared_lock)
    second = ConcurrencyGuardMiddleware(shared_lock)
    active = 0
    max_active = 0
    counter_lock = asyncio.Lock()

    async def handler(req: Any) -> str:
        nonlocal active, max_active
        async with counter_lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        async with counter_lock:
            active -= 1
        return "read"

    await asyncio.gather(
        first.awrap_tool_call(_make_request("read_file"), handler),  # type: ignore[arg-type]
        second.awrap_tool_call(_make_request("read_file"), handler),  # type: ignore[arg-type]
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_shared_middleware_lock_blocks_cross_graph_read_after_write():
    """一张图写入时，另一张图的读取必须等待同一 Host 锁释放。"""
    shared_lock = AsyncRWLock()
    writer = ConcurrencyGuardMiddleware(shared_lock)
    reader = ConcurrencyGuardMiddleware(shared_lock)
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    read_started = asyncio.Event()

    async def write_handler(req: Any) -> str:
        write_started.set()
        await release_write.wait()
        return "written"

    async def read_handler(req: Any) -> str:
        read_started.set()
        return "read"

    write_task = asyncio.create_task(
        writer.awrap_tool_call(_make_request("write_file"), write_handler)  # type: ignore[arg-type]
    )
    await write_started.wait()
    read_task = asyncio.create_task(
        reader.awrap_tool_call(_make_request("read_file"), read_handler)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    assert read_started.is_set() is False

    release_write.set()
    await asyncio.gather(write_task, read_task)
    assert read_started.is_set() is True


@pytest.mark.asyncio
async def test_middleware_releases_lock_after_cancelled_write():
    """取消写工具后必须释放锁，后续读取不能永久阻塞。"""
    middleware = ConcurrencyGuardMiddleware(AsyncRWLock())
    write_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def write_handler(req: Any) -> str:
        write_started.set()
        await never_finish.wait()
        return "unreachable"

    write_task = asyncio.create_task(
        middleware.awrap_tool_call(_make_request("write_file"), write_handler)  # type: ignore[arg-type]
    )
    await write_started.wait()
    write_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write_task

    result = await middleware.awrap_tool_call(
        _make_request("read_file"),
        lambda req: _return("read"),
    )
    assert result == "read"


async def _return(value: str) -> str:
    """为取消回归测试提供无副作用的异步 handler。"""
    return value
