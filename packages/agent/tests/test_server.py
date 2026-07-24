"""v2 JSON-RPC 握手、并发运行、双向交互和真实 stdio 回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "protocol": {"major": 2, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0"},
        "capabilities": ["run.cancel", "run.multithread", "interactive.approval", "interactive.question"],
    }
    params.update(overrides)
    return params


async def _capture_server(server: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(_request("initialize", _initialize_params(), "init-1"))
    return frames


async def _wait_for(frames: list[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    for _ in range(200):
        for frame in frames:
            if predicate(frame):
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out; received: {frames}")


def _event_types(frames: list[dict[str, Any]]) -> list[str]:
    return [frame["params"]["type"] for frame in frames if frame.get("method") == "event"]


async def test_initialize_negotiates_v2_and_capabilities(tmp_path: Path):
    """握手返回选定 minor、能力交集、限制和脱敏配置摘要。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True, config_home=tmp_path / "home")
    frames = await _capture_server(server)
    result = frames[0]["result"]
    assert result["protocol"] == {"major": 2, "minor": 0}
    assert "run.multithread" in result["enabled_capabilities"]
    assert result["limits"]["max_frame_bytes"] == 8 * 1024 * 1024
    assert result["config_summary"]["security"]["mode"] == "local"


async def test_initialize_rejects_incompatible_major_and_pre_initialize_calls():
    """不兼容 Major 和握手前业务调用必须被结构化拒绝。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request("run.start", {"message": "x"}, "run-early"))
    await server.dispatch(_request("initialize", _initialize_params(protocol={"major": 9, "min_minor": 0, "max_minor": 0}), "init-bad"))
    assert [frame["error"]["code"] for frame in frames] == [-32000, -32003]


async def test_config_show_exposes_redacted_runtime_pool_diagnostics(tmp_path: Path):
    """已有 config.show 提供 Pool 本地诊断，且不能泄露完整 Profile Key。"""
    from harness_agent.agent_runtime import AgentRuntime, RuntimePool
    from harness_agent.runtime_profile import ModelRoleBinding, RuntimeProfile, component_fingerprint
    from harness_agent.server import JsonRpcServer

    def fingerprint(component: str) -> str:
        return component_fingerprint({"server-diagnostics": component})

    profile = RuntimeProfile(
        project_fingerprint=fingerprint("project"),
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding(role="primary", model_config_fingerprint=fingerprint("model")),),
        tool_catalog_fingerprint=fingerprint("tools"),
        skill_catalog_fingerprint=fingerprint("skills"),
        mcp_config_fingerprint=fingerprint("mcp"),
        sandbox_config_fingerprint=fingerprint("sandbox"),
        policy_fingerprint=fingerprint("policy"),
        middleware_fingerprint=fingerprint("middleware"),
        prompt_template_fingerprint=fingerprint("prompt"),
    )
    server = JsonRpcServer(allow_echo=True, config_home=tmp_path / "home")
    frames = await _capture_server(server)
    pool = RuntimePool(lambda requested: AgentRuntime(profile=requested, graph=object()))
    server._runtime_pool = pool
    lease = await pool.acquire(profile)
    await lease.release()

    await server.dispatch(_request("config.show", {}, "config-runtime"))

    result = frames[-1]["result"]
    diagnostics = result["runtime_pool_diagnostics"]
    assert diagnostics["available"] is True
    assert diagnostics["pool_size"] == 1
    assert diagnostics["runtimes"][0]["profile_id"] == profile.profile_key[:12]
    assert profile.profile_key not in str(diagnostics)
    assert diagnostics["memory"]["status"] == "not_collected"

    await server._close_runtime_pool()
    await server.dispatch(_request("config.show", {}, "config-runtime-closed"))
    assert frames[-1]["result"]["runtime_pool_diagnostics"]["state"] == "not_initialized"


async def test_project_configuration_failure_prevents_agent_factory_invocation(tmp_path: Path):
    """未可信项目配置必须在创建模型或 Agent 之前以启动错误终止。"""
    from harness_agent.server import JsonRpcServer

    workspace = tmp_path / "workspace"
    project_config = workspace / ".harness" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text("[config]\nversion = 1\n", encoding="utf-8")
    invoked = False

    def factory(*_: Any) -> object:
        nonlocal invoked
        invoked = True
        return object()

    server = JsonRpcServer(agent_factory=factory, config_home=tmp_path / "home")
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(_request("initialize", _initialize_params(cwd=str(workspace)), "init-project"))
    result = frames[0]["result"]
    assert result["config_summary"] is None
    assert result["startup_error"]["code"] == "CONFIGURATION_ERROR"

    await server.dispatch(
        _request("run.start", {"message": "should not start", "thread_id": "project", "run_id": "blocked"}, "run-project")
    )
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.failed")
    assert invoked is False


async def test_echo_run_response_precedes_ordered_terminal_events():
    """run.start 响应必须早于 sequence 连续的统一事件。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "run-1"))
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    run_frames = frames[1:]
    assert run_frames[0]["result"]["accepted"] is True
    assert _event_types(run_frames) == ["run.started", "content.delta", "run.completed"]
    assert [frame["params"]["sequence"] for frame in run_frames if frame.get("method") == "event"] == [1, 2, 3]


