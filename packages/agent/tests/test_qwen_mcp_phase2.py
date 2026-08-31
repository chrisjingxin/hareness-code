"""HC-158 Phase 2 Qwen stdio MCP 的失败优先回归。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from harness_agent.extensions.mcp import McpConnectionManager, build_mcp_snapshot
from harness_agent.plugins.manager import PluginManager


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"


def _copy_fixture(tmp_path: Path) -> Path:
    """复制离线 Qwen fixture；测试不读取真实 ZA38 目录。"""
    target = tmp_path / "qwen-mcp"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _manifest(root: Path) -> dict[str, object]:
    """读取 fixture manifest 的可变副本。"""
    return json.loads((root / "devagent-extension.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    """写回脱敏 fixture manifest。"""
    (root / "devagent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _mcp_component(summary: dict[str, object]) -> dict[str, object]:
    """从 Plugin 摘要取 MCP component。"""
    components = summary["components"]
    assert isinstance(components, list)
    component = next(item for item in components if item["kind"] == "mcp")
    assert isinstance(component, dict)
    return component


def test_qwen_stdio_mcp_is_adapted_and_binds_to_installed_store(
    tmp_path: Path,
) -> None:
    """Qwen ZA38 stdio 条目必须进入 canonical McpServerConfig。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)

    component = _mcp_component(installed)
    assert component["status"] == "adapted"
    assert component["effective"] is True

    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    catalog = manager.catalog()
    result = manager.mcp_servers(catalog, workspace=tmp_path / "workspace")

    assert result.diagnostics == ()
    assert len(result.servers) == 1
    server = result.servers[0]
    package_root = manager.store.package_path(catalog.plugins[0])
    assert server.transport == "stdio"
    assert Path(server.command).is_absolute()
    assert Path(server.command).name == "node"
    assert server.args == (str(package_root / "mcp" / "context-server.mjs"),)
    assert server.plugin_root == str(package_root.resolve())
    assert server.inherit_environment is False
    assert server.inherit_path is False


