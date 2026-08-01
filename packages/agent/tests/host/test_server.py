"""v3 JSON-RPC 握手、并发运行、双向交互和真实 stdio 回归测试。"""

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
    requested = overrides.pop(
        "capabilities",
        [
            "run.cancel",
            "run.multithread",
            "config.read",
            "threads.read",
            "context.manage",
            "skills.read",
            "models.read",
            "models.select",
            "mcp.read",
            "mcp.manage",
            "config.write",
            "skills.manage",
            "interactive.approval",
            "interactive.question",
        ],
    )
    if isinstance(requested, list):
        handles = [
            capability.removeprefix("interactive.")
            for capability in requested
            if capability.startswith("interactive.")
        ]
        requested = [
            capability
            for capability in requested
            if not capability.startswith("interactive.")
        ]
    else:
        handles = []
    params: dict[str, Any] = {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": requested, "handles": handles},
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


async def test_initialize_negotiates_v3_and_capabilities(tmp_path: Path):
    """握手返回选定 minor、能力交集、限制和脱敏配置摘要。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True, config_home=tmp_path / "home")
    frames = await _capture_server(server)
    result = frames[0]["result"]
    assert result["protocol"] == {"major": 3, "minor": 0}
    assert "run.multithread" in result["capabilities"]["enabled"]
    assert result["limits"]["max_frame_bytes"] == 8 * 1024 * 1024
    assert result["config_summary"]["security"]["mode"] == "local"


async def test_initialize_rejects_incompatible_major_and_pre_initialize_calls():
    """不兼容 Major 和握手前业务调用必须被结构化拒绝。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request("run.start", {"message": "x"}, "run-early"))
    await server.dispatch(_request("initialize", _initialize_params(protocol={"major": 9, "min_minor": 0, "max_minor": 0}), "init-bad"))
    assert [frame["error"]["code"] for frame in frames] == [-32000, -32003]


