"""审批响应协议扩展决策选项的验证测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from harness_agent.policy.permission_rules import PermissionRule
from harness_agent.protocol.generated import ApprovalResponse
from harness_agent.host.run_coordinator import (
    InteractionRequest,
    RunCoordinator,
    _generate_permission_rule,
    _resume_value,
)


def test_approval_response_accepts_approve_always() -> None:
    """ApprovalResponse 接受 approve_always 决策。"""
    resp = ApprovalResponse.model_validate({
        "decision": "approve_always",
    })
    assert resp.decision == "approve_always"


def test_approval_response_accepts_reject_with_feedback() -> None:
    """ApprovalResponse 接受 reject_with_feedback 且 feedback 非空。"""
    resp = ApprovalResponse.model_validate({
        "decision": "reject_with_feedback",
        "feedback": "不允许执行此操作",
    })
    assert resp.decision == "reject_with_feedback"
    assert resp.feedback == "不允许执行此操作"


def test_approval_response_rejects_invalid_decision() -> None:
    """无效 decision 值抛出 ValidationError。"""
    with pytest.raises(ValidationError):
        ApprovalResponse.model_validate({
            "decision": "invalid_decision",
        })


def test_resume_value_approve_always_maps_to_approve() -> None:
    """验证 _resume_value 中 approve_always 映射为 approve。"""
    spec = InteractionRequest(
        request_id="req-4",
        type="approval",
        payload={"interrupt_id": "int-1", "description": "test"},
        interrupt_id="int-1",
        action_count=1,
    )
    result = _resume_value(spec, {"decision": "approve_always"})
    assert result == {"int-1": {"decisions": [{"type": "approve"}]}}


def test_resume_value_reject_with_feedback_puts_message_in_decision_args() -> None:
    """reject_with_feedback 的反馈应写入每个 decision 的 args.message。"""
    spec = InteractionRequest(
        request_id="req-5",
        type="approval",
        payload={"interrupt_id": "int-2", "description": "test"},
        interrupt_id="int-2",
        action_count=2,
    )
    result = _resume_value(
        spec, {"decision": "reject_with_feedback", "feedback": "危险操作"}
    )
    assert result == {
        "int-2": {
            "decisions": [
                {"type": "reject", "args": {"message": "危险操作"}},
                {"type": "reject", "args": {"message": "危险操作"}},
            ]
        }
    }


def test_resume_value_reject_with_feedback_empty_feedback_is_plain_reject() -> None:
    """反馈为空时退化为普通 reject，不携带 args。"""
    spec = InteractionRequest(
        request_id="req-6",
        type="approval",
        payload={"interrupt_id": "int-3", "description": "test"},
        interrupt_id="int-3",
        action_count=1,
    )
    result = _resume_value(spec, {"decision": "reject_with_feedback", "feedback": ""})
    assert result == {"int-3": {"decisions": [{"type": "reject"}]}}


def test_generate_permission_rule_execute_uses_command_prefix() -> None:
    """execute 工具取命令首词生成前缀通配模式。"""
    rule = _generate_permission_rule("execute", {"command": "git commit -m 'x'"})
    assert rule == PermissionRule(tool="execute", resource="git *", effect="allow")


def test_generate_permission_rule_file_tools_use_project_wildcard() -> None:
    """文件写/删类工具使用项目级通配：批准后项目内同类操作不再反复弹窗。

    敏感路径（.git/.harness 等）仍由预检的 L3.5 安全检查强制弹窗，
    工作区边界由边界预检短路，因此通配不会放宽硬性保护。
    """
    for tool_name in ("write_file", "edit_file", "delete_file", "apply_patch"):
        rule = _generate_permission_rule(tool_name, {"file_path": "src/app/main.py"})
        assert rule == PermissionRule(tool=tool_name, resource="*", effect="allow")


def test_generate_permission_rule_other_tool_uses_wildcard() -> None:
    """其他工具的资源模式为通配符。"""
    rule = _generate_permission_rule("web_fetch", {"url": "https://example.com"})
    assert rule == PermissionRule(tool="web_fetch", resource="*", effect="allow")


def _approval_spec(action_requests: list[dict]) -> InteractionRequest:
    """构造携带 action_requests 工具上下文的审批交互请求。"""
    return InteractionRequest(
        request_id="req-7",
        type="approval",
        payload={
            "interrupt_id": "int-4",
            "description": "test",
            "requests": {"action_requests": action_requests},
        },
        interrupt_id="int-4",
        action_count=len(action_requests),
    )


def _coordinator(project_dir: Path | None = None) -> RunCoordinator:
    """构造只用于规则记录测试的最小 RunCoordinator。"""

    async def no_persistence() -> None:
        return None

    async def no_preparation(_command: object, _persistence: object) -> None:
        return None

    async def no_runtime(_run: object) -> None:
        return None

    return RunCoordinator(
        persistence_provider=no_persistence,
        preparation_provider=no_preparation,  # type: ignore[arg-type]
        runtime_provider=no_runtime,  # type: ignore[arg-type]
        interaction_port=object(),  # type: ignore[arg-type]
        skill_registry_provider=lambda: None,  # type: ignore[return-value]
        project_dir=project_dir,
    )


def test_approve_thread_stores_session_rule_in_memory() -> None:
    """approve_thread 生成规则并保存到会话内存列表，不写文件。"""
    coordinator = _coordinator()
    spec = _approval_spec([{"name": "execute", "args": {"command": "git status"}}])
    coordinator._record_approval_rules(spec, {"decision": "approve_thread"})
    assert coordinator.session_rules == [
        PermissionRule(tool="execute", resource="git *", effect="allow")
    ]


def test_approve_always_persists_rule_to_project_layer(tmp_path: Path) -> None:
    """approve_always 生成规则并通过 save_rule 持久化到 project 层。"""
    coordinator = _coordinator(project_dir=tmp_path)
    spec = _approval_spec([{"name": "execute", "args": {"command": "git status"}}])
    coordinator._record_approval_rules(spec, {"decision": "approve_always"})
    assert coordinator.session_rules == []
    saved = json.loads(
        (tmp_path / ".harness" / "settings.json").read_text(encoding="utf-8")
    )
    assert saved["permissions"] == [
        {
            "tool": "execute",
            "resource": "git *",
            "effect": "allow",
            "scope": "project",
        }
    ]


def test_other_decisions_do_not_record_rules() -> None:
    """approve_once 与 reject 类决策不产生权限规则。"""
    coordinator = _coordinator()
    spec = _approval_spec([{"name": "execute", "args": {"command": "git status"}}])
    for decision in ("approve_once", "reject", "reject_with_feedback"):
        coordinator._record_approval_rules(spec, {"decision": decision, "feedback": "x"})
    assert coordinator.session_rules == []


class TestDeleteFileThreadApprovalRegression:
    """回归：delete_file 选择本线程允许后，后续删除不得反复弹窗。

    历史缺陷：规则资源使用精确文件路径，删除其他文件或模型换用不同路径
    形态（虚拟路径/绝对路径）时 fnmatch 不命中，导致每次删除都重新审批。
    """

    @staticmethod
    def _preflight(coordinator: RunCoordinator, workspace: Path):
        from harness_agent.runtime.agent import _make_approval_preflight

        preflight = _make_approval_preflight(
            "default", None, lambda: coordinator.session_rules, str(workspace)
        )
        assert preflight is not None
        return preflight

    @staticmethod
    def _delete_request(file_path: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            tool_call={
                "name": "delete_file",
                "id": f"call-{file_path}",
                "args": {"file_path": file_path},
            }
        )

    def test_thread_approval_covers_later_deletes(self, tmp_path: Path) -> None:
        """本线程允许一次删除后，其他文件的删除也自动放行。"""
        coordinator = _coordinator()
        spec = _approval_spec(
            [{"name": "delete_file", "args": {"file_path": "/tmp/a.txt"}}]
        )
        coordinator._record_approval_rules(spec, {"decision": "approve_thread"})

        preflight = self._preflight(coordinator, tmp_path)
        # 同路径、其他文件、不同路径形态均不再弹窗
        assert preflight(self._delete_request("/tmp/a.txt")) is False
        assert preflight(self._delete_request("/tmp/other.txt")) is False
        assert preflight(self._delete_request(str(tmp_path / "src" / "b.txt"))) is False

    def test_thread_approval_still_asks_for_sensitive_delete(
        self, tmp_path: Path
    ) -> None:
        """allow 规则命中敏感路径删除时仍强制弹窗确认。"""
        coordinator = _coordinator()
        spec = _approval_spec(
            [{"name": "delete_file", "args": {"file_path": "/tmp/a.txt"}}]
        )
        coordinator._record_approval_rules(spec, {"decision": "approve_thread"})

        preflight = self._preflight(coordinator, tmp_path)
        assert preflight(self._delete_request("/.git/index")) is True
        assert preflight(self._delete_request("/.harness/settings.json")) is True