async def test_context_compact_rewrites_idle_thread_and_returns_context_summary():
    """手动压缩只允许空闲 thread，成功后写回 checkpoint 并同步摘要状态。"""
    from langchain_core.messages import HumanMessage

    from harness_agent.context_window import ContextUpdate
    from harness_agent.server import JsonRpcServer

    class Store:
        def __init__(self) -> None:
            self.refreshed: list[str] = []

        async def load_context_messages(self, _thread_id: str) -> list[HumanMessage]:
            return [HumanMessage(content="旧上下文")]

        async def refresh_thread(self, thread_id: str) -> None:
            self.refreshed.append(thread_id)

        @staticmethod
        def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
            return {"configurable": {"thread_id": thread_id}}

    class Agent:
        def __init__(self) -> None:
            self.updates: list[tuple[dict[str, object], dict[str, object]]] = []

        async def aupdate_state(self, config: dict[str, object], update: dict[str, object], *, as_node: str) -> None:
            assert as_node == "model"
            self.updates.append((config, update))

    class Middleware:
        async def compact_now(self, thread_id: str, messages: list[HumanMessage]):
            update = ContextUpdate(
                thread_id=thread_id,
                action="manual_summary",
                estimated_tokens=20,
                input_cap_tokens=100,
                context_window_tokens=128,
                dynamic_tokens=10,
                artifact_ids=("history-123456789",),
            )
            return [HumanMessage(content="<harness_context_summary>摘要</harness_context_summary>")], update, True

        @staticmethod
        def consume_updates(_thread_id: str) -> tuple[()]:
            return ()

    store = Store()
    agent = Agent()
    server = JsonRpcServer(agent=agent)
    server._initialized = True
    server._enabled_capabilities = {"context.manage"}
    server._thread_store = store  # type: ignore[assignment]
    server._context_compactor = Middleware()
    server._thread_persistence_enabled = lambda: True  # type: ignore[method-assign]
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("context.compact", {"thread_id": "thread"}, "compact-1"))

    assert frames[0]["result"] == {
        "compacted": True,
        "context": {
            "action": "manual_summary",
            "estimated_tokens": 20,
            "input_cap_tokens": 100,
            "context_window_tokens": 128,
            "dynamic_tokens": 10,
            "cache_status": "unknown",
            "cached_tokens": None,
            "miss_reason": None,
            "artifact_ids": ["history-123456789"],
        },
    }
    assert store.refreshed == ["thread"]
    assert agent.updates[0][0] == {"configurable": {"thread_id": "thread"}}
    assert len(agent.updates[0][1]["messages"]) == 2