def test_qwen_mcp_accepts_only_the_four_declared_path_tokens(
    tmp_path: Path,
) -> None:
    """四个受支持 token 在安装 store 与当前 workspace 闭包内解析。"""
    source = _copy_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "request.txt").write_text("fixture", encoding="utf-8")
    manifest = _manifest(source)
    servers = manifest["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["za38.03_code_index"]
    assert isinstance(server, dict)
    server["args"] = [
        "${extensionPath}${/}mcp${pathSeparator}context-server.mjs",
        "${workspacePath}${/}request.txt",
        "${/}",
        "${pathSeparator}",
    ]
    server["cwd"] = "${extensionPath}${/}mcp"
    server["env"] = {
        "EXTENSION": "${extensionPath}",
        "WORKSPACE": "${workspacePath}",
        "SEPARATOR": "${pathSeparator}",
    }
    _write_manifest(source, manifest)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    catalog = manager.catalog()
    result = manager.mcp_servers(catalog, workspace=workspace)

    assert result.diagnostics == ()
    assert len(result.servers) == 1
    package_root = manager.store.package_path(catalog.plugins[0])
    assert result.servers[0].cwd == str((package_root / "mcp").resolve())
    assert result.servers[0].args == (
        str(package_root / "mcp" / "context-server.mjs"),
        str(workspace / "request.txt"),
        "/",
        "/",
    )
    assert result.servers[0].env["EXTENSION"] == str(package_root.resolve())
    assert result.servers[0].env["WORKSPACE"] == str(workspace.resolve())


def test_qwen_mcp_bad_server_is_isolated_from_valid_server(
    tmp_path: Path,
) -> None:
    """坏 Qwen MCP server 只能生成稳定诊断，不能污染同包有效 server。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    servers = manifest["mcpServers"]
    assert isinstance(servers, dict)
    servers["broken"] = {
        "command": "node",
        "args": ["${unknownPath}/not-allowed.mjs"],
    }
    _write_manifest(source, manifest)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    component = _mcp_component(installed)
    assert component["status"] == "adapted"
    assert component["effective"] is True
    assert any(
        "PLUGIN_MCP_PLACEHOLDER_INVALID" in diagnostic
        for diagnostic in component["diagnostics"]
    )

    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.mcp_servers(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )
    assert len(result.servers) == 1
    assert any("broken" in diagnostic for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    (
        ("command", "${extensionPath}/../outside.mjs", "PLUGIN_MCP_PATH_INVALID"),
        ("args", ["${extensionPath}/missing.mjs"], "PLUGIN_MCP_TARGET_MISSING"),
        ("cwd", "${extensionPath}/../outside", "PLUGIN_MCP_PATH_INVALID"),
        ("env", {"BROKEN": 1}, "PLUGIN_MCP_FIELD_INVALID"),
        ("timeout", 121, "PLUGIN_MCP_TIMEOUT_INVALID"),
    ),
)
def test_qwen_mcp_invalid_fields_are_reported_without_becoming_effective(
    tmp_path: Path,
    field: str,
    value: object,
    error_code: str,
) -> None:
    """Qwen stdio command/args/cwd/env/timeout 超出 Phase 2 子集时 fail closed。"""
    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    servers = manifest["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["za38.03_code_index"]
    assert isinstance(server, dict)
    server[field] = value
    _write_manifest(source, manifest)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    component = _mcp_component(installed)
    assert component["effective"] is False
    assert component["status"] == "invalid"
    assert any(error_code in diagnostic for diagnostic in component["diagnostics"])


@pytest.mark.asyncio
async def test_qwen_mcp_effective_items_leave_static_preview_and_fake_client_is_used(
    tmp_path: Path,
) -> None:
    """effective Qwen MCP 只经 canonical manager 连接，静态 preview 不重复显示。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    assert manager.static_preview()["mcp"] == []
    result = manager.mcp_servers(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )
    assert len(result.servers) == 1

    fake_tool = AsyncMock()
    fake_tool.name = f"{result.servers[0].name}_search_za38"
    fake_tool.ainvoke.return_value = {"matches": ["login"]}
    fake_client = AsyncMock()
    fake_client.get_tools.return_value = [fake_tool]
    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ) as client_class:
        canonical = McpConnectionManager(result.servers)
        await canonical.connect_all()
        assert canonical.get_server_statuses()[0]["status"] == "connected"
        assert await canonical.get_tools()[0].ainvoke({"query": "login"}) == {
            "matches": ["login"]
        }
        await canonical.close_all()

    client_class.assert_called_once()
    assert fake_client.aclose.await_count == 1


