"""Claude Plugin Hook、LSP、Monitor 运行目录与进程清理测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.runtime import HookRunner, PluginRuntimeManager


def _write_json(path: Path, value: object) -> None:
    """写入测试 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _runtime_plugin(root: Path) -> None:
    """创建包含 command Hook、stdio LSP 和 Monitor 的 Claude Plugin。"""
    _write_json(
        root / ".claude-plugin" / "plugin.json",
        {"name": "runtime-suite", "version": "1.0.0"},
    )
    hook_script = root / "scripts" / "hook.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text(
        """
import json
import os
import sys

value = json.load(sys.stdin)
assert "HARNESS_RUNTIME_SECRET" not in os.environ
command = str(value.get("tool_input", {}).get("command", ""))
if "danger" in command:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "danger blocked",
        }
    }))
else:
    print("{}")
""".strip(),
        encoding="utf-8",
    )
    _write_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": sys.executable,
                                "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/hook.py"],
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        },
    )
    lsp_script = root / "scripts" / "lsp.py"
    lsp_script.write_text(
        """
import json
import sys

def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        if line in (b"\\r\\n", b"\\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return json.loads(sys.stdin.buffer.read(length))

def write(value):
    body = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode() + body)
    sys.stdout.buffer.flush()

while True:
    message = read()
    method = message.get("method")
    if "id" not in message:
        if method == "exit":
            break
        continue
    if method == "initialize":
        result = {"capabilities": {"definitionProvider": True}}
    elif method == "textDocument/definition":
        uri = message["params"]["textDocument"]["uri"]
        result = [{"uri": uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]
    elif method == "shutdown":
        result = None
    else:
        result = []
    write({"jsonrpc": "2.0", "id": message["id"], "result": result})
""".strip(),
        encoding="utf-8",
    )
    _write_json(
        root / ".lsp.json",
        {
            "toy": {
                "command": sys.executable,
                "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/lsp.py"],
                "extensionToLanguage": {".toy": "toy"},
            }
        },
    )
    _write_json(
        root / "monitors" / "monitors.json",
        [
            {
                "name": "ready",
                "description": "test monitor",
                "command": "printf 'ready\\n'; sleep 30",
            }
        ],
    )


async def test_runtime_catalog_executes_hook_lsp_monitor_and_closes(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """三个运行组件使用同一启用快照，且关闭后没有活动子进程。"""
    source = tmp_path / "runtime-plugin"
    source.mkdir()
    _runtime_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    fingerprint = str(installed["capability_fingerprint"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=fingerprint,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.toy"
    target.write_text("symbol", encoding="utf-8")
    monkeypatch.setenv("HARNESS_RUNTIME_SECRET", "must-not-leak")  # type: ignore[attr-defined]

    catalog = manager.runtime_catalog(manager.catalog(), workspace=workspace)
    assert len(catalog.hooks) == 1
    assert len(catalog.lsp_servers) == 1
    assert len(catalog.monitors) == 1
    assert catalog.diagnostics == ()
    runtime = PluginRuntimeManager(catalog)
    await runtime.start()

    hook_results = await runtime.hooks.run(
        "PreToolUse",
        tool_name="execute",
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "danger"},
        },
    )
    assert hook_results[0].blocks_pre_tool == (True, "danger blocked")

    for _ in range(100):
        if "ready" in runtime.monitors.context():
            break
        await asyncio.sleep(0.01)
    assert "ready" in runtime.monitors.context()

    result = await runtime.lsp.query(
        "definition",
        "sample.toy",
        1,
        1,
        str(workspace),
    )
    assert result["server"] == "toy"
    assert isinstance(result["results"], list)
    assert result["results"][0]["uri"] == target.as_uri()

    processes = tuple(runtime.monitors._processes.values())
    clients = tuple(runtime.lsp._clients.values())
    await runtime.aclose()
    assert all(process.returncode is not None for process in processes)
    assert all(client._process is None for client in clients)


async def test_hook_runner_exit_two_blocks_and_nonblocking_error_continues(
    tmp_path: Path,
) -> None:
    """command Hook 遵循 Claude exit 2 阻断、其他非零不阻断规则。"""
    from types import SimpleNamespace

    from langchain_core.messages import ToolMessage

    from harness_agent.plugins.runtime import (
        HookDefinition,
        MonitorManager,
        PluginRuntimeMiddleware,
    )

    definitions = tuple(
        HookDefinition(
            plugin_id="test",
            event="PreToolUse",
            matcher="Bash",
            command=command,
            args=None,
            timeout_seconds=5,
            asynchronous=False,
            shell=None,
            root=tmp_path,
            data=tmp_path,
            workspace=tmp_path,
        )
        for command in (
            "printf 'blocked' >&2; exit 2",
            "printf 'warning' >&2; exit 1",
        )
    )
    runner = HookRunner(definitions)
    results = await runner.run(
        "PreToolUse",
        tool_name="execute",
        payload={"tool_input": {"command": "anything"}},
    )
    assert results[0].blocks_pre_tool == (True, "blocked")
    assert results[1].blocks_pre_tool == (False, "")
    middleware = PluginRuntimeMiddleware(runner, MonitorManager(()))
    called = False

    async def handler(_request: object) -> str:
        nonlocal called
        called = True
        return "executed"

    response = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool_call={
                "id": "tool-1",
                "name": "execute",
                "args": {"command": "anything"},
            },
            runtime=SimpleNamespace(context=None),
        ),
        handler,
    )
    assert isinstance(response, ToolMessage)
    assert response.status == "error"
    assert called is False
    await runner.aclose()