async def test_context_compact_rejects_active_run():
    """运行中 checkpoint 会变动，手动压缩必须等待当前 run 结束。"""
    from harness_agent.server import ActiveRun, JsonRpcServer

    server = JsonRpcServer()
    server._initialized = True
    server._enabled_capabilities = {"context.manage"}
    server._runs["thread"] = ActiveRun(thread_id="thread", run_id="run", message="运行中")
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("context.compact", {"thread_id": "thread"}, "compact-active"))

    assert frames[0]["error"]["message"] == "CONTEXT_COMPACTION_RUN_ACTIVE"


async def test_models_list_and_run_start_freeze_selected_profile(tmp_path: Path, monkeypatch) -> None:
    """models.list 只返回脱敏目录，run.start 首次选择 executor 后 Thread 不能热切换。"""
    from harness_agent.server import JsonRpcServer

    home = tmp_path / "home"
    config = home / ".harness" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
provider = "openai-compatible"
provider_label = "Fast Gateway"
model = "fast-model"
base_url = "https://fast.example/v1"
api_key_env = "FAST_KEY"

[models.profiles.pro]
provider = "openai-compatible"
provider_label = "Pro Gateway"
model = "pro-model"
base_url = "https://pro.example/v1"
api_key_env = "PRO_KEY"
capabilities = ["tool-calling", "streaming", "vision"]

[models.roles]
planner = "pro"
executor = "fast"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAST_KEY", "fast-secret")
    monkeypatch.setenv("PRO_KEY", "pro-secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = JsonRpcServer(config_home=home)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request(
        "initialize",
        _initialize_params(
            protocol={"major": 2, "min_minor": 0, "max_minor": 5},
            capabilities=["models.read"],
            cwd=str(workspace),
        ),
        "init-models",
    ))

    await server.dispatch(_request("models.list", {}, "models"))
    catalog = frames[-1]["result"]
    assert [profile["id"] for profile in catalog["profiles"]] == ["fast", "pro"]
    assert catalog["profiles"][0]["is_default"] is True
    assert catalog["profiles"][1]["is_default"] is False
    assert "https://fast.example" not in str(catalog)
    assert "fast-secret" not in str(catalog)

    async def finish_without_build(run: Any) -> None:
        run.status = "completed"
        server._runs.pop(run.thread_id, None)

    server._execute_run = finish_without_build  # type: ignore[method-assign]
    await server.dispatch(_request(
        "run.start",
        {"message": "使用 pro", "thread_id": "thread-model", "run_id": "first", "model_profile": "pro"},
        "start-model",
    ))
    assert frames[-1]["result"]["accepted"] is True
    await asyncio.sleep(0)
    assert server._thread_store is not None
    bindings = await server._thread_store.get_model_bindings("thread-model")
    assert bindings is not None
    assert bindings["roles"]["executor"]["id"] == "pro"  # type: ignore[index]
    assert server._config is not None
    runtime_profile = await server._resolve_runtime_profile("thread-model", server._config)
    assert server._runtime_build_specs[runtime_profile.profile_key].model_settings.name == "pro-model"

    await server.dispatch(_request(
        "run.start",
        {"message": "尝试切换", "thread_id": "thread-model", "run_id": "second", "model_profile": "fast"},
        "start-model-again",
    ))
    assert frames[-1]["error"]["message"] == "THREAD_MODEL_PROFILE_IMMUTABLE"

    await server.dispatch(_request("models.list", {"thread_id": "thread-model"}, "models-bound"))
    binding = frames[-1]["result"]["thread_binding"]
    assert binding["state"] == "bound"
    assert binding["roles"]["executor"]["model"] == "pro-model"
    await server._close_thread_store()