async def test_config_show_exposes_redacted_agent_engine_pool_diagnostics(tmp_path: Path):
    """已有 config.show 提供 Pool 本地诊断，且不能泄露完整 Profile Key。"""
    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool
    from harness_agent.runtime.agent_engine_profile import ModelRoleBinding, AgentEngineProfile, component_fingerprint
    from harness_agent.host.agent_host import AgentHost

    def fingerprint(component: str) -> str:
        return component_fingerprint({"server-diagnostics": component})

    profile = AgentEngineProfile(
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
    server = AgentHost(allow_echo=True, config_home=tmp_path / "home")
    frames = await _capture_server(server)
    pool = AgentEnginePool(lambda requested: AgentEngine(profile=requested, graph=object()))
    server._agent_engine_pool = pool
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


async def test_config_write_rpc_previews_and_commits_user_default_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """v3 配置写接口必须协商 capability、返回 CAS revision 并只修改用户白名单字段。"""
    from harness_agent.host.agent_host import AgentHost

    home = tmp_path / "home"
    config_path = home / ".harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '''[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
model = "fast-model"
base_url = "https://gateway.example/v1"
api_key_env = "HARNESS_FAST_KEY"

[models.profiles.pro]
model = "pro-model"
base_url = "https://gateway.example/v1"
api_key_env = "HARNESS_PRO_KEY"

[models.roles]
executor = "fast"
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_FAST_KEY", "fast-test")
    monkeypatch.setenv("HARNESS_PRO_KEY", "pro-test")
    server = AgentHost(config_home=home)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(
        _request(
            "initialize",
            _initialize_params(
                capabilities=["config.write", "run.multithread"],
            ),
            "init-config-write",
        )
    )
    assert "config.write" in frames[-1]["result"]["capabilities"]["enabled"]

    await server.dispatch(_request("config.details", {}, "config-details"))
    details = frames[-1]["result"]
    assert next(field for field in details["fields"] if field["path"] == "models.default_profile")["editable"] is True

    await server.dispatch(
        _request(
            "config.preview",
            {"changes": [{"path": "models.default_profile", "value": "pro"}]},
            "config-preview",
        )
    )
    preview = frames[-1]["result"]
    await server.dispatch(
        _request(
            "config.commit",
            {
                "expected_revision": preview["revision"],
                "changes": [{"path": "models.default_profile", "value": "pro"}],
            },
            "config-commit",
        )
    )
    assert frames[-1]["result"]["applies_to"] == ["new-thread"]
    assert "default_profile = \"pro\"" in config_path.read_text(encoding="utf-8")
    assert server._config is not None
    assert server._config.model_profile == "pro"

    from harness_agent.host.run_coordinator import RunRuntime

    async def no_runtime(_run: Any) -> RunRuntime:
        """避免真实模型调用，仅保留新 Thread 解析的持久化事实。"""

        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    server._run_coordinator._runtime_provider = no_runtime
    await server.dispatch(
        _request(
            "run.start",
            {"message": "全新 Thread 必须使用 pro", "thread_id": "fresh-thread", "run_id": "fresh-run"},
            "fresh-start",
        )
    )
    assert frames[-1].get("result", {}).get("accepted") is True, frames[-1]
    await asyncio.sleep(0)
    assert server._thread_persistence is not None
    binding = (await server._thread_persistence.load_run_state("fresh-thread")).latest_run
    assert binding is not None
    assert binding.actual_primary.profile_id == "pro"
    await server._close_thread_persistence()


async def test_config_write_requires_capability(tmp_path: Path) -> None:
    """未显式协商 config.write 的客户端不能调用配置写接口。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True, config_home=tmp_path / "home")
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(
        _request(
            "initialize",
            _initialize_params(
                capabilities=["config.read"],
            ),
            "init-config-write-old",
        )
    )
    assert "config.write" not in frames[-1]["result"]["capabilities"]["enabled"]
    await server.dispatch(_request("config.details", {}, "config-details-old"))
    assert frames[-1]["error"]["data"]["code"] == "CAPABILITY_REQUIRED"

    await server._close_agent_engine_pool()
    await server.dispatch(_request("config.show", {}, "config-runtime-closed"))
    assert frames[-1]["result"]["runtime_pool_diagnostics"]["state"] == "not_initialized"


async def test_project_configuration_failure_prevents_agent_factory_invocation(tmp_path: Path):
    """未可信项目配置必须在创建模型或 Agent 之前以启动错误终止。"""
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    project_config = workspace / ".harness" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text("[config]\nversion = 1\n", encoding="utf-8")
    invoked = False

    def factory(*_: Any) -> object:
        nonlocal invoked
        invoked = True
        return object()

    server = AgentHost(
        agent_factory=factory,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(_request("initialize", _initialize_params(), "init-project"))
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
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True)
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "run-1"))
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    run_frames = frames[1:]
    assert run_frames[0]["result"]["accepted"] is True
    assert _event_types(run_frames) == ["run.started", "content.delta", "run.completed"]
    assert [frame["params"]["sequence"] for frame in run_frames if frame.get("method") == "event"] == [1, 2, 3]


async def test_run_started_emits_authoritative_primary_model_binding():
    """run.started 必须携带本次 Run 的脱敏实际模型，而非 TUI 的本地选择。"""
    from harness_agent.runtime.execution_binding import (
        RunExecutionBinding,
        SafeModelProfile,
        SelectionOrigin,
        ThreadExecutionSelection,
    )
    from harness_agent.host.run_coordinator import (
        ConnectionRef,
        RunCoordinator,
        RunPreparation,
        RunRuntime,
        StartRun,
    )

    binding = RunExecutionBinding(
        thread_id="thread-model",
        run_id="run-model",
        requested_selection=ThreadExecutionSelection("pro"),
        actual_primary=SafeModelProfile.from_record({
            "id": "pro",
            "model": "pro-model",
            "provider_label": "Pro Gateway",
            "context_window_tokens": 256000,
            "capabilities": ["streaming", "tool-calling"],
            "is_default": False,
            "available": True,
            "unavailable_reason": None,
            "source": "user",
        }),
        selection_origin=SelectionOrigin.REQUEST,
        runtime_profile_id="123456789abc",
        created_at_ms=1,
    )
    async def preparation(_command: StartRun, _persistence: object) -> RunPreparation:
        return RunPreparation(execution_binding=binding)

    async def runtime(_run: object) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    async def no_persistence() -> None:
        return None

    coordinator = RunCoordinator(
        persistence_provider=no_persistence,
        preparation_provider=preparation,
        runtime_provider=runtime,
        interaction_port=object(),  # type: ignore[arg-type]
        skill_registry_provider=lambda: None,  # type: ignore[return-value]
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-model", run_id="run-model", message="使用 pro"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    started = next(event for event in events if event.type == "run.started")
    assert started.payload["runtime_profile_id"] == "123456789abc"
    assert started.payload["primary_model"] == binding.protocol_primary_model()


async def test_context_compact_rewrites_idle_thread_and_returns_context_summary():
    """手动压缩只允许空闲 thread，成功后写回 checkpoint 并同步摘要状态。"""
    from langchain_core.messages import HumanMessage

    from harness_agent.threads.context_window import ContextUpdate
    from harness_agent.host.agent_host import AgentHost
    from harness_agent.threads.thread_persistence import ContextSnapshot, ContextState

    class Store:
        def __init__(self) -> None:
            self.refreshed: list[str] = []

        async def load_context(self, _thread_id: str) -> ContextSnapshot:
            return ContextSnapshot(
                messages=(HumanMessage(content="旧上下文"),),
                state=ContextState(),
                recoverable=True,
            )

        async def complete_run(self, thread_id: str) -> None:
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
    server = AgentHost(agent=agent)
    server._owner_connection.initialized = True
    server._owner_connection.enabled_capabilities = {"context.manage"}
    server._thread_persistence = store  # type: ignore[assignment]
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
    from harness_agent.host.run_coordinator import ConnectionRef, RunPreparation, RunState, StartRun
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost()
    server._owner_connection.initialized = True
    server._owner_connection.enabled_capabilities = {"context.manage"}
    server._run_coordinator._runs["thread"] = RunState(
        start=StartRun(thread_id="thread", run_id="run", message="运行中"),
        owner=ConnectionRef(server._owner_connection.connection_id),
        persistence=None,
        preparation=RunPreparation(),
    )
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("context.compact", {"thread_id": "thread"}, "compact-active"))

    assert frames[0]["error"]["message"] == "CONTEXT_COMPACTION_RUN_ACTIVE"


async def test_models_list_and_run_start_resolve_current_thread_selection(tmp_path: Path, monkeypatch) -> None:
    """models.select 让同一 Thread 的后续 Run 立即采用新主模型，并保留历史事实。"""
    from harness_agent.host.agent_host import AgentHost

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
    server = AgentHost(config_home=home, workspace=workspace)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request(
        "initialize",
        _initialize_params(
            capabilities=["models.read", "models.select"],
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

    from harness_agent.host.run_coordinator import RunRuntime

    async def no_runtime(_run: Any) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    server._run_coordinator._runtime_provider = no_runtime
    await server.dispatch(_request(
        "run.start",
        {
            "message": "使用 pro",
            "thread_id": "thread-model",
            "run_id": "first",
            "model_selection": {"primary_profile": "pro"},
        },
        "start-model",
    ))
    assert frames[-1]["result"]["accepted"] is True
    await asyncio.sleep(0)
    assert server._thread_persistence is not None
    assert (
        await server._thread_persistence.load_run_state("thread-model")
    ).legacy_models is None
    first = (await server._thread_persistence.load_run_state("thread-model")).latest_run
    assert first is not None
    assert first.requested_selection.to_record() == {"primary_profile": "pro"}
    assert first.actual_primary.profile_id == "pro"
    assert server._config is not None
    resolved = await server._resolve_execution_binding("thread-model", server._config)
    agent_engine_profile = await server._resolve_agent_engine_profile(
        "thread-model",
        server._config,
        resolved,
    )
    assert server._resolved_agent_specs[agent_engine_profile.profile_key].model_settings.name == "pro-model"

    await server.dispatch(_request(
        "run.start",
        {
            "message": "切换 fast",
            "thread_id": "thread-model",
            "run_id": "second",
            "model_selection": {"primary_profile": "fast"},
        },
        "start-model-again",
    ))
    assert frames[-1]["result"]["accepted"] is True
    second = (await server._thread_persistence.load_run_state("thread-model")).latest_run
    assert second is not None
    assert second.requested_selection.to_record() == {"primary_profile": "fast"}
    assert second.actual_primary.profile_id == "fast"

    await server.dispatch(_request("models.list", {"thread_id": "thread-model"}, "models-bound"))
    model_state = frames[-1]["result"]
    assert model_state["thread_selection"] == {"primary_profile": "fast"}
    assert model_state["last_run_binding"]["profile"]["model"] == "fast-model"
    await server._close_thread_persistence()


async def test_default_sidecar_shares_engine_by_profile_without_draining_other_models(
    tmp_path: Path,
):
    """默认 Sidecar 以 Profile 而非 thread 缓存图；模型快照变化只排空旧 Profile。"""
    from harness_agent.agent_engine import AgentEngine
    from harness_agent.config import (
        ExecutionSettings,
        ModelSettings,
        AgentEnginePoolSettings,
        Za38Config,
    )
    from harness_agent.runtime.agent_engine_profile import component_fingerprint
    from harness_agent.host.agent_host import AgentHost

    class Store:
        project_fingerprint = component_fingerprint({"project": "server-runtime"})

        def __init__(self) -> None:
            self.profiles: dict[str, object] = {}

        async def persist_agent_engine_profile(self, profile: object) -> None:
            self.profiles[str(getattr(profile, "profile_key"))] = profile

        async def load_run_state(self, _thread_id: str) -> object:
            from harness_agent.runtime.execution_binding import PersistedBindingState

            return PersistedBindingState()

    def config(model_name: str, *, pin_default_profile: bool = False) -> Za38Config:
        return Za38Config(
            model=ModelSettings(
                name=model_name,
                base_url="https://gateway.example/v1",
                api_key="test-key",
            ),
            model_profile="default",
            execution=ExecutionSettings(),
            agent_engine_pool=AgentEnginePoolSettings(
                max_profiles=2,
                idle_ttl_seconds=600,
                pin_default_profile=pin_default_profile,
            ),
            paths=(),
            workspace=tmp_path,
            sources={},
        )

    server = AgentHost(config_home=tmp_path / "home")
    server._config = config("fast-v1", pin_default_profile=True)
    server._load_config = lambda: None  # type: ignore[method-assign]
    store = Store()
    server._thread_persistence = store  # type: ignore[assignment]
    builds = 0

    async def build(profile: object) -> AgentEngine:
        nonlocal builds
        builds += 1
        return AgentEngine(profile=profile, graph=object())  # type: ignore[arg-type]

    server._build_default_agent_engine = build  # type: ignore[method-assign]
    first_lease, first_engine = await server._acquire_default_agent_engine("thread-a")
    second_lease, second_engine = await server._acquire_default_agent_engine("thread-b")

    assert first_lease is not None and second_lease is not None
    assert first_engine is second_engine
    assert builds == 1
    assert len(store.profiles) == 1

    await server._release_agent_engine_lease(first_lease)
    await server._release_agent_engine_lease(second_lease)
    old_engine = first_engine

    server._config = config("fast-v2", pin_default_profile=True)
    third_lease, third_engine = await server._acquire_default_agent_engine("thread-c")

    assert third_lease is not None
    assert third_engine is not old_engine
    assert builds == 2
    assert len(store.profiles) == 2
    # Ordinary resolution of a different model must not infer a global
    # invalidation; both stable Profiles remain available until an explicit
    # snapshot-change event targets one of them.
    assert old_engine is not None and old_engine.graph is not None

    await server._release_agent_engine_lease(third_lease)
    await server._close_agent_engine_pool()


async def test_host_snapshot_boundary_serializes_update_with_single_flight_acquire() -> None:
    """更新等待旧 Run 的首建完成，新 Profile 才能在失效边界后取得图。"""
    from types import SimpleNamespace

    from harness_agent.agent_engine import AgentEngine, AgentEnginePool, AgentEngineState
    from harness_agent.agent_engine_profile import (
        AgentEngineProfile,
        ModelRoleBinding,
        component_fingerprint,
    )
    from harness_agent.mcp import McpServerConfig, build_mcp_snapshot
    from harness_agent.server import AgentHost

    def profile(mcp_fingerprint: str) -> AgentEngineProfile:
        def fingerprint(component: str) -> str:
            return component_fingerprint({"snapshot-boundary": component})

        return AgentEngineProfile(
            project_fingerprint=fingerprint("project"),
            topology_id="single-agent",
            topology_version=1,
            model_roles=(
                ModelRoleBinding(
                    role="primary",
                    model_config_fingerprint=fingerprint("model"),
                ),
            ),
            tool_catalog_fingerprint=fingerprint("tools"),
            skill_catalog_fingerprint=fingerprint("skills"),
            mcp_config_fingerprint=mcp_fingerprint,
            sandbox_config_fingerprint=fingerprint("sandbox"),
            policy_fingerprint=fingerprint("policy"),
            middleware_fingerprint=fingerprint("middleware"),
            prompt_template_fingerprint=fingerprint("prompt"),
        )

    old_snapshot = build_mcp_snapshot([], revision="old-boundary")
    new_snapshot = build_mcp_snapshot(
        [McpServerConfig(name="updated", transport="stdio", command="updated")],
        revision="new-boundary",
    )
    old_profile = profile(old_snapshot.digest)
    new_profile = profile(new_snapshot.digest)
    builder_started = asyncio.Event()
    release_builder = asyncio.Event()
    order: list[str] = []

    async def build(requested: AgentEngineProfile) -> AgentEngine:
        builder_started.set()
        await release_builder.wait()
        order.append("build_finished")
        return AgentEngine(profile=requested, graph=object())

    class Manager:
        reaps = 0

        async def reap(self) -> None:
            self.reaps += 1

    server = AgentHost(allow_echo=False)
    server._config = SimpleNamespace(model=object())
    server._load_config = lambda: None  # type: ignore[method-assign]
    server._agent_engine_pool = AgentEnginePool(build)
    manager = Manager()
    server._mcp_manager = manager  # type: ignore[assignment]

    acquire_task = asyncio.create_task(
        server._acquire_default_agent_engine("old-thread", profile=old_profile)
    )
    await builder_started.wait()

    async def update() -> None:
        async with server._agent_engine_snapshot_lock:
            await server._invalidate_profiles_for_snapshot(
                new_snapshot,
                reason="mcp_snapshot_changed",
            )
            order.append("invalidated")

    update_task = asyncio.create_task(update())
    await asyncio.sleep(0)
    assert not update_task.done()

    release_builder.set()
    old_lease, old_engine = await acquire_task
    await update_task
    assert order.index("build_finished") < order.index("invalidated")
    assert old_engine is not None
    assert await server._agent_engine_pool.state_for(old_profile.profile_key) == AgentEngineState.DRAINING
    assert old_engine.graph is not None
    assert manager.reaps == 1

    await server._release_agent_engine_lease(old_lease)
    assert old_engine.graph is None

    new_lease, new_engine = await server._acquire_default_agent_engine(
        "new-thread",
        profile=new_profile,
    )
    assert new_engine is not old_engine
    await server._release_agent_engine_lease(new_lease)
    await server._close_agent_engine_pool()


async def test_default_engine_builder_passes_one_host_lock_to_each_profile(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """不同 Profile 的默认图必须共享所属 AgentHost 的工具读写锁。"""
    from types import SimpleNamespace

    import harness_agent.agent as agent_module
    import harness_agent.context_window as context_window_module
    import harness_agent.execution as execution_module
    import harness_agent.providers.harness_gateway as gateway_module
    from harness_agent.mcp import McpConnectionManager, build_mcp_snapshot
    from harness_agent.server import AgentHost

    captured_locks: list[object] = []
    monkeypatch.setattr(
        agent_module,
        "create_harness_agent",
        lambda *_args, **kwargs: captured_locks.append(kwargs["concurrency_lock"]) or object(),
    )
    monkeypatch.setattr(
        context_window_module,
        "ContextWindowMiddleware",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        execution_module,
        "create_execution_context",
        lambda *_args, **_kwargs: SimpleNamespace(backend=object(), aclose=lambda: None),
    )
    monkeypatch.setattr(
        gateway_module,
        "create_openai_compatible_model",
        lambda *_args, **_kwargs: object(),
    )

    class ProviderLease:
        value = object()

        async def release(self) -> None:
            return None

    class ProviderClients:
        async def acquire(self, _settings: object) -> ProviderLease:
            return ProviderLease()

    async def persistence() -> object:
        return SimpleNamespace(checkpointer=object())

    mcp_snapshot = build_mcp_snapshot([], revision="test")

    def profile(profile_key: str) -> SimpleNamespace:
        return SimpleNamespace(
            profile_key=profile_key,
            mcp_config_fingerprint=mcp_snapshot.digest,
            sandbox_config_fingerprint="sandbox",
        )

    def spec(runtime_profile: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            runtime_profile=runtime_profile,
            mcp_snapshot=mcp_snapshot,
            execution=SimpleNamespace(approval_mode="yolo"),
            workspace=tmp_path,
            model_settings=SimpleNamespace(context_window_tokens=128_000),
            tools=(),
            interactive=False,
            enable_ask_user=False,
            enable_memory=False,
            enable_skills=False,
            effective_policy=SimpleNamespace(approval_mode=None),
            skill_registry=object(),
            pinned=False,
        )

    server = AgentHost(allow_echo=True, workspace=tmp_path)
    server._mcp_manager = McpConnectionManager(mcp_snapshot)
    server._provider_client_pool = ProviderClients()  # type: ignore[assignment]
    server._ensure_thread_persistence = persistence  # type: ignore[method-assign]
    first_profile = profile("profile-first")
    second_profile = profile("profile-second")
    server._resolved_agent_specs = {
        first_profile.profile_key: spec(first_profile),
        second_profile.profile_key: spec(second_profile),
    }

    first = await server._build_default_agent_engine(first_profile)  # type: ignore[arg-type]
    second = await server._build_default_agent_engine(second_profile)  # type: ignore[arg-type]

    assert captured_locks == [server._tool_concurrency_lock, server._tool_concurrency_lock]
    assert captured_locks[0] is captured_locks[1]
    await first.aclose()
    await second.aclose()


async def test_default_context_compact_acquires_and_releases_profile_engine(tmp_path: Path):
    """默认 compact 也必须经 AgentEnginePool 租用图，完成后不残留 thread 专属引用。"""
    from langchain_core.messages import HumanMessage

    from harness_agent.runtime.agent_engine import AgentEngine, AgentEngineState
    from harness_agent.config.config import (
        ExecutionSettings,
        ModelSettings,
        AgentEnginePoolSettings,
        Za38Config,
    )
    from harness_agent.threads.context_window import ContextUpdate
    from harness_agent.runtime.agent_engine_profile import component_fingerprint
    from harness_agent.host.agent_host import AgentHost, _AgentEngineArtifacts

    class Store:
        project_fingerprint = component_fingerprint({"project": "compact-runtime"})

        def __init__(self) -> None:
            self.profiles: dict[str, object] = {}
            self.refreshed: list[str] = []

        async def persist_agent_engine_profile(self, profile: object) -> None:
            self.profiles[str(getattr(profile, "profile_key"))] = profile

        async def load_run_state(self, _thread_id: str) -> object:
            from harness_agent.runtime.execution_binding import PersistedBindingState

            return PersistedBindingState()

        async def load_context(self, _thread_id: str) -> object:
            from harness_agent.threads.thread_persistence import ContextSnapshot, ContextState

            return ContextSnapshot(
                messages=(HumanMessage(content="历史"),),
                state=ContextState(),
                recoverable=True,
            )

        async def complete_run(self, thread_id: str) -> None:
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

    server = AgentHost(config_home=tmp_path / "home")
    server._owner_connection.initialized = True
    server._owner_connection.enabled_capabilities = {"context.manage"}
    server._config = Za38Config(
        model=ModelSettings(
            name="fast",
            base_url="https://gateway.example/v1",
            api_key="test-key",
        ),
        model_profile="default",
        execution=ExecutionSettings(),
        agent_engine_pool=AgentEnginePoolSettings(),
        paths=(),
        workspace=tmp_path,
        sources={},
    )
    server._load_config = lambda: None  # type: ignore[method-assign]
    store = Store()
    server._thread_persistence = store  # type: ignore[assignment]
    graph = Graph()

    async def build(profile: object) -> AgentEngine:
        server._agent_engine_artifacts[profile.profile_key] = _AgentEngineArtifacts(  # type: ignore[attr-defined]
            execution_context=object(),
            context_compactor=Middleware(),
        )
        return AgentEngine(profile=profile, graph=graph)  # type: ignore[arg-type]

    server._build_default_agent_engine = build  # type: ignore[method-assign]
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("context.compact", {"thread_id": "thread"}, "compact-default"))

    assert frames[0]["result"]["compacted"] is True
    assert store.refreshed == ["thread"]
    assert len(graph.updates) == 1
    pool = server._agent_engine_pool
    assert pool is not None
    engine = await pool.engine_for(next(iter(store.profiles.values())).profile_key)  # type: ignore[union-attr]
    assert engine is not None and engine.state == AgentEngineState.IDLE
    await server._close_agent_engine_pool()


async def test_agent_engine_pool_capacity_is_reported_as_stable_rpc_error():
    """无安全淘汰候选时控制面返回稳定资源繁忙码，而不是内部异常类型。"""
    from harness_agent.runtime.agent_engine import AgentEnginePoolCapacityError
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True)
    server._owner_connection.initialized = True
    server._owner_connection.enabled_capabilities = {"config.read"}

    async def busy(_params: dict[str, Any], _request_id: str) -> None:
        raise AgentEnginePoolCapacityError("RUNTIME_POOL_CAPACITY_EXHAUSTED")

    server._handlers["config.show"] = busy
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]

    await server.dispatch(_request("config.show", {}, "busy"))

    assert frames[0]["error"] == {
        "code": -32030,
        "message": "RUNTIME_POOL_CAPACITY_EXHAUSTED",
        "data": {
            "code": "INTERNAL_ERROR",
            "retryable": False,
            "details": {"code": "RUNTIME_POOL_CAPACITY_EXHAUSTED"},
        },
    }


def test_stream_translation_prefers_normalized_content_blocks():
    """首轮仅提供 content_blocks 时仍必须产生正文事件。"""
    from types import SimpleNamespace

    from harness_agent.host.run_coordinator import (
        ConnectionRef,
        RunPreparation,
        RunState,
        StartRun,
        _translate_stream_event,
    )

    chunk = SimpleNamespace(
        content="",
        content_blocks=[{"type": "text", "text": "首轮回复"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 4},
        tool_call_chunks=[],
    )
    run = RunState(
        start=StartRun(thread_id="thread", run_id="run", message="你好"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    events = list(_translate_stream_event(((), "messages", (chunk, {})), run))

    assert events == [("content.delta", {"text": "首轮回复"})]
    assert run.usage == {"input_tokens": 10, "output_tokens": 4}


def test_tool_fragments_with_missing_ids_are_merged_by_index():
    """工具名和参数分片缺少重复 id 时仍应归并为同一协议工具。"""
    from types import SimpleNamespace

    from harness_agent.host.run_coordinator import (
        ConnectionRef,
        RunPreparation,
        RunState,
        StartRun,
        _translate_stream_event,
    )

    run = RunState(
        start=StartRun(thread_id="thread", run_id="run", message="执行 pwd"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-1", "name": "execute", "args": ""}])
    second = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": None, "name": None, "args": '{"command":"pwd"}'}])
    result = type("ToolMessage", (), {"content": "/workspace", "tool_call_id": "call-1", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()

    events = [
        *_translate_stream_event(((), "messages", (first, {})), run),
        *_translate_stream_event(((), "messages", (second, {})), run),
        *_translate_stream_event(((), "messages", (result, {})), run),
    ]

    assert [payload["tool_call_id"] for _, payload in events] == ["call-1", "call-1", "call-1"]
    assert [event_type for event_type, _ in events] == ["tool.started", "tool.delta", "tool.completed"]


def test_tool_stream_reuses_index_for_later_calls_without_overwriting_history():
    """每轮工具流重置 index 时，新的真实调用 ID 仍必须产生独立事件。"""
    from types import SimpleNamespace

    from harness_agent.host.run_coordinator import (
        ConnectionRef,
        RunPreparation,
        RunState,
        StartRun,
        _translate_stream_event,
    )

    run = RunState(
        start=StartRun(thread_id="thread", run_id="run", message="连续执行两次"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-1", "name": "execute", "args": ""}])
    first_result = type("ToolMessage", (), {"content": "first result", "tool_call_id": "call-1", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()
    second = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-2", "name": "execute", "args": ""}])
    second_result = type("ToolMessage", (), {"content": "second result", "tool_call_id": "call-2", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()

    events = [
        *_translate_stream_event(((), "messages", (first, {})), run),
        *_translate_stream_event(((), "messages", (first_result, {})), run),
        *_translate_stream_event(((), "messages", (second, {})), run),
        *_translate_stream_event(((), "messages", (second_result, {})), run),
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
    from harness_agent.host.agent_host import AgentHost

    releases = {"t1": asyncio.Event(), "t2": asyncio.Event()}

    class BlockingAgent:
        async def astream(self, _input: Any, *, config: dict[str, Any], **_kwargs: Any):
            thread_id = config["configurable"]["thread_id"]
            yield ("messages", (type("Chunk", (), {"content": thread_id, "usage_metadata": None, "tool_call_chunks": []})(), {}))
            await releases[thread_id].wait()

    server = AgentHost(agent=BlockingAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "a", "thread_id": "t1", "run_id": "r1"}, "start-1"))
    await server.dispatch(_request("run.start", {"message": "b", "thread_id": "t2", "run_id": "r2"}, "start-2"))
    await server.dispatch(_request("run.start", {"message": "c", "thread_id": "t1", "run_id": "r3"}, "start-3"))
    assert any(frame.get("id") == "start-3" and frame.get("error", {}).get("code") == -32000 for frame in frames)
    await server.dispatch(_request("run.cancel", {"thread_id": "t1", "run_id": "r1"}, "cancel-1"))
    await server.dispatch(_request("run.cancel", {"thread_id": "t2", "run_id": "r2"}, "cancel-2"))
    await _wait_for(frames, lambda frame: _event_count(frames, "run.cancelled") == 2)


async def test_question_request_uses_standard_response_and_stable_question_id():
    """AskUser interrupt 通过 Agent→Client request 恢复，不再调用 respond 方法。"""
    from langgraph.types import Command, Interrupt
    from harness_agent.host.agent_host import AgentHost

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

    server = AgentHost(agent=AskAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "开始", "thread_id": "t", "run_id": "r"}, "start"))
    interaction = await _wait_for(frames, lambda frame: frame.get("method") == "interaction.question")
    assert interaction["id"] == "ask-1"
    assert interaction["params"]["payload"]["questions"][0]["id"] == "question-1"
    await server.dispatch({"jsonrpc": "2.0", "id": "ask-1", "result": {"answers": {"question-1": ["src"]}}})
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    assert "interaction.resolved" in _event_types(frames)


async def test_real_hitl_rejection_prevents_file_write():
    """真实 deepagents 写入审批被拒绝后不得落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        destination = Path(workspace) / "blocked.txt"
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": str(destination), "content": "x"}, "id": "call-1"}]), AIMessage(content="已拒绝")])
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(model, cwd=workspace, enable_skills=False, enable_memory=False, enable_ask_user=False, approval_mode="default")
        server = AgentHost(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(_request("run.start", {"message": "写入", "thread_id": "t", "run_id": "r"}, "start"))
        interaction = await _wait_for(frames, lambda frame: frame.get("method") == "interaction.approval")
        await server.dispatch({"jsonrpc": "2.0", "id": interaction["id"], "result": {"decision": "reject"}})
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert not destination.exists()


async def test_approve_thread_delete_rule_skips_later_deletions_in_same_thread():
    """delete_file 选择“本线程允许”后，同线程后续删除不再弹审批。

    规则注入镜像生产路径：会话规则保存在 RunCoordinator 内存列表，
    Agent 的 rules_provider 每次评估时读取该列表与持久化层合并结果。
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.policy.permission_rules import PermissionRule, load_rules, merge_rules
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        first = Path(workspace) / "first.txt"
        second = Path(workspace) / "second.txt"
        first.write_text("a", encoding="utf-8")
        second.write_text("b", encoding="utf-8")

        host_ref: dict[str, Any] = {}

        def rules_provider() -> list[PermissionRule]:
            host = host_ref.get("host")
            persisted = load_rules(project_dir=Path(workspace))
            if host is not None:
                persisted["session"] = host._run_coordinator.session_rules
            return merge_rules(persisted)

        model = ToolModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "delete_file", "args": {"file_path": str(first)}, "id": "del-1"}]),
                AIMessage(content="已删除第一个文件"),
                AIMessage(content="", tool_calls=[{"name": "delete_file", "args": {"file_path": str(second)}, "id": "del-2"}]),
                AIMessage(content="已删除第二个文件"),
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
            rules_provider=rules_provider,
        )
        server = AgentHost(agent=agent)
        host_ref["host"] = server
        frames = await _capture_server(server)

        # 第一次删除：弹窗审批，选择“本线程允许”后会话规则应落库。
        await server.dispatch(
            _request("run.start", {"message": "删除 first.txt", "thread_id": "del-thread", "run_id": "del-run-1"}, "del-start-1")
        )
        interaction = await _wait_for(frames, lambda frame: frame.get("method") == "interaction.approval")
        await server.dispatch({"jsonrpc": "2.0", "id": interaction["id"], "result": {"decision": "approve_thread"}})
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert not first.exists()
        assert PermissionRule(tool="delete_file", resource="*", effect="allow") in server._run_coordinator.session_rules

        # 第二次删除：会话规则命中，应直接执行，不再出现新的审批请求。
        await server.dispatch(
            _request("run.start", {"message": "删除 second.txt", "thread_id": "del-thread", "run_id": "del-run-2"}, "del-start-2")
        )
        await _wait_for(frames, lambda frame: _event_count(frames, "run.completed") == 2)
        approvals = [frame for frame in frames if frame.get("method") == "interaction.approval"]
        assert len(approvals) == 1
        assert not second.exists()


async def test_workspace_rejection_precedes_default_approval_request():
    """越界文件调用在 HITL 前被拒绝，避免用户看到无法改变边界的审批框。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

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
        server = AgentHost(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "越界写入", "thread_id": "outside", "run_id": "outside-run"}, "outside-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not destination.exists()
        assert not any(str(frame.get("method", "")).startswith("interaction.") for frame in frames)


async def test_auto_edit_writes_without_interruption_but_shell_still_requires_approval():
    """自动编辑模式只跳过 write_file；execute 仍必须由客户端明确拒绝或批准。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

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
        write_server = AgentHost(agent=write_agent)
        write_frames = await _capture_server(write_server)
        await write_server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "write", "run_id": "write-run"}, "write-start")
        )
        await _wait_for(write_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert (Path(workspace) / "auto.txt").read_text(encoding="utf-8") == "written"
        assert not any(str(frame.get("method", "")).startswith("interaction.") for frame in write_frames)

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
        shell_server = AgentHost(agent=shell_agent)
        shell_frames = await _capture_server(shell_server)
        await shell_server.dispatch(
            _request("run.start", {"message": "执行", "thread_id": "shell", "run_id": "shell-run"}, "shell-start")
        )
        interaction = await _wait_for(shell_frames, lambda frame: frame.get("method") == "interaction.approval")
        assert interaction["method"] == "interaction.approval"
        await shell_server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": interaction["id"],
                "result": {"decision": "reject"},
            }
        )
        await _wait_for(shell_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")


async def test_batch_tool_call_approval_restores_one_decision_per_hanging_call():
    """模型一轮发出多个需审批工具调用时，单个审批决定应复制到每个挂起调用。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "execute", "args": {"command": "echo a"}, "id": "call-1"},
                        {"name": "execute", "args": {"command": "echo b"}, "id": "call-2"},
                        {"name": "execute", "args": {"command": "echo c"}, "id": "call-3"},
                    ],
                ),
                AIMessage(content="已完成"),
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
        server = AgentHost(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "批量执行", "thread_id": "batch", "run_id": "batch-run"}, "batch-start")
        )
        interaction = await _wait_for(frames, lambda frame: frame.get("method") == "interaction.approval")
        assert interaction["method"] == "interaction.approval"
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": interaction["id"],
                "result": {"decision": "approve_once"},
            }
        )
        completed = await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert completed["params"]["payload"]["finish_reason"] == "completed"


