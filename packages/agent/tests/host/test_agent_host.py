"""Project-scoped AgentHost 的多 Connection、owner 与 attachment 回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from harness_agent.host.agent_host import AgentHost


class _BlockingAgent:
    """让协议测试通过 public run.start 保持一个可观察的 active Run。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def astream(self, *_args: Any, **_kwargs: Any):
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield None


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize(*requests: str) -> dict[str, Any]:
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "1", "kind": "test"},
        "capabilities": {"requests": list(requests), "handles": []},
    }


async def test_run_owner_and_observer_receive_identical_events(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("run.cancel"), "web-init"),
    )
    host._owner_connection.watched_threads.add("thread-1")
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "hello", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    for _ in range(100):
        if any(frame.get("params", {}).get("type") == "run.completed" for frame in attached_frames):
            break
        await asyncio.sleep(0.01)

    owner_events = [frame["params"] for frame in owner_frames if frame.get("method") == "event"]
    attached_events = [frame["params"] for frame in attached_frames if frame.get("method") == "event"]
    assert owner_events == attached_events
    assert [event["sequence"] for event in owner_events] == [1, 2, 3]
    await host.close()


async def test_only_run_owner_can_cancel(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("run.cancel"), "web-init"),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch(
        _request(
            "run.cancel",
            {"thread_id": "thread-1", "run_id": "run-1"},
            "cancel",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "RUN_NOT_OWNER"
    await host.close()


async def test_run_id_retry_is_idempotent_and_conflicting_content_is_rejected(
    tmp_path: Path,
) -> None:
    """活动 Run 的相同请求可重试，复用 ID 的不同内容稳定冲突。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize(), "owner-init"))
    await host.dispatch(
        _request(
            "run.start",
            {"message": "same", "thread_id": "thread-1", "run_id": "run-1"},
            "retry",
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await host.dispatch(
        _request(
            "run.start",
            {"message": "same", "thread_id": "thread-1", "run_id": "run-1"},
            "retry",
        )
    )
    assert owner_frames[-1]["result"] == {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "accepted": True,
    }

    await host.dispatch(
        _request(
            "run.start",
            {"message": "different", "thread_id": "thread-1", "run_id": "run-1"},
            "conflict",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "RUN_ID_CONFLICT"
    await host.close()


async def test_watch_rejects_active_thread_and_attached_disconnect_cancels_only_owned_run(
    tmp_path: Path,
) -> None:
    """active Thread 不允许新增 watch，attached EOF 只取消自己的 Run。"""
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request("initialize", _initialize("threads.read"), "owner-init")
    )

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("threads.read"), "web-init"),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch(
        _request("threads.watch", {"thread_id": "thread-1"}, "watch")
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "THREAD_BUSY"

    await host.close_connection(attached)
    assert host._owner_connection.closed is False
    await host.dispatch(
        _request(
            "run.start",
            {"message": "again", "thread_id": "thread-1", "run_id": "run-2"},
            "owner-start",
        )
    )
    assert owner_frames[-1]["result"]["accepted"] is True
    await host.close()


async def test_attachment_token_is_origin_bound_single_use_and_capability_limited(
    tmp_path: Path,
) -> None:
    owner_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43210"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin="http://127.0.0.1:1", proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.attach", "run.cancel"),
                    "web-init",
                )
            )
        )
        initialized = json.loads(await socket.recv())["result"]
        assert initialized["connection"]["role"] == "attached"
        assert initialized["capabilities"]["enabled"] == ["run.cancel"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()
    await host.close()


class TestBuildApprovalClassifier:
    """AUTO 模式分类器装配：profile 解析失败时优雅降级而不是崩溃。"""

    @staticmethod
    def _fake_host(config: Any) -> Any:
        """构造只带 _config 属性的伪 host，供未绑定方法调用。"""
        from types import SimpleNamespace

        return SimpleNamespace(_config=config)

    @staticmethod
    def _model_settings(api_key: str | None) -> Any:
        """构造分类器 profile 用的模型设置；使用独立环境变量避免串用。"""
        from harness_agent.config.config import ModelSettings

        return ModelSettings(
            name="small-fast",
            base_url="https://gateway.example.internal/v1",
            api_key_env="HARNESS_CLASSIFIER_TEST_KEY",
            api_key=api_key,
            timeout_seconds=120.0,
        )

    def test_returns_classifier_when_profile_available(self, monkeypatch: pytest.MonkeyPatch):
        """profile 存在且密钥可用时返回 SafetyClassifier，并使用收紧的超时。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ModelProfile
        from harness_agent.policy.classifier import SafetyClassifier

        monkeypatch.delenv("HARNESS_CLASSIFIER_TEST_KEY", raising=False)
        settings = self._model_settings(api_key="test-key")
        profile = ModelProfile(
            profile_id="small-fast", settings=settings, is_default=False, source="test"
        )
        config = SimpleNamespace(
            model_catalog=SimpleNamespace(require_profile=lambda _id: profile)
        )

        classifier = AgentHost._build_approval_classifier(self._fake_host(config), "small-fast")

        assert isinstance(classifier, SafetyClassifier)

    def test_missing_profile_degrades_to_none(self):
        """profile 不存在时返回 None（回退人工审批），不抛异常。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ConfigError

        def require_profile(_id: str) -> Any:
            raise ConfigError("MODEL_PROFILE_NOT_FOUND: nope")

        config = SimpleNamespace(model_catalog=SimpleNamespace(require_profile=require_profile))

        assert AgentHost._build_approval_classifier(self._fake_host(config), "nope") is None

    def test_missing_api_key_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch):
        """profile 无可用密钥时返回 None（回退人工审批），不抛异常。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ModelProfile

        monkeypatch.delenv("HARNESS_CLASSIFIER_TEST_KEY", raising=False)
        profile = ModelProfile(
            profile_id="small-fast",
            settings=self._model_settings(api_key=None),
            is_default=False,
            source="test",
        )
        config = SimpleNamespace(
            model_catalog=SimpleNamespace(require_profile=lambda _id: profile)
        )

        assert (
            AgentHost._build_approval_classifier(self._fake_host(config), "small-fast") is None
        )

    def test_missing_model_catalog_degrades_to_none(self):
        """配置未加载模型目录时返回 None，不阻断引擎构建。"""
        assert AgentHost._build_approval_classifier(self._fake_host(None), "small-fast") is None


async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)