async def test_default_sidecar_shares_runtime_by_profile_and_drains_invalidated_config(
    tmp_path: Path,
):
    """默认 Sidecar 以 Profile 而非 thread 缓存图；配置切换只排空旧图。"""
    from harness_agent.agent_runtime import AgentRuntime
    from harness_agent.config import (
        ExecutionSettings,
        ModelSettings,
        RuntimePoolSettings,
        Za38Config,
    )
    from harness_agent.runtime_profile import component_fingerprint
    from harness_agent.server import JsonRpcServer

    class Store:
        project_fingerprint = component_fingerprint({"project": "server-runtime"})

        def __init__(self) -> None:
            self.profiles: dict[str, object] = {}

        async def get_runtime_profile(self, thread_id: str) -> object | None:
            return self.profiles.get(thread_id)

        async def save_runtime_profile(self, thread_id: str, profile: object) -> None:
            self.profiles[thread_id] = profile

    def config(model_name: str, *, pin_default_profile: bool = False) -> Za38Config:
        return Za38Config(
            model=ModelSettings(name=model_name, base_url="https://gateway.example/v1"),
            model_profile="default",
            execution=ExecutionSettings(),
            runtime_pool=RuntimePoolSettings(
                max_profiles=2,
                idle_ttl_seconds=600,
                pin_default_profile=pin_default_profile,
            ),
            paths=(),
            workspace=tmp_path,
            sources={},
        )

    server = JsonRpcServer(config_home=tmp_path / "home")
    server._config = config("fast-v1", pin_default_profile=True)
    server._load_config = lambda: None  # type: ignore[method-assign]
    store = Store()
    server._thread_store = store  # type: ignore[assignment]
    builds = 0

    async def build(profile: object) -> AgentRuntime:
        nonlocal builds
        builds += 1
        return AgentRuntime(profile=profile, graph=object())  # type: ignore[arg-type]

    server._build_default_runtime = build  # type: ignore[method-assign]
    first_lease, first_runtime = await server._acquire_default_runtime("thread-a")
    second_lease, second_runtime = await server._acquire_default_runtime("thread-b")

    assert first_lease is not None and second_lease is not None
    assert first_runtime is second_runtime
    assert builds == 1
    assert set(store.profiles) == {"thread-a", "thread-b"}

    await server._release_runtime_lease(first_lease)
    await server._release_runtime_lease(second_lease)
    old_runtime = first_runtime

    server._config = config("fast-v2", pin_default_profile=True)
    third_lease, third_runtime = await server._acquire_default_runtime("thread-c")

    assert third_lease is not None
    assert third_runtime is not old_runtime
    assert builds == 2
    assert old_runtime is not None and old_runtime.graph is None

    await server._release_runtime_lease(third_lease)
    await server._close_runtime_pool()