async def test_plugin_runtime_middleware_uses_catalog_workspace_for_run_context(
    tmp_path: Path,
) -> None:
    """真实 RunContext 不携带 workspace 时 Hook 仍能读取 Skill。"""
    from types import SimpleNamespace

    from harness_agent.plugins.runtime import (
        HookDefinition,
        MonitorManager,
        PluginRuntimeCatalog,
        PluginRuntimeManager,
    )
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.runtime.run_context import RunContext

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition = HookDefinition(
        plugin_id="test",
        event="PreToolUse",
        matcher="Read",
        command="printf '{}'",
        args=None,
        timeout_seconds=5,
        asynchronous=False,
        shell=None,
        root=tmp_path,
        data=tmp_path,
        workspace=workspace,
    )
    manager = PluginRuntimeManager(
        PluginRuntimeCatalog(workspace=workspace, hooks=(definition,))
    )
    context = RunContext(
        thread_id="thread-1",
        run_id="run-1",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="thread-1",
            system_prompt="test",
            workspace=str(workspace),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
    )
    called = False

    async def handler(_request: object) -> str:
        nonlocal called
        called = True
        return "read complete"

    response = await manager.middleware.awrap_tool_call(
        SimpleNamespace(
            tool_call={
                "id": "tool-1",
                "name": "read_file",
                "args": {"file_path": "/.harness/skills/demo/SKILL.md"},
            },
            runtime=SimpleNamespace(context=context),
        ),
        handler,
    )
    assert response == "read complete"
    assert called is True
    await manager.aclose()


@pytest.mark.skipif(os.name == "nt", reason="进程存活探针使用 POSIX signal 0")
async def test_async_hook_process_is_terminated_on_runner_close(tmp_path: Path) -> None:
    """Host 关闭取消异步 Hook 时必须同时终止其外部进程。"""
    from harness_agent.plugins.runtime import HookDefinition

    script = tmp_path / "long_hook.py"
    pid_file = tmp_path / "hook.pid"
    script.write_text(
        """
import os
import pathlib
import sys
import time

pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
""".strip(),
        encoding="utf-8",
    )
    runner = HookRunner(
        (
            HookDefinition(
                plugin_id="test",
                event="PostToolUse",
                matcher="Bash",
                command=sys.executable,
                args=(str(script), str(pid_file)),
                timeout_seconds=60,
                asynchronous=True,
                shell=None,
                root=tmp_path,
                data=tmp_path,
                workspace=tmp_path,
            ),
        )
    )
    await runner.run(
        "PostToolUse",
        tool_name="execute",
        payload={"tool_input": {"command": "anything"}},
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    process_id = int(pid_file.read_text(encoding="utf-8"))

    await runner.aclose()

    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