async def test_plan_mode_returns_tool_message_without_writing_or_requesting_approval():
    """计划模式写工具调用必须由内核硬拒绝，不能先交给 TUI 或落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.host.agent_host import AgentHost

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
        server = AgentHost(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "plan", "run_id": "plan-run"}, "plan-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not (Path(workspace) / "plan.txt").exists()
        assert not any(str(frame.get("method", "")).startswith("interaction.") for frame in frames)


async def test_run_start_approval_mode_override_derives_mode_specific_profile(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """run.start 携带 approval_mode 时按模式派生独立 Profile，配置默认值保持不动。"""
    from harness_agent.host.agent_host import AgentHost

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
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAST_KEY", "fast-secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = AgentHost(config_home=home, workspace=workspace)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(
        _request("initialize", _initialize_params(capabilities=[]), "init-approval")
    )

    from harness_agent.host.run_coordinator import RunRuntime

    async def no_runtime(_run: Any) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    server._run_coordinator._runtime_provider = no_runtime

    def response(request_id: str) -> dict[str, Any]:
        return next(frame for frame in frames if frame.get("id") == request_id)

    await server.dispatch(
        _request(
            "run.start",
            {
                "message": "覆盖为 yolo",
                "thread_id": "thread-approval",
                "run_id": "override-run",
                "approval_mode": "yolo",
            },
            "start-override",
        )
    )
    assert response("start-override")["result"]["accepted"] is True
    # 等待首个 Run 结束，保证同一 Thread 的第二次 run.start 不会撞上 THREAD_BUSY。
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

    assert server._config is not None
    # Run 级覆盖不得改动配置默认模式。
    assert server._config.execution.approval_mode == "default"
    override_specs = list(server._resolved_agent_specs.values())
    assert [spec.effective_policy.approval_mode for spec in override_specs] == ["yolo"]

    await server.dispatch(
        _request(
            "run.start",
            {
                "message": "回到配置默认",
                "thread_id": "thread-approval",
                "run_id": "default-run",
            },
            "start-default",
        )
    )
    assert response("start-default")["result"]["accepted"] is True

    def both_runs_completed(frame: dict[str, Any]) -> bool:
        del frame
        return (
            sum(
                1
                for item in frames
                if item.get("params", {}).get("type") == "run.completed"
            )
            >= 2
        )

    await _wait_for(frames, both_runs_completed)

    specs = {spec.effective_policy.approval_mode: spec for spec in server._resolved_agent_specs.values()}
    # 两种模式必须派生出不同的 Profile key，引擎池据此各自缓存强制策略图。
    assert set(specs) == {"default", "yolo"}
    assert (
        specs["default"].runtime_profile.profile_key != specs["yolo"].runtime_profile.profile_key
    )
    await server._close_thread_persistence()


async def test_missing_interaction_capability_fails_closed_without_reverse_request():
    """无头客户端不声明交互能力时，服务端直接返回拒绝而不发送 request。"""
    from harness_agent.host.run_coordinator import ConnectionRef, InteractionRequest, RunRef
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True)
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
    result = await server._run_coordinator._interaction_port.request(
        ConnectionRef(server._owner_connection.connection_id),
        RunRef(thread_id="headless", run_id="headless-run"),
        InteractionRequest(
            request_id="approval-headless",
            type="approval",
            payload={},
            interrupt_id="approval-headless",
        ),
    )

    assert result.value == {"decision": "reject"}
    assert not any(str(frame.get("method", "")).startswith("interaction.") for frame in frames)


def test_tool_output_is_utf8_safely_truncated():
    """超限工具输出携带截断标记和原始字节数。"""
    from harness_agent.protocol.generated import MAX_TOOL_PAYLOAD_BYTES
    from harness_agent.host.run_coordinator import _truncate_text

    original = "界" * (MAX_TOOL_PAYLOAD_BYTES // 2)
    clipped, truncated, original_bytes = _truncate_text(original)
    assert truncated is True
    assert len(clipped.encode()) <= MAX_TOOL_PAYLOAD_BYTES
    assert original_bytes == len(original.encode())


async def test_stdio_subprocess_end_to_end_echo_mode():
    """真实 sidecar 完成 v3 initialize、run.start、event，并由 owner EOF 关闭。"""
    package_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory() as home:
        env = {
            **os.environ,
            "HOME": home,
            "PYTHONPATH": str(package_root),
            "HARNESS_ECHO_MODE": "1",
        }
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "harness_agent", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        assert process.stdin and process.stdout
        process.stdin.write((json.dumps(_request("initialize", _initialize_params(), "init")) + "\n" + json.dumps(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "start")) + "\n").encode())
        await process.stdin.drain()
        frames: list[dict[str, Any]] = []
        while not any(frame.get("params", {}).get("type") == "run.completed" for frame in frames):
            frames.append(json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=2)))
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=2)
        assert "content.delta" in _event_types(frames)


async def test_agent_host_closes_engines_before_shared_resource_owners(tmp_path: Path) -> None:
    """Host 先释放 AgentEngine lease，再按 owner 顺序关闭共享资源。"""
    from types import SimpleNamespace

    from harness_agent.server import AgentHost

    order: list[str] = []
    server = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)

    async def close_run_coordinator() -> None:
        order.append("run")

    async def close_engines() -> None:
        order.append("engine")

    async def close_mcp() -> None:
        order.append("mcp")

    async def close_workspace() -> None:
        order.append("workspace")

    async def close_provider() -> None:
        order.append("provider")

    async def close_persistence() -> None:
        order.append("persistence")

    server._run_coordinator.close = close_run_coordinator  # type: ignore[method-assign]
    server._close_agent_engine_pool = close_engines  # type: ignore[method-assign]
    server._mcp_manager = SimpleNamespace(close_all=close_mcp)
    server._workspace_execution_resources = SimpleNamespace(aclose=close_workspace)
    server._provider_client_pool = SimpleNamespace(aclose=close_provider)
    server._close_thread_persistence = close_persistence  # type: ignore[method-assign]

    await server.close()

    assert order == ["run", "engine", "mcp", "workspace", "provider", "persistence"]


async def test_mcp_status_no_config(tmp_path: Path):
    """未配置 MCP 服务器时 mcp.status 返回空列表和零工具数。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True, config_home=tmp_path / "home")
    frames = await _capture_server(server)
    await server.dispatch(_request("mcp.status", {}, "mcp-status"))
    assert frames[-1]["result"] == {"servers": [], "total_tools": 0}