async def test_default_context_compact_acquires_and_releases_profile_runtime(tmp_path: Path):
    """默认 compact 也必须经 RuntimePool 租用图，完成后不残留 thread 专属引用。"""
    from langchain_core.messages import HumanMessage

    from harness_agent.agent_runtime import AgentRuntime, AgentRuntimeState
    from harness_agent.config import (
        ExecutionSettings,
        ModelSettings,
        RuntimePoolSettings,
        Za38Config,
    )
    from harness_agent.context_window import ContextUpdate
    from harness_agent.runtime_profile import component_fingerprint
    from harness_agent.server import JsonRpcServer, _RuntimeArtifacts

    class Store:
        project_fingerprint = component_fingerprint({"project": "compact-runtime"})

        def __init__(self) -> None:
            self.profiles: dict[str, object] = {}
            self.refreshed: list[str] = []

        async def get_runtime_profile(self, thread_id: str) -> object | None:
            return self.profiles.get(thread_id)

        async def save_runtime_profile(self, thread_id: str, profile: object) -> None:
            self.profiles[thread_id] = profile

        async def load_context_messages(self, _thread_id: str) -> list[HumanMessage]:
            return [HumanMessage(content="历史")]

        async def refresh_thread(self, thread_id: str) -> None:
            self.refreshed.append(thread_id)

        @staticmethod
        def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
            return {"configurable": {"thread_id": thread_id}}

    class Middleware:
        async def compact_now(self, thread_id: str, _messages: list[HumanMessage]):
            return (
                [HumanMessage(content="摘要")],
                ContextUpdate(
                    thread_id=thread_id,
                    action="manual_summary",
                    estimated_tokens=8,
                    input_cap_tokens=100,
                    context_window_tokens=128,
                    dynamic_tokens=4,
                ),
                True,
            )

        @staticmethod
        def consume_updates(_thread_id: str) -> tuple[()]:
            return ()

    class Graph:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        async def aupdate_state(
            self, _config: dict[str, object], update: dict[str, object], *, as_node: str
        ) -> None:
            assert as_node == "model"
            self.updates.append(update)

    server = JsonRpcServer(config_home=tmp_path / "home")
    server._initialized = True
    server._enabled_capabilities = {"context.manage"}
    server._config = Za38Config(
        model=ModelSettings(name="fast", base_url="https://gateway.example/v1"),
        model_profile="default",
        execution=ExecutionSettings(),
        runtime_pool=RuntimePoolSettings(),
        paths=(),
        workspace=tmp_path,
        sources={},
    )
    server._load_config = lambda: None  # type: ignore[method-assign]
    store = Store()
    server._thread_store = store  # type: ignore[assignment]
    graph = Graph()

    async def build(profile: object) -> AgentRuntime:
        server._runtime_artifacts[profile.profile_key] = _RuntimeArtifacts(  # type: ignore[attr-defined]
            execution_context=object(),
            context_compactor=Middleware(),
        )
        return AgentRuntime(profile=profile, graph=graph)  # type: ignore[arg-type]

    server._build_default_runtime = build  # type: ignore[method-assign]
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("context.compact", {"thread_id": "thread"}, "compact-default"))

    assert frames[0]["result"]["compacted"] is True
    assert store.refreshed == ["thread"]
    assert len(graph.updates) == 1
    pool = server._runtime_pool
    assert pool is not None
    runtime = await pool.runtime_for(next(iter(store.profiles.values())).profile_key)  # type: ignore[union-attr]
    assert runtime is not None and runtime.state == AgentRuntimeState.IDLE
    await server._close_runtime_pool()


async def test_runtime_pool_capacity_is_reported_as_stable_rpc_error():
    """无安全淘汰候选时控制面返回稳定资源繁忙码，而不是内部异常类型。"""
    from harness_agent.agent_runtime import RuntimePoolCapacityError
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    server._initialized = True

    async def busy(_params: dict[str, Any], _request_id: str) -> None:
        raise RuntimePoolCapacityError("RUNTIME_POOL_CAPACITY_EXHAUSTED")

    server._handlers["runtime.busy"] = busy
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("runtime.busy", {}, "busy"))

    assert frames[0]["error"] == {
        "code": -32030,
        "message": "RUNTIME_POOL_CAPACITY_EXHAUSTED",
        "data": {"code": "RUNTIME_POOL_CAPACITY_EXHAUSTED"},
    }


