"""HC-158 Phase 3：Qwen Hook/LSP canonical seam 的离线红绿测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.runtime import PluginRuntimeError, PluginRuntimeManager
from harness_agent.runtime.managed_agent_executor import acquire_pooled_agent_runtime


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"


def _copy_fixture(tmp_path: Path) -> Path:
    """复制不含真实进程的 Qwen fixture。"""
    target = tmp_path / "qwen-phase3"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _manifest(root: Path) -> dict[str, object]:
    """读取测试清单。"""
    return json.loads((root / "devagent-extension.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    """写回测试清单。"""
    (root / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _enabled_manager(
    tmp_path: Path,
    source: Path,
) -> tuple[PluginManager, Path]:
    """安装并显式信任 fixture，返回固定 workspace。"""
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return manager, workspace


def _write_hook_script(source: Path) -> str:
    """写入 fake Hook；只返回安全包内 shell command。"""
    script = source / "scripts" / "phase3-hook.py"
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
event = payload.get("hook_event_name")
tool_input = payload.get("tool_input") or {}
if event == "PreToolUse" and "danger" in str(tool_input.get("command", "")):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "qwen pre blocked",
    }}))
elif event == "PostToolUse":
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "qwen post low trust " + str(payload.get("prompt_id", "")) + "/" + str(payload.get("execution_id", "")),
    }}))
elif event == "PostToolUseFailure":
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUseFailure",
        "additionalContext": "qwen post failure low trust " + str(payload.get("prompt_id", "")) + "/" + str(payload.get("execution_id", "")),
    }}))
else:
    print("{}")
""".strip(),
        encoding="utf-8",
    )
    return 'python3 "${extensionPath}/scripts/phase3-hook.py"'


