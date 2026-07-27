"""并发安全分区、异步读写锁与并发守卫中间件回归测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.concurrency import (
    AsyncRWLock,
    ToolBatch,
    execute_batches,
    get_max_concurrency,
    is_concurrency_safe,
    is_shell_command_read_only,
    partition_tool_calls,
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


def test_subagent_tool_is_concurrency_safe():
    """子代理工具拥有隔离上下文，可安全并行。"""
    assert is_concurrency_safe("task", {"prompt": "do something"}) is True


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
# partition_tool_calls
# ---------------------------------------------------------------------------


def _call(name: str, args: dict | None = None) -> dict:
    """构造工具调用字典辅助函数。"""
    return {"name": name, "args": args or {}}


def test_partition_all_read_only_into_single_parallel_batch():
    """全部只读工具合并为单个并行批次。"""
    calls = [_call("read_file"), _call("read_file"), _call("grep")]
    batches = partition_tool_calls(calls)

    assert len(batches) == 1
    assert batches[0].concurrent is True
    assert batches[0].calls == calls


def test_partition_mixed_read_write_read():
    """读写混合产生三个批次：并行(读)、串行(写)、串行(读)。"""
    read1 = _call("read_file")
    write = _call("write_file")
    read2 = _call("grep")
    batches = partition_tool_calls([read1, write, read2])

    assert len(batches) == 3
    assert batches[0].concurrent is False  # 单个读降级为串行
    assert batches[0].calls == [read1]
    assert batches[1].concurrent is False
    assert batches[1].calls == [write]
    assert batches[2].concurrent is False
    assert batches[2].calls == [read2]


def test_partition_all_write_tools():
    """全部写工具各自独立为串行批次。"""
    write = _call("write_file")
    edit = _call("edit_file")
    batches = partition_tool_calls([write, edit])

    assert len(batches) == 2
    assert all(not b.concurrent for b in batches)
    assert batches[0].calls == [write]
    assert batches[1].calls == [edit]


def test_partition_single_tool_degrades_to_sequential():
    """仅含单个调用的批次降级为串行（无并行收益）。"""
    read = _call("read_file")
    batches = partition_tool_calls([read])

    assert len(batches) == 1
    assert batches[0].concurrent is False
    assert batches[0].calls == [read]


def test_partition_subagents_into_single_parallel_batch():
    """多个子代理工具合并为单个并行批次。"""
    calls = [_call("task"), _call("task"), _call("task")]
    batches = partition_tool_calls(calls)

    assert len(batches) == 1
    assert batches[0].concurrent is True
    assert batches[0].calls == calls


def test_partition_mixed_with_execute():
    """只读工具与只读 execute 合并为并行批次，写工具独立串行。"""
    read = _call("read_file")
    execute = _call("execute", {"command": "git status"})
    write = _call("write_file")
    batches = partition_tool_calls([read, execute, write])

    assert len(batches) == 2
    assert batches[0].concurrent is True
    assert batches[0].calls == [read, execute]
    assert batches[1].concurrent is False
    assert batches[1].calls == [write]


# ---------------------------------------------------------------------------
# execute_batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_parallel_batch_preserves_order():
    """并行批次执行所有调用并保持原始顺序。"""
    calls = [_call("read_file", {"path": f"f{i}"}) for i in range(5)]
    batch = ToolBatch(concurrent=True, calls=calls)

    async def executor(call: dict) -> str:
        await asyncio.sleep(0.01)
        return call["args"]["path"]

    results = await execute_batches([batch], executor)
    assert results == ["f0", "f1", "f2", "f3", "f4"]


@pytest.mark.asyncio
async def test_execute_sequential_batch_in_order():
    """串行批次按顺序逐一执行。"""
    calls = [_call("write_file", {"path": f"w{i}"}) for i in range(3)]
    batch = ToolBatch(concurrent=False, calls=calls)
    execution_order: list[str] = []

    async def executor(call: dict) -> str:
        execution_order.append(call["args"]["path"])
        return call["args"]["path"]

    results = await execute_batches([batch], executor)
    assert results == ["w0", "w1", "w2"]
    assert execution_order == ["w0", "w1", "w2"]


@pytest.mark.asyncio
async def test_execute_mixed_batches_preserve_overall_order():
    """混合批次整体保持原始调用顺序。"""
    read1 = _call("read_file", {"id": "r1"})
    read2 = _call("grep", {"id": "r2"})
    write = _call("write_file", {"id": "w1"})
    read3 = _call("ls", {"id": "r3"})

    batches = [
        ToolBatch(concurrent=True, calls=[read1, read2]),
        ToolBatch(concurrent=False, calls=[write]),
        ToolBatch(concurrent=False, calls=[read3]),
    ]

    async def executor(call: dict) -> str:
        await asyncio.sleep(0.005)
        return call["args"]["id"]

    results = await execute_batches(batches, executor)
    assert results == ["r1", "r2", "w1", "r3"]


@pytest.mark.asyncio
async def test_execute_respects_max_concurrency():
    """并行批次内并发数不超过 max_concurrency 限制。"""
    max_concurrent = 0
    current = 0
    lock = asyncio.Lock()

    calls = [_call("read_file", {"id": str(i)}) for i in range(6)]
    batch = ToolBatch(concurrent=True, calls=calls)

    async def executor(call: dict) -> str:
        nonlocal max_concurrent, current
        async with lock:
            current += 1
            if current > max_concurrent:
                max_concurrent = current
        await asyncio.sleep(0.02)
        async with lock:
            current -= 1
        return call["args"]["id"]

    await execute_batches([batch], executor, max_concurrency=2)
    assert max_concurrent <= 2


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
    middleware = ConcurrencyGuardMiddleware()
    request = _make_request("read_file", {"path": "a.txt"})

    async def handler(req: Any) -> str:
        return "read_result"

    result = await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert result == "read_result"


@pytest.mark.asyncio
async def test_middleware_write_tool_handler_called():
    """写工具通过写锁执行 handler 并返回结果。"""
    middleware = ConcurrencyGuardMiddleware()
    request = _make_request("write_file", {"path": "b.txt", "content": "x"})

    async def handler(req: Any) -> str:
        return "write_result"

    result = await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert result == "write_result"


@pytest.mark.asyncio
async def test_middleware_multiple_reads_concurrent():
    """多个只读工具可并发执行（读锁共享）。"""
    middleware = ConcurrencyGuardMiddleware()
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
    middleware = ConcurrencyGuardMiddleware()
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