def test_stream_translation_prefers_normalized_content_blocks():
    """首轮仅提供 content_blocks 时仍必须产生正文事件。"""
    from types import SimpleNamespace

    from harness_agent.server import ActiveRun, JsonRpcServer

    chunk = SimpleNamespace(
        content="",
        content_blocks=[{"type": "text", "text": "首轮回复"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 4},
        tool_call_chunks=[],
    )
    run = ActiveRun(thread_id="thread", run_id="run", message="你好")
    events = list(JsonRpcServer(allow_echo=True)._translate_stream_event(((), "messages", (chunk, {})), run))

    assert events == [("content.delta", {"text": "首轮回复"})]
    assert run.usage == {"input_tokens": 10, "output_tokens": 4}


def test_tool_fragments_with_missing_ids_are_merged_by_index():
    """工具名和参数分片缺少重复 id 时仍应归并为同一协议工具。"""
    from types import SimpleNamespace

    from harness_agent.server import ActiveRun, JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    run = ActiveRun(thread_id="thread", run_id="run", message="执行 pwd")
    first = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-1", "name": "execute", "args": ""}])
    second = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": None, "name": None, "args": '{"command":"pwd"}'}])
    result = type("ToolMessage", (), {"content": "/workspace", "tool_call_id": "call-1", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()

    events = [
        *server._translate_stream_event(((), "messages", (first, {})), run),
        *server._translate_stream_event(((), "messages", (second, {})), run),
        *server._translate_stream_event(((), "messages", (result, {})), run),
    ]

    assert [payload["tool_call_id"] for _, payload in events] == ["call-1", "call-1", "call-1"]
    assert [event_type for event_type, _ in events] == ["tool.started", "tool.delta", "tool.completed"]


def test_tool_stream_reuses_index_for_later_calls_without_overwriting_history():
    """每轮工具流重置 index 时，新的真实调用 ID 仍必须产生独立事件。"""
    from types import SimpleNamespace

    from harness_agent.server import ActiveRun, JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    run = ActiveRun(thread_id="thread", run_id="run", message="连续执行两次")
    first = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-1", "name": "execute", "args": ""}])
    first_result = type("ToolMessage", (), {"content": "first result", "tool_call_id": "call-1", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()
    second = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-2", "name": "execute", "args": ""}])
    second_result = type("ToolMessage", (), {"content": "second result", "tool_call_id": "call-2", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()

    events = [
        *server._translate_stream_event(((), "messages", (first, {})), run),
        *server._translate_stream_event(((), "messages", (first_result, {})), run),
        *server._translate_stream_event(((), "messages", (second, {})), run),
        *server._translate_stream_event(((), "messages", (second_result, {})), run),
    ]

    assert [(event_type, payload["tool_call_id"]) for event_type, payload in events] == [
        ("tool.started", "call-1"),
        ("tool.completed", "call-1"),
        ("tool.started", "call-2"),
        ("tool.completed", "call-2"),
    ]
    assert events[1][1]["result"]["content"] == "first result"
    assert events[3][1]["result"]["content"] == "second result"


async def test_multiple_threads_run_concurrently_but_same_thread_is_rejected():
    """不同 thread 可并发，同一 thread 的第二个活动 run 被拒绝。"""
    from harness_agent.server import JsonRpcServer

    releases = {"t1": asyncio.Event(), "t2": asyncio.Event()}

    class BlockingAgent:
        async def astream(self, _input: Any, *, config: dict[str, Any], **_kwargs: Any):
            thread_id = config["configurable"]["thread_id"]
            yield ("messages", (type("Chunk", (), {"content": thread_id, "usage_metadata": None, "tool_call_chunks": []})(), {}))
            await releases[thread_id].wait()

    server = JsonRpcServer(agent=BlockingAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "a", "thread_id": "t1", "run_id": "r1"}, "start-1"))
    await server.dispatch(_request("run.start", {"message": "b", "thread_id": "t2", "run_id": "r2"}, "start-2"))
    await server.dispatch(_request("run.start", {"message": "c", "thread_id": "t1", "run_id": "r3"}, "start-3"))
    assert any(frame.get("id") == "start-3" and frame.get("error", {}).get("code") == -32000 for frame in frames)
    first_run = server._runs["t1"]
    second_run = server._runs["t2"]
    await server.dispatch(_request("run.cancel", {"thread_id": "t1", "run_id": "r1"}, "cancel-1"))
    await server.dispatch(_request("run.cancel", {"thread_id": "t2", "run_id": "r2"}, "cancel-2"))
    await _wait_for(frames, lambda frame: _event_count(frames, "run.cancelled") == 2)
    assert first_run.cancellation_token.cancelled is True
    assert second_run.cancellation_token.cancelled is True


async def test_question_request_uses_standard_response_and_stable_question_id():
    """AskUser interrupt 通过 Agent→Client request 恢复，不再调用 respond 方法。"""
    from langgraph.types import Command, Interrupt
    from harness_agent.server import JsonRpcServer

    class AskAgent:
        def __init__(self) -> None:
            self.inputs: list[object] = []

        async def astream(self, stream_input: object, **_kwargs: Any):
            self.inputs.append(stream_input)
            if len(self.inputs) == 1:
                yield ("updates", {"__interrupt__": (Interrupt({"type": "ask_user", "questions": [{"question": "目录？", "type": "multiple_choice", "choices": [{"value": "src"}]}]}, id="ask-1"),)})
                return
            assert isinstance(stream_input, Command)
            assert stream_input.resume == {"ask-1": {"status": "answered", "answers": ["src"]}}
            yield ("messages", (type("Chunk", (), {"content": "完成", "usage_metadata": None, "tool_call_chunks": []})(), {}))

    server = JsonRpcServer(agent=AskAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "开始", "thread_id": "t", "run_id": "r"}, "start"))
    interaction = await _wait_for(frames, lambda frame: frame.get("method") == "request")
    assert interaction["id"] == interaction["params"]["request_id"] == "ask-1"
    assert interaction["params"]["payload"]["questions"][0]["id"] == "question-1"
    await server.dispatch({"jsonrpc": "2.0", "id": "ask-1", "result": {"type": "question", "request_id": "ask-1", "answers": {"question-1": ["src"]}}})
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    assert "interaction.resolved" in _event_types(frames)


async def test_real_hitl_rejection_prevents_file_write():
    """真实 deepagents 写入审批被拒绝后不得落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        destination = Path(workspace) / "blocked.txt"
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": str(destination), "content": "x"}, "id": "call-1"}]), AIMessage(content="已拒绝")])
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(model, cwd=workspace, enable_skills=False, enable_memory=False, enable_ask_user=False, approval_mode="default")
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(_request("run.start", {"message": "写入", "thread_id": "t", "run_id": "r"}, "start"))
        interaction = await _wait_for(frames, lambda frame: frame.get("method") == "request")
        await server.dispatch({"jsonrpc": "2.0", "id": interaction["id"], "result": {"type": "approval", "request_id": interaction["id"], "decision": "reject"}})
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert not destination.exists()


async def test_workspace_rejection_precedes_default_approval_request():
    """越界文件调用在 HITL 前被拒绝，避免用户看到无法改变边界的审批框。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace, TemporaryDirectory() as outside:
        destination = Path(outside) / "must-not-write.md"
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": str(destination), "content": "blocked"},
                            "id": "call-outside",
                        }
                    ],
                ),
                AIMessage(content="越界已拒绝"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=workspace,
            approval_mode="default",
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "越界写入", "thread_id": "outside", "run_id": "outside-run"}, "outside-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not destination.exists()
        assert not any(frame.get("method") == "request" for frame in frames)