async def test_mcp_status_not_initialized():
    """握手前调用 mcp.status 必须被结构化拒绝。"""
    from harness_agent.host.agent_host import AgentHost

    server = AgentHost(allow_echo=True)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request("mcp.status", {}, "mcp-early"))
    assert frames[0]["error"]["code"] == -32000


async def test_mcp_add_stdio(tmp_path: Path):
    """添加 stdio MCP 服务器后配置持久化且返回连接状态。"""
    from unittest.mock import AsyncMock, patch

    from harness_agent.host.agent_host import AgentHost

    config_home = tmp_path / "home"
    config_file = config_home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[config]\nversion = 1\n", encoding="utf-8")

    fake_status = {"name": "my-server", "transport": "stdio", "status": "connected", "tool_names": ["my-server_tool1"]}
    with (
        patch("harness_agent.host.agent_host.McpConnectionManager") as MockManager,
    ):
        mock_instance = MockManager.return_value
        mock_instance.connect_all = AsyncMock()
        mock_instance.apply_snapshot = AsyncMock(return_value=[fake_status])
        server = AgentHost(allow_echo=True, config_home=config_home)
        frames = await _capture_server(server)
        await server.dispatch(
            _request(
                "mcp.add",
                {"name": "my-server", "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
                "mcp-add-1",
            )
        )

    result = frames[-1]["result"]
    assert result["added"] is True
    assert result["connected"] is True
    assert result["tool_names"] == ["my-server_tool1"]
    assert result["error"] is None
    # 验证配置已写入文件
    assert config_file.exists()
    content = config_file.read_text(encoding="utf-8")
    assert "my-server" in content


async def test_mcp_add_duplicate(tmp_path: Path):
    """添加同名 MCP 服务器必须返回错误。"""
    from harness_agent.host.agent_host import AgentHost

    config_home = tmp_path / "home"
    config_file = config_home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        '[config]\nversion = 1\n\n[[mcp.servers]]\nname = "existing"\ntransport = "stdio"\ncommand = "echo"\n',
        encoding="utf-8",
    )
    server = AgentHost(allow_echo=True, config_home=config_home)
    frames = await _capture_server(server)

    await server.dispatch(
        _request("mcp.add", {"name": "existing", "transport": "stdio", "command": "echo"}, "mcp-add-dup")
    )

    assert frames[-1]["error"]["code"] == -32602
    assert frames[-1]["error"]["data"]["code"] == "MCP_SERVER_DUPLICATE"