@pytest.mark.asyncio
async def test_qwen_mcp_real_fake_stdio_round_trip_cancel_and_close(
    tmp_path: Path,
) -> None:
    """Qwen adapter 到 canonical manager 走真实 fake stdio initialize/list/call/cancel。"""
    source = _copy_fixture(tmp_path)
    server_script = source / "mcp" / "fake-stdio-server.py"
    server_script.parent.mkdir(exist_ok=True)
    server_script.write_text(
        """
import json
import os
import sys
import time

events = os.environ["FAKE_MCP_EVENTS"]

def record(value):
    with open(events, "a", encoding="utf-8") as handle:
        handle.write(value + "\\n")

record("pid:" + str(os.getpid()))

def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    return json.loads(line)

def write_message(value):
    body = json.dumps(value, separators=(",", ":")).encode() + b"\\n"
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()

while True:
    try:
        message = read_message()
    except EOFError:
        record("eof")
        break
    method = message.get("method")
    record(method or "notification")
    if "id" not in message:
        if method == "exit":
            record("close")
            break
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-za38", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "search_za38",
                    "description": "offline fake search",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        query = message.get("params", {}).get("arguments", {}).get("query")
        if query == "cancel":
            record("call-waiting")
            time.sleep(30)
        result = {"content": [{"type": "text", "text": "found:" + str(query)}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        result = {}
    write_message({"jsonrpc": "2.0", "id": message["id"], "result": result})
""".strip(),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = workspace / "mcp-events.log"
    events.write_text("", encoding="utf-8")
    manifest = _manifest(source)
    servers = manifest["mcpServers"]
    assert isinstance(servers, dict)
    servers["za38.03_code_index"] = {
        "type": "stdio",
        "command": "python3",
        "args": ["${extensionPath}${/}mcp${/}fake-stdio-server.py"],
        "cwd": "${workspacePath}",
        "env": {"FAKE_MCP_EVENTS": "${workspacePath}${/}mcp-events.log"},
        "timeout": 10,
    }
    _write_manifest(source, manifest)

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.mcp_servers(manager.catalog(), workspace=workspace)
    assert result.diagnostics == ()
    assert len(result.servers) == 1
    config = result.servers[0]
    # Qwen command 已在 adapter 阶段冻结为绝对 executable，避免继承宿主 PATH。
    assert Path(config.command).name.startswith("python3")
    assert config.cwd == str(workspace.resolve())
    assert config.env["FAKE_MCP_EVENTS"] == str(events.resolve())

    canonical = McpConnectionManager(build_mcp_snapshot(result.servers, "real-fake"))
    await canonical.connect_all()
    tools = canonical.get_tools()
    assert len(tools) == 1
    assert "initialize" in events.read_text(encoding="utf-8")
    assert "tools/list" in events.read_text(encoding="utf-8")
    call_result = await tools[0].ainvoke({"query": "login"})
    assert "found:login" in str(call_result)
    assert "tools/call" in events.read_text(encoding="utf-8")

    pending = asyncio.create_task(tools[0].ainvoke({"query": "cancel"}))
    for _ in range(100):
        if "call-waiting" in events.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.01)
    assert "call-waiting" in events.read_text(encoding="utf-8")
    lines = events.read_text(encoding="utf-8").splitlines()
    pids = [int(line.split(":", 1)[1]) for line in lines if line.startswith("pid:")]
    assert pids
    cancelled_pid = pids[-1]
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    for _ in range(100):
        try:
            os.kill(cancelled_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(cancelled_pid, 0)

    await canonical.close_all()
    assert "eof" in events.read_text(encoding="utf-8")
    assert canonical.get_tools() == []
    assert canonical._resources == {}


@pytest.mark.asyncio
async def test_qwen_mcp_generation_replacement_drains_old_fake_client(
    tmp_path: Path,
) -> None:
    """Qwen server 复用 canonical generation，旧 Run lease 释放后才关闭旧 client。"""
    source = _copy_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    result = manager.mcp_servers(
        manager.catalog(),
        workspace=tmp_path / "workspace",
    )
    assert len(result.servers) == 1
    old_server = result.servers[0]
    new_server = replace(old_server, args=(*old_server.args, "--replacement"))
    old_snapshot = build_mcp_snapshot([old_server], revision="qwen-old")
    new_snapshot = build_mcp_snapshot([new_server], revision="qwen-new")

    class FakeTool:
        """离线 tool，区分旧/新 generation。"""

        def __init__(self, name: str) -> None:
            self.name = name

    class FakeClient:
        """fake initialize/list/close client；不创建 stdio 子进程。"""

        instances: list["FakeClient"] = []

        def __init__(self, connections: dict[str, object], **_kwargs: object) -> None:
            self.connections = connections
            self.tool = FakeTool(f"{next(iter(connections))}_search_za38")
            self.closed = False
            self.__class__.instances.append(self)

        async def get_tools(self, *, server_name: str | None = None) -> list[FakeTool]:
            assert server_name in self.connections
            return [self.tool]

        async def aclose(self) -> None:
            self.closed = True

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        FakeClient,
    ):
        canonical = McpConnectionManager(old_snapshot)
        await canonical.connect_all()
        old_lease = await canonical.acquire(old_snapshot)
        await canonical.apply_snapshot(new_snapshot)
        new_lease = await canonical.acquire(new_snapshot)

        assert canonical.snapshot is new_snapshot
        assert canonical.get_tool_names() == [f"{new_server.name}_search_za38"]
        assert FakeClient.instances[0].closed is False

        await new_lease.release()
        await old_lease.release()
        await canonical.reap()
        assert FakeClient.instances[0].closed is True
        await canonical.close_all()
        assert FakeClient.instances[1].closed is True