async def test_auto_edit_writes_without_interruption_but_shell_still_requires_approval():
    """自动编辑模式只跳过 write_file；execute 仍必须由客户端明确拒绝或批准。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        write_model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": str(Path(workspace) / "auto.txt"),
                                "content": "written",
                            },
                            "id": "call-write",
                        }
                    ],
                ),
                AIMessage(content="写入完成"),
            ]
        )
        write_model.profile = {"max_input_tokens": 200_000}
        write_agent = create_harness_agent(
            write_model,
            cwd=workspace,
            approval_mode="auto-edit",
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        write_server = JsonRpcServer(agent=write_agent)
        write_frames = await _capture_server(write_server)
        await write_server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "write", "run_id": "write-run"}, "write-start")
        )
        await _wait_for(write_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert (Path(workspace) / "auto.txt").read_text(encoding="utf-8") == "written"
        assert not any(frame.get("method") == "request" for frame in write_frames)

        shell_model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "execute", "args": {"command": "pwd"}, "id": "call-shell"}
                    ],
                ),
                AIMessage(content="命令被拒绝"),
            ]
        )
        shell_model.profile = {"max_input_tokens": 200_000}
        shell_agent = create_harness_agent(
            shell_model,
            cwd=workspace,
            approval_mode="auto-edit",
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        shell_server = JsonRpcServer(agent=shell_agent)
        shell_frames = await _capture_server(shell_server)
        await shell_server.dispatch(
            _request("run.start", {"message": "执行", "thread_id": "shell", "run_id": "shell-run"}, "shell-start")
        )
        interaction = await _wait_for(shell_frames, lambda frame: frame.get("method") == "request")
        assert interaction["params"]["type"] == "approval"
        await shell_server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": interaction["id"],
                "result": {"type": "approval", "request_id": interaction["id"], "decision": "reject"},
            }
        )
        await _wait_for(shell_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")


async def test_plan_mode_returns_tool_message_without_writing_or_requesting_approval():
    """计划模式写工具调用必须由内核硬拒绝，不能先交给 TUI 或落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": str(Path(workspace) / "plan.txt"),
                                "content": "must not write",
                            },
                            "id": "call-plan-write",
                        }
                    ],
                ),
                AIMessage(content="已提供计划"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=workspace,
            approval_mode="plan",
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "plan", "run_id": "plan-run"}, "plan-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not (Path(workspace) / "plan.txt").exists()
        assert not any(frame.get("method") == "request" for frame in frames)


