"""审批响应协议扩展决策选项的验证测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness_agent.protocol_generated import ApprovalResponse
from harness_agent.server import InteractionSpec, JsonRpcServer


def test_approval_response_accepts_approve_always() -> None:
    """ApprovalResponse 接受 approve_always 决策。"""
    resp = ApprovalResponse.model_validate({
        "type": "approval",
        "request_id": "req-1",
        "decision": "approve_always",
    })
    assert resp.decision == "approve_always"


def test_approval_response_accepts_reject_with_feedback() -> None:
    """ApprovalResponse 接受 reject_with_feedback 且 feedback 非空。"""
    resp = ApprovalResponse.model_validate({
        "type": "approval",
        "request_id": "req-2",
        "decision": "reject_with_feedback",
        "feedback": "不允许执行此操作",
    })
    assert resp.decision == "reject_with_feedback"
    assert resp.feedback == "不允许执行此操作"


def test_approval_response_rejects_invalid_decision() -> None:
    """无效 decision 值抛出 ValidationError。"""
    with pytest.raises(ValidationError):
        ApprovalResponse.model_validate({
            "type": "approval",
            "request_id": "req-3",
            "decision": "invalid_decision",
        })


def test_resume_value_approve_always_maps_to_approve() -> None:
    """验证 _resume_value 中 approve_always 映射为 approve。"""
    server = object.__new__(JsonRpcServer)
    spec = InteractionSpec(
        request_id="req-4",
        type="approval",
        payload={"interrupt_id": "int-1", "description": "test"},
        interrupt_id="int-1",
        action_count=1,
    )
    result = server._resume_value(spec, {"decision": "approve_always"})
    assert result == {"int-1": {"decisions": [{"type": "approve"}]}}