async def test_mcp_add_invalid_name(tmp_path: Path):
    """非法服务器名称必须被拒绝。"""
    from harness_agent.host.agent_host import AgentHost

    config_home = tmp_path / "home"
    config_file = config_home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[config]\nversion = 1\n", encoding="utf-8")
    server = AgentHost(allow_echo=True, config_home=config_home)
    frames = await _capture_server(server)

    await server.dispatch(
        _request("mcp.add", {"name": "bad name!", "transport": "stdio", "command": "echo"}, "mcp-add-bad")
    )

    assert frames[-1]["error"]["code"] == -32602
    assert frames[-1]["error"]["data"]["code"] == "MCP_SERVER_NAME_INVALID"


async def test_mcp_remove_existing(tmp_path: Path):
    """删除已存在的 MCP 服务器后配置更新且返回成功。"""
    from harness_agent.host.agent_host import AgentHost

    config_home = tmp_path / "home"
    config_file = config_home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        '[config]\nversion = 1\n\n[[mcp.servers]]\nname = "to-remove"\ntransport = "stdio"\ncommand = "echo"\n',
        encoding="utf-8",
    )
    server = AgentHost(allow_echo=True, config_home=config_home)
    frames = await _capture_server(server)

    await server.dispatch(
        _request("mcp.remove", {"name": "to-remove"}, "mcp-remove-1")
    )

    result = frames[-1]["result"]
    assert result["removed"] is True
    # 验证配置文件中已无该服务器
    content = config_file.read_text(encoding="utf-8")
    assert "to-remove" not in content


async def test_mcp_remove_nonexistent(tmp_path: Path):
    """删除不存在的 MCP 服务器必须返回错误。"""
    from harness_agent.host.agent_host import AgentHost

    config_home = tmp_path / "home"
    config_file = config_home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[config]\nversion = 1\n", encoding="utf-8")
    server = AgentHost(allow_echo=True, config_home=config_home)
    frames = await _capture_server(server)

    await server.dispatch(
        _request("mcp.remove", {"name": "ghost"}, "mcp-remove-ghost")
    )

    assert frames[-1]["error"]["code"] == -32602
    assert frames[-1]["error"]["data"]["code"] == "MCP_SERVER_NOT_FOUND"


async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)


def _event_count(frames: list[dict[str, Any]], event_type: str) -> int:
    return sum(frame.get("params", {}).get("type") == event_type for frame in frames)