async def test_missing_interaction_capability_fails_closed_without_reverse_request():
    """无头客户端不声明交互能力时，服务端直接返回拒绝而不发送 request。"""
    from harness_agent.server import ActiveRun, InteractionSpec, JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(
        _request(
            "initialize",
            _initialize_params(capabilities=["run.cancel", "run.multithread", "config.read"]),
            "init-headless",
        )
    )
    result = await server._request_interaction(
        ActiveRun(thread_id="headless", run_id="headless-run", message="test"),
        InteractionSpec(
            request_id="approval-headless",
            type="approval",
            payload={},
            interrupt_id="approval-headless",
        ),
    )

    assert result == {"type": "approval", "request_id": "approval-headless", "decision": "reject"}
    assert not any(frame.get("method") == "request" for frame in frames)


def test_tool_output_is_utf8_safely_truncated():
    """超限工具输出携带截断标记和原始字节数。"""
    from harness_agent.protocol_generated import MAX_TOOL_PAYLOAD_BYTES
    from harness_agent.server import _truncate_text

    original = "界" * (MAX_TOOL_PAYLOAD_BYTES // 2)
    clipped, truncated, original_bytes = _truncate_text(original)
    assert truncated is True
    assert len(clipped.encode()) <= MAX_TOOL_PAYLOAD_BYTES
    assert original_bytes == len(original.encode())


async def test_stdio_subprocess_end_to_end_echo_mode():
    """真实 sidecar 完成 v2 initialize、run.start、event 与 shutdown。"""
    package_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(package_root), "HARNESS_ECHO_MODE": "1"}
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "harness_agent", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    assert process.stdin and process.stdout
    process.stdin.write((json.dumps(_request("initialize", _initialize_params(), "init")) + "\n" + json.dumps(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "start")) + "\n").encode())
    await process.stdin.drain()
    frames: list[dict[str, Any]] = []
    while not any(frame.get("params", {}).get("type") == "run.completed" for frame in frames):
        frames.append(json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=2)))
    process.stdin.write((json.dumps(_request("shutdown", {}, "stop")) + "\n").encode())
    await process.stdin.drain()
    await asyncio.wait_for(process.wait(), timeout=2)
    assert "content.delta" in _event_types(frames)


async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)


def _event_count(frames: list[dict[str, Any]], event_type: str) -> int:
    return sum(frame.get("params", {}).get("type") == event_type for frame in frames)
