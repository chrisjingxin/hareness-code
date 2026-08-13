"""Compose 生产 seam 回归：服务组装、stage spec 解析与 Verify 审批接线。

覆盖 HC-140 真实模型 E2E 暴露的两个接线缺陷：
1. ``_provide_compose_services`` 曾以 ``run.agent_engine_profile_key`` 组装
   profile key；该字段在 adapter 组装时尚未由 runtime 获取填充，导致
   ``_resolve_compose_stage_spec("")`` 返回 None，全部 stage 驱动失败。
2. ``_WorkItemVerificationPort`` 曾不传 ``approve`` 回调，非白名单验证命令
   （如 pytest）直接 ``APPROVAL_REQUIRED`` 失败且无审批通道。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params() -> dict[str, Any]:
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": ["threads.read"], "handles": ["approval", "question"]},
    }


def _write_model_config(home: Path) -> None:
    """最小可解析模型配置；base_url 使用本地端口避免外部网络。"""
    config_dir = home / ".harness"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '''[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
provider = "openai-compatible"
provider_label = "fast"
model = "fake-model"
base_url = "http://127.0.0.1:9/v1"
api_key = "fake-key"
context_window_tokens = 32000

[approval]
mode = "auto-edit"
''',
        encoding="utf-8",
    )


class _StubRuntime:
    """避免真实模型调用；Compose 阶段执行前测试即已完成断言。"""

    @staticmethod
    async def release() -> None:
        return None


async def test_compose_services_resolve_stage_spec_from_prepared_profile(
    tmp_path: Path,
) -> None:
    """组装后的 services.profile_key 必须能解析出可信 stage spec。

    RED 依据：此前 profile_key 为空字符串，``_resolve_compose_stage_spec("")``
    返回 None，Task/Spec/Plan 任一 gate 驱动在第一次 stage 调用时抛
    ``COMPOSE_STAGE_SPEC_MISSING``，turn 静默收敛为 waiting_user。
    """
    from harness_agent.host.agent_host import AgentHost
    from harness_agent.host.run_coordinator import RunRuntime

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _write_model_config(home)
    server = AgentHost(config_home=home, workspace=workspace)
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture  # type: ignore[method-assign]
    await server.dispatch(_request("initialize", _initialize_params(), "init"))

    # 仅 stub runtime 获取，保留 prepare 阶段的 spec/profile 解析事实。
    async def no_runtime(_run: Any) -> RunRuntime:
        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=_StubRuntime.release,
        )

    server._run_coordinator._runtime_provider = no_runtime
    thread_id = "compose-services-thread"
    await server.dispatch(
        _request(
            "run.start",
            {
                "mode": "compose",
                "message": "实现站内搜索",
                "thread_id": thread_id,
                "run_id": "run-services",
            },
            "start-services",
        )
    )
    run = server._run_coordinator._runs.get(thread_id)
    assert run is not None
    try:
        services = await server._provide_compose_services(run)
        assert services is not None
        assert services.profile_key, "组装后的 profile_key 不能为空"
        assert services.cancellation_token is not None, "cancellation_token 必须随 Run 注入"
        spec = server._resolve_compose_stage_spec(services.profile_key)
        assert spec is not None, "profile_key 必须能解析出内置 stage spec"
    finally:
        await server._run_coordinator._cancel_runs(lambda candidate: True)
        await asyncio.sleep(0.05)
        await server._close_thread_persistence()


async def test_work_item_verification_port_wires_approval_channel() -> None:
    """非白名单验证命令必须经 RunLifecyclePort.request_approval 审批。

    RED 依据：此前 ``_WorkItemVerificationPort`` 不传 approve，命令在无规则
    工作区直接 ``APPROVAL_REQUIRED``，生产链路无法完成 Verify Activity。
    """
    from harness_agent.host.run_execution import _WorkItemVerificationPort

    calls: list[dict[str, Any]] = []

    class _FakeVerification:
        async def run(self, request: Any) -> Any:
            approved = await request.approve("需要审批")
            assert approved is True
            return _FakeEvidence()

    class _FakeEvidence:
        exit_code = 0
        output_digest = "d" * 64
        finished_at_ms = 1_700_000_000_000

    class _FakeRun:
        root_execution_ref = None  # 仅用于 approval scope；测试不依赖

    class _FakePort:
        async def request_approval(
            self,
            run: Any,
            *,
            request_id: str,
            interrupt_id: str,
            description: str,
            decisions: list[str],
            action_requests: list[dict[str, object]],
            execution_id: str | None = None,
            parent_execution_id: str | None = None,
            agent_id: str | None = None,
            compose_scope: Any = None,
        ) -> Any:
            calls.append(
                {
                    "description": description,
                    "decisions": decisions,
                    "action_requests": action_requests,
                }
            )
            return _FakeResult()

    class _FakeResult:
        value: Any = {"decision": "approve_once"}

    port = _WorkItemVerificationPort(
        _FakeVerification(), run=_FakeRun(), port=_FakePort()
    )
    result = await port.run_command("python -m pytest -q", work_item_id="wi-1")
    assert result.exit_code == 0
    assert result.output_digest == "d" * 64
    assert calls, "验证命令需要审批时必须走审批通道"
    assert calls[0]["decisions"] == ["approve_once", "reject"]
    assert calls[0]["action_requests"][0]["args"]["command"] == "python -m pytest -q"