@pytest.mark.asyncio
async def test_qwen_mcp_host_status_and_close_use_fake_stdio_generation(
    tmp_path: Path,
) -> None:
    """Host 只把 Qwen canonical server 交给 fake stdio client，并在 close 回收。"""
    from harness_agent.host.agent_host import AgentHost

    class FakeTool:
        """离线 MCP tool；不创建进程，只记录 canonical tool 调用。"""

        def __init__(self, name: str) -> None:
            self.name = name
            self.calls: list[object] = []

        async def ainvoke(self, payload: object) -> dict[str, object]:
            self.calls.append(payload)
            return {"matches": ["login"]}

    class FakeClient:
        """替代 MultiServerMCPClient 的 fake initialize/list/close seam。"""

        instances: list["FakeClient"] = []

        def __init__(self, connections: dict[str, object], **_kwargs: object) -> None:
            self.connections = connections
            self.tool = next(
                FakeTool(f"{name}_search_za38") for name in connections
            )
            self.closed = False
            self.__class__.instances.append(self)

        async def get_tools(self, *, server_name: str | None = None) -> list[FakeTool]:
            assert server_name in self.connections
            return [self.tool]

        async def aclose(self) -> None:
            self.closed = True

    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    installed = server._plugin_manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)
    server._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        FakeClient,
    ):
        await server._handle_initialize(
            {
                "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
                "client": {"name": "fake", "version": "0.1.0", "kind": "test"},
                "capabilities": {"requests": [], "handles": []},
            },
            "initialize",
        )
        status = await server._handle_mcp_status({}, "mcp-status")
        assert len(status["servers"]) == 1
        assert status["servers"][0]["status"] == "connected"
        assert status["servers"][0]["tool_names"]
        assert status["static_preview"] == []

        fake_client = FakeClient.instances[-1]
        connection = next(iter(fake_client.connections.values()))
        assert isinstance(connection, dict)
        assert connection["transport"] == "stdio"
        assert "NODE_OPTIONS" not in (connection.get("env") or {})
        assert "HOME" not in (connection.get("env") or {})
        assert "PATH" not in (connection.get("env") or {})
        result = await server._mcp_manager.get_tools()[0].ainvoke({"query": "login"})
        assert result == {"matches": ["login"]}

    await server.close()
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_qwen_mcp_status_exposes_isolated_server_diagnostics(
    tmp_path: Path,
) -> None:
    """单个坏 server 只隔离自身，并通过 mcp.status 返回稳定诊断。"""
    from harness_agent.host.agent_host import AgentHost

    source = _copy_fixture(tmp_path)
    manifest = _manifest(source)
    servers = manifest["mcpServers"]
    assert isinstance(servers, dict)
    servers["broken"] = {"command": "node", "args": ["${unknownPath}/bad.mjs"]}
    _write_manifest(source, manifest)

    class FakeClient:
        """只提供 canonical list/close seam，不启动真实 stdio。"""

        def __init__(self, connections: dict[str, object], **_kwargs: object) -> None:
            self.connections = connections

        async def get_tools(self, *, server_name: str | None = None) -> list[object]:
            assert server_name in self.connections
            return []

        async def aclose(self) -> None:
            return None

    host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    installed = host._plugin_manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    host._plugin_manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        FakeClient,
    ):
        await host._handle_initialize(
            {
                "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
                "client": {"name": "fake", "version": "0.1.0", "kind": "test"},
                "capabilities": {"requests": [], "handles": []},
            },
            "initialize",
        )
        status = await host._handle_mcp_status({}, "mcp-status")

    assert len(status["servers"]) == 1
    assert any("broken" in item for item in status["diagnostics"])
    assert any("PLUGIN_MCP_PLACEHOLDER_INVALID" in item for item in status["diagnostics"])
    await host.close()


@pytest.mark.asyncio
async def test_qwen_mcp_disabled_plugin_never_constructs_client(
    tmp_path: Path,
) -> None:
    """disabled/untrusted Qwen MCP 只保留 preview，不能触发 canonical client。"""
    from harness_agent.host.agent_host import AgentHost

    host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
    )
    installed = host._plugin_manager.install(_copy_fixture(tmp_path))["plugin"]
    assert isinstance(installed, dict)

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
    ) as client_class:
        await host._handle_initialize(
            {
                "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
                "client": {"name": "fake", "version": "0.1.0", "kind": "test"},
                "capabilities": {"requests": [], "handles": []},
            },
            "initialize",
        )
        status = await host._handle_mcp_status({}, "mcp-status")

    assert status["servers"] == []
    assert status["static_preview"]
    client_class.assert_not_called()
    await host.close()