def _run_context(workspace: Path, thread_id: str, run_id: str, execution_id: str = "root"):
    """构造真实 RunContext，测试不得用缺失身份的伪运行时。"""
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    return RunContext(
        thread_id=thread_id,
        run_id=run_id,
        execution_id=execution_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=thread_id,
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


def _write_lsp_script(source: Path) -> None:
    """写入 fake stdio LSP；进程只处理测试所需的有限方法。"""
    script = source / "scripts" / "phase3-lsp.py"
    script.write_text(
        """
import json
import os
import sys
import time
from pathlib import Path

Path.cwd().joinpath("lsp.pid").write_text(str(os.getpid()), encoding="utf-8")

def read_message():
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

def write_message(value):
    body = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode() + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    method = message.get("method")
    if "id" not in message:
        if method == "exit":
            break
        continue
    if method == "initialize":
        result = {"capabilities": {"definitionProvider": True, "hoverProvider": True}}
    elif method == "textDocument/definition":
        uri = message["params"]["textDocument"]["uri"]
        result = [{"uri": uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]
    elif method == "textDocument/hover":
        time.sleep(30)
        result = {"contents": "late"}
    elif method == "shutdown":
        result = None
    else:
        result = []
    write_message({"jsonrpc": "2.0", "id": message["id"], "result": result})
""".strip(),
        encoding="utf-8",
    )


def _qwen_lsp_manifest(source: Path) -> dict[str, object]:
    """返回包含一个安全 stdio LSP 的 Qwen manifest。"""
    manifest = _manifest(source)
    manifest["lspServers"] = {
        "toy": {
            "transport": "stdio",
            "command": "python3",
            "args": ["${extensionPath}${/}scripts${/}phase3-lsp.py"],
            "workspaceFolder": "${workspacePath}",
            "extensionToLanguage": {".toy": "toy"},
        }
    }
    return manifest


def test_qwen_hook_events_are_effective_per_event_and_unsupported_events_stay_static(
    tmp_path: Path,
) -> None:
    """有效三类 Tool Hook 可运行，Prompt 等无 seam 事件不能借道执行。"""
    source = _copy_fixture(tmp_path)
    command = _write_hook_script(source)
    manifest = _manifest(source)
    manifest["hooks"] = {
        event: [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop")
    }
    manifest["hooks"]["Prompt"] = [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
    ]
    _write_manifest(source, manifest)

    manager = PluginManager(home=tmp_path / "home")
    summary = manager.validate(source)["plugin"]
    assert isinstance(summary, dict)
    hooks = next(item for item in summary["components"] if item["kind"] == "hooks")
    assert hooks["status"] == "adapted"
    assert hooks["effective"] is True
    assert hooks["count"] == 4
    assert any("Prompt" in item for item in hooks["diagnostics"])

    manager, workspace = _enabled_manager(tmp_path, source)
    catalog = manager.runtime_catalog(manager.catalog(), workspace=workspace)
    assert {definition.event for definition in catalog.hooks} == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStop",
    }
    assert any("PLUGIN_QWEN_HOOK_EVENT_UNSUPPORTED: Prompt" in item for item in catalog.diagnostics)


@pytest.mark.asyncio
async def test_qwen_hook_preserves_policy_boundaries_and_post_context_is_low_trust(
    tmp_path: Path,
) -> None:
    """Pre 可阻断、Post 只附加低可信上下文、Failure 保留原错误。"""
    source = _copy_fixture(tmp_path)
    command = _write_hook_script(source)
    manifest = _manifest(source)
    manifest["hooks"] = {
        event: [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
    }
    _write_manifest(source, manifest)
    manager, workspace = _enabled_manager(tmp_path, source)
    runtime = PluginRuntimeManager(
        manager.runtime_catalog(manager.catalog(), workspace=workspace)
    )
    context = _run_context(workspace, "thread-hook", "run-hook")
    called = False

    async def safe_handler(_request: object) -> str:
        nonlocal called
        called = True
        return "real-tool-result"

    request = SimpleNamespace(
        tool_call={"id": "tool-1", "name": "execute", "args": {"command": "danger"}},
        runtime=SimpleNamespace(context=context),
    )
    blocked = await runtime.middleware.awrap_tool_call(request, safe_handler)
    assert getattr(blocked, "status", None) == "error"
    assert called is False

    request.tool_call["args"] = {"command": "safe"}
    result = await runtime.middleware.awrap_tool_call(request, safe_handler)
    assert result == "real-tool-result"
    assert called is True

    captured: dict[str, str] = {}
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model_request = ModelRequest(
        model=model,
        messages=[],
        tools=[],
        runtime=SimpleNamespace(context=context),
    ).override(system_message=SystemMessage(content="base policy"))

    async def model_handler(next_request: ModelRequest) -> ModelResponse:
        captured["system"] = str(next_request.system_message.content)
        return ModelResponse(result=[AIMessage(content="ok")])

    await runtime.middleware.awrap_model_call(model_request, model_handler)
    assert "qwen post low trust" in captured["system"]
    assert "base policy" in captured["system"]

    original = RuntimeError("tool failed")

    async def failing_handler(_request: object) -> str:
        raise original

    with pytest.raises(RuntimeError) as error:
        await runtime.middleware.awrap_tool_call(request, failing_handler)
    assert error.value is original
    await runtime.aclose()


@pytest.mark.asyncio
async def test_qwen_hook_feedback_isolated_by_run_and_missing_context_fails_closed(
    tmp_path: Path,
) -> None:
    """Hook feedback 按 thread/run/execution 隔离，并拒绝没有真实 RunContext 的消费。"""
    source = _copy_fixture(tmp_path)
    command = _write_hook_script(source)
    manifest = _manifest(source)
    manifest["hooks"] = {
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
        ]
    }
    _write_manifest(source, manifest)
    manager, workspace = _enabled_manager(tmp_path, source)
    runtime = PluginRuntimeManager(
        manager.runtime_catalog(manager.catalog(), workspace=workspace)
    )
    context_a = _run_context(workspace, "thread-a", "run-a", "execution-a")
    context_b = _run_context(workspace, "thread-b", "run-b", "execution-b")

    async def handler(_request: object) -> str:
        return "tool-result"

    def request(context: object, call_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            tool_call={"id": call_id, "name": "execute", "args": {"command": "safe"}},
            runtime=SimpleNamespace(context=context),
        )

    await runtime.middleware.awrap_tool_call(request(context_a, "a"), handler)
    await runtime.middleware.awrap_tool_call(request(context_b, "b"), handler)

    seen: dict[str, str] = {}

    async def model_handler(label: str):
        async def handle(next_request: ModelRequest) -> ModelResponse:
            seen[label] = (
                str(next_request.system_message.content)
                if next_request.system_message is not None
                else ""
            )
            return ModelResponse(result=[AIMessage(content="ok")])

        return handle

    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model_b = ModelRequest(
        model=model,
        messages=[],
        tools=[],
        runtime=SimpleNamespace(context=context_b),
    )
    await runtime.middleware.awrap_model_call(model_b, await model_handler("b"))
    model_a = ModelRequest(
        model=model,
        messages=[],
        tools=[],
        runtime=SimpleNamespace(context=context_a),
    )
    await runtime.middleware.awrap_model_call(model_a, await model_handler("a"))
    assert "run-b" in seen["b"]
    assert "run-a" not in seen["b"]
    assert "run-a" in seen["a"]

    with pytest.raises(Exception) as error:
        await runtime.middleware.awrap_model_call(
            ModelRequest(
                model=model,
                messages=[],
                tools=[],
                runtime=SimpleNamespace(context=None),
            ),
            await model_handler("missing"),
        )
    assert getattr(error.value, "code", "") == "PLUGIN_HOOK_RUN_CONTEXT_REQUIRED"

    # 取消只清掉被取消 Run 的一次性反馈；它不能影响其他 Run。
    await runtime.middleware.awrap_tool_call(request(context_a, "a-after"), handler)
    context_a.cancellation_token.cancel()
    cancelled = ModelRequest(
        model=model,
        messages=[],
        tools=[],
        runtime=SimpleNamespace(context=context_a),
    )
    with pytest.raises(PluginRuntimeError) as cancelled_error:
        await runtime.middleware.awrap_model_call(
            cancelled,
            await model_handler("cancelled"),
        )
    assert cancelled_error.value.code == "PLUGIN_HOOK_RUN_CANCELLED"
    assert "cancelled" not in seen

    # Host close 清理 middleware 单例里尚未被消费的其他 Run 反馈。
    await runtime.middleware.awrap_tool_call(request(context_b, "b-before-close"), handler)
    await runtime.aclose()
    after_close = ModelRequest(
        model=model,
        messages=[],
        tools=[],
        runtime=SimpleNamespace(context=context_b),
    )
    await runtime.middleware.awrap_model_call(
        after_close,
        await model_handler("after-close"),
    )
    assert "run-b" not in seen["after-close"]


def test_qwen_hook_freezes_executable_and_windows_style_argv_without_shell_requoting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qwen command 的 argv 保留空格和反斜杠，不依赖 POSIX shell quoting。"""
    import harness_agent.plugins.runtime as runtime_module

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    executable, args = runtime_module._freeze_qwen_shell_command(
        'node "C:\\Program Files\\ZA38\\script.mjs"'
    )

    assert executable == str(node)
    assert args == (r"C:\Program Files\ZA38\script.mjs",)


@pytest.mark.asyncio
async def test_qwen_hook_feedback_release_scopes_managed_children_and_host_run(
    tmp_path: Path,
) -> None:
    """Managed child release 只清自身，Host 的 root release 清理整个 Run。"""
    from harness_agent.host.agent_host import AgentHost

    source = _copy_fixture(tmp_path)
    command = _write_hook_script(source)
    manifest = _manifest(source)
    manifest["hooks"] = {
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
        ],
        "PostToolUseFailure": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
        ],
    }
    _write_manifest(source, manifest)
    manager, workspace = _enabled_manager(tmp_path, source)
    runtime = PluginRuntimeManager(
        manager.runtime_catalog(manager.catalog(), workspace=workspace)
    )
    root = _run_context(workspace, "thread-life", "run-life", "root")
    child_one = _run_context(workspace, "thread-life", "run-life", "child-one")
    child_two = _run_context(workspace, "thread-life", "run-life", "child-two")

    async def successful_handler(_request: object) -> str:
        return "tool-result"

    async def failing_handler(_request: object) -> str:
        raise RuntimeError("fixture tool failure")

    def tool_request(context: object, call_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            tool_call={
                "id": call_id,
                "name": "execute",
                "args": {"command": "safe"},
            },
            runtime=SimpleNamespace(context=context),
        )

    async def add_feedback(
        context: object,
        call_id: str,
        handler: object = successful_handler,
    ) -> None:
        await runtime.middleware.awrap_tool_call(
            tool_request(context, call_id),
            handler,  # type: ignore[arg-type]
        )

    async def project(context: object) -> str:
        captured = {"value": ""}
        model_request = ModelRequest(
            model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
            messages=[],
            tools=[],
            runtime=SimpleNamespace(context=context),
        )

        async def model_handler(next_request: ModelRequest) -> ModelResponse:
            captured["value"] = (
                str(next_request.system_message.content)
                if next_request.system_message is not None
                else ""
            )
            return ModelResponse(result=[AIMessage(content="ok")])

        await runtime.middleware.awrap_model_call(model_request, model_handler)
        return captured["value"]

    class _RunLease:
        async def release(self) -> None:
            return None

    class _EngineLease:
        engine = SimpleNamespace(graph=object())

        async def run(self) -> _RunLease:
            return _RunLease()

        async def release(self) -> None:
            return None

    class _Pool:
        def __init__(self) -> None:
            self.lease = _EngineLease()

        async def acquire(self, _profile: object) -> _EngineLease:
            return self.lease

        async def finalize_draining(self, _profile_key: str) -> None:
            return None

    async def release_child(context: object) -> None:
        pooled = await acquire_pooled_agent_runtime(
            pool=_Pool(),
            profile=SimpleNamespace(profile_key="fixture-profile"),
            run_context=context,
            graph_config=lambda namespace: {"configurable": {"thread_id": namespace}},
            on_release=lambda: runtime.clear_execution_context(context),  # type: ignore[arg-type]
        )
        await pooled.release()

    try:
        await add_feedback(root, "root-success")
        await add_feedback(child_one, "child-one-success")
        await add_feedback(child_two, "child-two-success")
        await release_child(child_one)

        assert "child-one" not in await project(child_one)
        assert "child-two" in await project(child_two)
        assert "root" in await project(root)

        with pytest.raises(RuntimeError, match="fixture tool failure"):
            await add_feedback(child_two, "child-two-failure", failing_handler)
        await release_child(child_two)
        assert "child-two" not in await project(child_two)

        await add_feedback(child_two, "child-two-cancel")
        child_two.cancellation_token.cancel()
        model_calls = 0

        async def cancelled_model(_request: ModelRequest) -> ModelResponse:
            nonlocal model_calls
            model_calls += 1
            return ModelResponse(result=[AIMessage(content="must-not-run")])

        with pytest.raises(PluginRuntimeError) as cancelled_error:
            await runtime.middleware.awrap_model_call(
                ModelRequest(
                    model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
                    messages=[],
                    tools=[],
                    runtime=SimpleNamespace(context=child_two),
                ),
                cancelled_model,
            )
        assert cancelled_error.value.code == "PLUGIN_HOOK_RUN_CANCELLED"
        assert model_calls == 0

        host = AgentHost(
            agent=object(),
            allow_echo=True,
            config_home=tmp_path / "host-home",
            workspace=workspace,
        )
        host._plugin_runtime_manager = runtime
        host_run = SimpleNamespace(
            run_context=root,
            persistence=None,
            agent_engine_run_lease=None,
            agent_engine_lease=None,
            agent_engine_profile_key=None,
        )
        host_runtime = await host._acquire_run_runtime(host_run)
        await add_feedback(root, "root-before-release")
        await add_feedback(child_one, "child-one-before-release")
        await host_runtime.release()
        assert "root" not in await project(root)
        assert "child-one" not in await project(child_one)
        await host.close()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_qwen_bare_node_hook_and_lsp_are_frozen_without_inheriting_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qwen bare node 从受控 PATH 冻结，子进程仍不继承 PATH。"""
    source = _copy_fixture(tmp_path)
    scripts = source / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "za38-git-commit-gate.mjs").write_text("// fixture", encoding="utf-8")
    (scripts / "za38-language-server.mjs").write_text("// fixture", encoding="utf-8")
    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path.cwd().joinpath('qwen-env.txt').write_text('PATH=' + str(os.environ.get('PATH')), encoding='utf-8')\n"
        "args = sys.argv[1:]\n"
        "if any('language-server' in item for item in args):\n"
        "  def read():\n"
        "    length = None\n"
        "    while True:\n"
        "      line = sys.stdin.buffer.readline()\n"
        "      if not line: raise EOFError\n"
        "      if line in (b'\\r\\n', b'\\n'): break\n"
        "      if line.lower().startswith(b'content-length:'): length = int(line.split(b':', 1)[1])\n"
        "    return json.loads(sys.stdin.buffer.read(length))\n"
        "  def write(value):\n"
        "    body = json.dumps(value, separators=(',', ':')).encode()\n"
        "    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body); sys.stdout.buffer.flush()\n"
        "  while True:\n"
        "    message = read(); method = message.get('method')\n"
        "    if 'id' not in message:\n"
        "      if method == 'exit': break\n"
        "      continue\n"
        "    result = {'capabilities': {'definitionProvider': True}} if method == 'initialize' else ([] if method != 'shutdown' else None)\n"
        "    write({'jsonrpc': '2.0', 'id': message['id'], 'result': result})\n"
        "else:\n"
        "  payload = json.load(sys.stdin)\n"
        "  if payload.get('hook_event_name') == 'PreToolUse':\n"
        "    print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'frozen node'}}))\n"
        "  else: print('{}')\n",
        encoding="utf-8",
    )
    node.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    manifest = _manifest(source)
    manifest["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'node "${extensionPath}/scripts/za38-git-commit-gate.mjs"',
                    }
                ],
            }
        ]
    }
    manifest["lspServers"] = {
        "node-lsp": {
            "transport": "stdio",
            "command": "node",
            "args": ["${extensionPath}${/}scripts${/}za38-language-server.mjs"],
            "workspaceFolder": "${workspacePath}",
            "extensionToLanguage": {".node": "javascript"},
        }
    }
    _write_manifest(source, manifest)
    manager, workspace = _enabled_manager(tmp_path, source)
    target = workspace / "sample.node"
    target.write_text("const value = 1", encoding="utf-8")
    catalog = manager.runtime_catalog(manager.catalog(), workspace=workspace)
    assert catalog.hooks[0].command == str(node)
    assert catalog.hooks[0].args is not None
    assert catalog.hooks[0].args[0].endswith(
        "scripts/za38-git-commit-gate.mjs"
    )
    assert catalog.lsp_servers[0].command == str(node)
    runtime = PluginRuntimeManager(catalog)
    try:
        context = _run_context(workspace, "node-thread", "node-run")
        blocked = await runtime.middleware.awrap_tool_call(
            SimpleNamespace(
                tool_call={"id": "node-hook", "name": "execute", "args": {"command": "safe"}},
                runtime=SimpleNamespace(context=context),
            ),
            lambda _request: asyncio.sleep(0, result="unreachable"),
        )
        assert getattr(blocked, "status", None) == "error"
        result = await runtime.lsp.query("definition", "sample.node", 1, 1, str(workspace))
        assert result["server"] == "node-lsp"
        assert (workspace / "qwen-env.txt").read_text(encoding="utf-8") == "PATH=None"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("initialize-header", "PLUGIN_LSP_HEADER_INVALID"),
        ("initialize-eof", "PLUGIN_LSP_PIPE_CLOSED"),
        ("query-incomplete", "PLUGIN_LSP_MESSAGE_INCOMPLETE"),
        ("query-json", "PLUGIN_LSP_JSON_INVALID"),
    ),
)
async def test_qwen_lsp_malformed_stdio_is_normalized_and_cleaned(
    tmp_path: Path,
    mode: str,
    expected: str,
) -> None:
    """initialize/query 阶段的坏 header、EOF 和坏 body 都归一化并回收 client。"""
    source = _copy_fixture(tmp_path)
    script = source / "scripts" / f"malformed-{mode}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        "import json, sys\n"
        "def read():\n"
        "  length = None\n"
        "  while True:\n"
        "    line = sys.stdin.buffer.readline()\n"
        "    if not line: raise EOFError\n"
        "    if line in (b'\\r\\n', b'\\n'): break\n"
        "    if line.lower().startswith(b'content-length:'): length = int(line.split(b':', 1)[1])\n"
        "  return json.loads(sys.stdin.buffer.read(length))\n"
        "if " + repr(mode) + ".startswith('initialize'):\n"
        "  if " + repr(mode) + " == 'initialize-header':\n"
        "    sys.stdout.buffer.write(b'Content-Length: 1\\xff\\n\\n'); sys.stdout.buffer.flush()\n"
        "  else:\n"
        "    sys.exit(0)\n"
        "else:\n"
        "  while True:\n"
        "    message = read()\n"
        "    if message.get('method') == 'initialize':\n"
        "      body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {'capabilities': {}}}).encode(); sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body); sys.stdout.buffer.flush()\n"
        "    elif message.get('method') == 'textDocument/definition':\n"
        "      body = b'{}' if " + repr(mode) + " == 'query-incomplete' else b'xxxxx'\n"
        "      sys.stdout.buffer.write(f'Content-Length: 5\\r\\n\\r\\n'.encode() + body); sys.stdout.buffer.flush(); break\n",
        encoding="utf-8",
    )
    manifest = _manifest(source)
    manifest["lspServers"] = {
        "broken": {
            "transport": "stdio",
            "command": "python3",
            "args": [f"${{extensionPath}}${{/}}scripts${{/}}malformed-{mode}.py"],
            "workspaceFolder": "${workspacePath}",
            "extensionToLanguage": {".broken": "broken"},
        }
    }
    _write_manifest(source, manifest)
    manager, workspace = _enabled_manager(tmp_path, source)
    target = workspace / "sample.broken"
    target.write_text("broken", encoding="utf-8")
    runtime = PluginRuntimeManager(
        manager.runtime_catalog(manager.catalog(), workspace=workspace)
    )
    try:
        result = await runtime.lsp.query("definition", "sample.broken", 1, 1, str(workspace))
        assert result["error"] == expected
        assert runtime.lsp._clients == {}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_qwen_stdio_lsp_is_canonical_and_invalid_entries_are_isolated(
    tmp_path: Path,
) -> None:
    """Qwen inline stdio LSP 可进入既有 manager，坏项与冲突项逐项隔离。"""
    source = _copy_fixture(tmp_path)
    _write_lsp_script(source)
    manifest = _qwen_lsp_manifest(source)
    manifest["lspServers"]["bad-transport"] = {
        "transport": "socket",
        "command": "node",
        "extensionToLanguage": {".bad": "bad"},
    }
    manifest["lspServers"]["bad-field"] = {
        "transport": "stdio",
        "command": "python3",
        "unknown": True,
        "extensionToLanguage": {".bad2": "bad"},
    }
    manifest["lspServers"]["zzz-conflict"] = {
        "transport": "stdio",
        "command": "python3",
        "args": ["${extensionPath}${/}scripts${/}phase3-lsp.py"],
        "extensionToLanguage": {".toy": "other"},
    }
    manifest["lspServers"]["bad-placeholder"] = {
        "transport": "stdio",
        "command": "${unknownCommand}",
        "extensionToLanguage": {".unknown": "unknown"},
    }
    manifest["lspServers"]["bad-path"] = {
        "transport": "stdio",
        "command": "python3",
        "args": ["${extensionPath}${/}../outside.py"],
        "extensionToLanguage": {".escape": "escape"},
    }
    manifest["lspServers"]["bad-env"] = {
        "transport": "stdio",
        "command": "python3",
        "env": {"PATH": "should-not-run"},
        "extensionToLanguage": {".envbad": "envbad"},
    }
    _write_manifest(source, manifest)

    manager, workspace = _enabled_manager(tmp_path, source)
    target = workspace / "sample.toy"
    target.write_text("symbol", encoding="utf-8")
    catalog = manager.runtime_catalog(manager.catalog(), workspace=workspace)
    assert len(catalog.lsp_servers) == 1
    assert catalog.lsp_servers[0].name == "toy"
    assert any("PLUGIN_LSP_TRANSPORT_UNSUPPORTED" in item for item in catalog.diagnostics)
    assert any("PLUGIN_LSP_FIELD_INVALID" in item for item in catalog.diagnostics)
    assert any("PLUGIN_LSP_EXTENSION_CONFLICT" in item for item in catalog.diagnostics)
    assert any("PLUGIN_LSP_PLACEHOLDER_INVALID" in item for item in catalog.diagnostics)
    assert any("PLUGIN_LSP_PATH_INVALID" in item for item in catalog.diagnostics)
    assert any("PLUGIN_LSP_ENV_INVALID" in item for item in catalog.diagnostics)

    runtime = PluginRuntimeManager(catalog)
    try:
        from harness_agent.tools.tools_intelligence import lsp

        result = await lsp(
            "definition",
            "sample.toy",
            1,
            1,
            str(workspace),
            manager=runtime.lsp,
        )
        assert result["server"] == "toy"
        assert isinstance(result["results"], list)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_qwen_lsp_timeout_cancel_close_and_generation_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LSP 超时/取消和 generation 替换都清理旧 stdio client。"""
    import harness_agent.plugins.runtime as runtime_module

    source = _copy_fixture(tmp_path)
    _write_lsp_script(source)
    _write_manifest(source, _qwen_lsp_manifest(source))
    manager, workspace = _enabled_manager(tmp_path, source)
    target = workspace / "sample.toy"
    target.write_text("symbol", encoding="utf-8")
    catalog = manager.runtime_catalog(manager.catalog(), workspace=workspace)
    runtime = PluginRuntimeManager(catalog)
    monkeypatch.setattr(runtime_module, "_LSP_REQUEST_TIMEOUT_SECONDS", 0.05)

    try:
        timeout = await runtime.lsp.query("hover", "sample.toy", 1, 1, str(workspace))
        assert timeout["error"] == "PLUGIN_LSP_REQUEST_TIMEOUT"
        assert runtime.lsp._clients == {}

        cancelled = asyncio.create_task(
            runtime.lsp.query("hover", "sample.toy", 1, 1, str(workspace))
        )
        for _ in range(100):
            if (workspace / "lsp.pid").exists():
                break
            await asyncio.sleep(0.01)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert runtime.lsp._clients == {}

        monkeypatch.setattr(runtime_module, "_LSP_REQUEST_TIMEOUT_SECONDS", 5.0)
        definition = await runtime.lsp.query("definition", "sample.toy", 1, 1, str(workspace))
        assert definition.get("server") == "toy", definition
        old_client = runtime.lsp._clients["toy"]
        old_process = old_client._process
        assert old_process is not None
        await runtime.lsp.replace(tuple(catalog.lsp_servers))
        assert old_process.returncode is not None
        assert runtime.lsp._clients == {}
    finally:
        await runtime.aclose()
