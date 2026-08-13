"""审批响应协议扩展决策选项的验证测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from harness_agent.policy.permission_rules import PermissionRule
from harness_agent.protocol.generated import ApprovalResponse
from harness_agent.protocol.runtime import validate_interaction_params
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    RunCoordinator,
    RunPreparation,
    RunState,
    StartRun,
    _generate_permission_rule,
)
from harness_agent.host.run_execution import _extract_interaction, _resume_value
from harness_agent.runtime.interactions import InteractionRequest, InteractionResult


def _assert_approval_params_schema_compliant(spec: InteractionRequest) -> None:
    """审批反向请求参数必须通过协议 schema 校验。

    回归护栏：approvalRequest.payload 为 additionalProperties: false，
    任何附加字段都会让客户端校验失败、整次审批静默降级为 reject（无弹窗）。
    """
    validate_interaction_params(
        "interaction.approval",
        {
            "thread_id": "thread-x",
            "run_id": "run-x",
            "timeout_ms": 1000,
            "payload": dict(spec.payload),
        },
    )


def test_approval_response_accepts_approve_project() -> None:
    """ApprovalResponse 接受 approve_project 决策。"""
    resp = ApprovalResponse.model_validate({
        "decision": "approve_project",
    })
    assert resp.decision == "approve_project"


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


def test_approval_file_diff_presentation_rejects_unknown_fields_and_oversize_text() -> None:
    """file_diff tagged shape 严格校验额外字段与 Schema 字符上限。"""
    payload = {
        "interrupt_id": "int-diff",
        "description": "文件变更需要审批",
        "requests": None,
        "decisions": ["approve_once", "reject"],
        "presentation": {
            "kind": "file_diff",
            "operation": "edit",
            "path": "/src/a.py",
            "added_lines": 1,
            "removed_lines": 1,
            "truncated": False,
            "unified_diff": "+new",
            "unexpected": True,
        },
    }
    with pytest.raises(ValidationError):
        validate_interaction_params(
            "interaction.approval",
            {"thread_id": "thread", "run_id": "run", "timeout_ms": 1000, "payload": payload},
        )

    payload["presentation"].pop("unexpected")
    payload["presentation"]["unified_diff"] = "x" * 16_385
    with pytest.raises(ValidationError):
        validate_interaction_params(
            "interaction.approval",
            {"thread_id": "thread", "run_id": "run", "timeout_ms": 1000, "payload": payload},
        )


def test_resume_value_question_maps_answers() -> None:
    """提问类交互的回复按问题序号映射为 answers。"""
    spec = InteractionRequest(
        request_id="req-q",
        type="question",
        payload={"interrupt_id": "int-q"},
        interrupt_id="int-q",
        questions=({"question": "选哪个", "choices": []},),
    )
    result = _resume_value(spec, {"answers": {"question-1": ["选项A"]}})
    assert result == {"int-q": {"status": "answered", "answers": ["选项A"]}}


class _ScriptedInteractionPort:
    """按脚本顺序返回审批结果的测试用交互端口。"""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        """保存按请求顺序消费的响应脚本。"""
        self._responses = list(responses)
        self.requests: list[InteractionRequest] = []

    async def request(
        self, owner: object, ref: object, spec: InteractionRequest
    ) -> InteractionResult:
        """记录请求并返回下一个脚本响应，脚本耗尽时默认拒绝。"""
        self.requests.append(spec)
        value = self._responses.pop(0) if self._responses else {"decision": "reject"}
        return InteractionResult(value=value)


def _serial_spec(
    actions: list[dict[str, object]],
    safe_indices: list[int],
    unsafe_indices: list[int],
    interrupt_id: str = "int-serial",
) -> InteractionRequest:
    """构造与 _extract_interaction 输出同形的串行审批首批请求。

    串行元数据经 serial_context 传递，wire payload 只含 schema 四字段。
    """
    return InteractionRequest(
        request_id="req-serial",
        type="approval",
        payload={
            "interrupt_id": interrupt_id,
            "description": "test",
            "requests": {"action_requests": []},
            "decisions": [
                "approve_once",
                "approve_thread",
                "approve_project",
                "reject",
                "reject_with_feedback",
            ],
        },
        interrupt_id=interrupt_id,
        action_count=1,
        serial_context={
            "all_action_requests": actions,
            "safe_indices": safe_indices,
            "unsafe_indices": unsafe_indices,
        },
    )


def _serial_run() -> RunState:
    """构造串行审批测试用的最小 RunState。"""
    return RunState(
        start=StartRun(mode="build", thread_id="thread-serial", run_id="run-serial", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )


class TestSerialApprovals:
    """多工具逐个串行审批：单次 interrupt，本地收集决策后一次性 resume。"""

    @staticmethod
    def _collect(coordinator: RunCoordinator, run: RunState, spec: InteractionRequest):
        return asyncio.run(coordinator._collect_serial_approvals(run, spec))

    def test_all_approved_in_order(self) -> None:
        """两个 unsafe 工具逐个弹窗，decisions 按原始顺序全部 approve。"""
        port = _ScriptedInteractionPort(
            [{"decision": "approve_once"}, {"decision": "approve_once"}]
        )
        coordinator = _coordinator_with_port(port)
        actions = [
            {"name": "execute", "args": {"command": "npm build"}},
            {"name": "read_file", "args": {"file_path": "a.txt"}},
            {"name": "execute", "args": {"command": "npm test"}},
        ]
        run = _serial_run()
        result = self._collect(
            coordinator, run, _serial_spec(actions, [1], [0, 2])
        )
        assert result == {
            "int-serial": {
                "decisions": [
                    {"type": "approve"},
                    {"type": "approve"},
                    {"type": "approve"},
                ]
            }
        }
        # safe 工具不弹窗，只弹了两次；序号并入 description 展示
        assert [r.payload["description"] for r in port.requests] == [
            "（第 1/2 个待审批操作）A tool execution requires approval",
            "（第 2/2 个待审批操作）A tool execution requires approval",
        ]
        # 每个弹窗的 wire payload 都必须通过协议 schema 校验
        for spec in port.requests:
            _assert_approval_params_schema_compliant(spec)
        assert run.batch_rejected is False
        assert run.pending_approvals == []

    def test_file_approval_includes_registered_structured_presentation(self) -> None:
        """Coordinator 只附带当前 Run 按相同工具参数登记的有界展示。"""
        port = _ScriptedInteractionPort([{"decision": "approve_once"}])
        coordinator = _coordinator_with_port(port)
        action = {
            "name": "edit_file",
            "args": {"file_path": "/src/a.py", "old_string": "x", "new_string": "y"},
            "description": "文件变更需要审批",
        }
        run = _serial_run()
        presentation = {
            "kind": "file_diff",
            "operation": "edit",
            "path": "/src/a.py",
            "added_lines": 1,
            "removed_lines": 1,
            "truncated": False,
            "unified_diff": "--- /src/a.py\n+++ /src/a.py\n@@ -1 +1 @@\n-x\n+y",
        }
        assert run.approval_presentations.remember(
            "edit_file",
            action["args"],
            presentation,
        )

        result = self._collect(
            coordinator,
            run,
            _serial_spec([action], [], [0]),
        )

        assert result["int-serial"]["decisions"] == [{"type": "approve"}]
        assert port.requests[0].payload["presentation"] == presentation
        _assert_approval_params_schema_compliant(port.requests[0])

    def test_file_approval_does_not_reuse_presentation_for_changed_args(self) -> None:
        """参数指纹不一致时展示缺失并回退通用审批，不串用旧 diff。"""
        port = _ScriptedInteractionPort([{"decision": "reject"}])
        coordinator = _coordinator_with_port(port)
        run = _serial_run()
        assert run.approval_presentations.remember(
            "edit_file",
            {"file_path": "/src/a.py", "new_string": "approved"},
            {
                "kind": "file_diff",
                "operation": "edit",
                "path": "/src/a.py",
                "added_lines": 1,
                "removed_lines": 1,
                "truncated": False,
                "unified_diff": "+approved",
            },
        )
        action = {
            "name": "edit_file",
            "args": {"file_path": "/src/a.py", "new_string": "changed"},
        }

        self._collect(coordinator, run, _serial_spec([action], [], [0]))

        assert "presentation" not in port.requests[0].payload

    def test_user_reject_cancels_remaining_batch(self) -> None:
        """UserReject 终止同批：剩余工具不再弹窗并收到取消原因。"""
        port = _ScriptedInteractionPort(
            [{"decision": "approve_once"}, {"decision": "reject"}]
        )
        coordinator = _coordinator_with_port(port)
        actions = [
            {"name": "execute", "args": {"command": "npm build"}},
            {"name": "execute", "args": {"command": "npm test"}},
            {"name": "execute", "args": {"command": "npm publish"}},
        ]
        run = _serial_run()
        result = self._collect(coordinator, run, _serial_spec(actions, [], [0, 1, 2]))
        decisions = result["int-serial"]["decisions"]
        assert decisions[0] == {"type": "approve"}
        assert decisions[1] == {"type": "reject"}
        assert decisions[2] == {
            "type": "reject",
            "message": "cancelled due to earlier permission rejection",
        }
        # 第三个工具没有弹窗
        assert len(port.requests) == 2
        assert run.batch_rejected is True

    def test_reject_with_feedback_carries_message(self) -> None:
        """reject_with_feedback 的反馈写入 LangChain RejectDecision.message。"""
        port = _ScriptedInteractionPort(
            [{"decision": "reject_with_feedback", "feedback": "危险操作"}]
        )
        coordinator = _coordinator_with_port(port)
        actions = [{"name": "execute", "args": {"command": "rm -rf /"}}]
        result = self._collect(
            coordinator, _serial_run(), _serial_spec(actions, [], [0])
        )
        assert result["int-serial"]["decisions"] == [
            {"type": "reject", "message": "危险操作"}
        ]

    def test_approve_thread_auto_approves_matching_queued_tool(self) -> None:
        """approve_thread 生成的规则立即自动放行同批匹配的后续工具。"""
        port = _ScriptedInteractionPort([{"decision": "approve_thread"}])
        coordinator = _coordinator_with_port(port)
        actions = [
            {"name": "execute", "args": {"command": "git status"}},
            {"name": "execute", "args": {"command": "git status --short"}},
        ]
        run = _serial_run()
        result = self._collect(coordinator, run, _serial_spec(actions, [], [0, 1]))
        assert result["int-serial"]["decisions"] == [
            {"type": "approve"},
            {"type": "approve"},
        ]
        # 第二个工具命中会话规则，未弹窗
        assert len(port.requests) == 1

    def test_policy_deny_continues_with_next_tool(self) -> None:
        """deny 规则命中的排队工具按 PolicyDeny 拒绝，但不终止同批后续工具。"""
        port = _ScriptedInteractionPort([{"decision": "approve_once"}])
        coordinator = _coordinator_with_port(port)
        coordinator._session_rules.append(
            PermissionRule(tool="execute", resource="rm *", effect="deny")
        )
        actions = [
            {"name": "execute", "args": {"command": "rm -rf build"}},
            {"name": "execute", "args": {"command": "npm test"}},
        ]
        run = _serial_run()
        result = self._collect(coordinator, run, _serial_spec(actions, [], [0, 1]))
        decisions = result["int-serial"]["decisions"]
        assert decisions[0] == {
            "type": "reject",
            "message": "denied by policy rule",
        }
        assert decisions[1] == {"type": "approve"}
        # deny 不弹窗，只有第二个工具弹了窗
        assert len(port.requests) == 1
        assert run.batch_rejected is False

    def test_sensitive_path_not_auto_approved_by_queued_rule(self, tmp_path: Path) -> None:
        """敏感路径即使命中排队 allow 规则也必须弹窗确认。"""
        port = _ScriptedInteractionPort(
            [{"decision": "approve_thread"}, {"decision": "approve_once"}]
        )
        coordinator = _coordinator_with_port(port, project_dir=tmp_path)
        actions = [
            {"name": "edit_file", "args": {"file_path": "src/a.txt"}},
            {"name": "edit_file", "args": {"file_path": "src/.git/config"}},
        ]
        result = self._collect(
            coordinator, _serial_run(), _serial_spec(actions, [], [0, 1])
        )
        assert result["int-serial"]["decisions"] == [
            {"type": "approve"},
            {"type": "approve"},
        ]
        # 第二个工具命中 src/** 但属敏感路径，仍需弹窗
        assert len(port.requests) == 2

    def test_directory_trust_session_grant_skips_second_prompt_same_batch(
        self, tmp_path: Path
    ) -> None:
        """首个调用选本会话信任后，同批同目录后续调用自动放行且选项首选 session。"""
        from harness_agent.policy.workspace_roots import WorkspaceRootRegistry

        outside = tmp_path.parent / f"zc142-batch-{tmp_path.name}"
        outside.mkdir(exist_ok=True)
        registry = WorkspaceRootRegistry(tmp_path, project_dir=tmp_path, load_persisted=False)
        port = _ScriptedInteractionPort([{"decision": "allow_session"}])
        coordinator = _coordinator_with_port(port, project_dir=tmp_path)
        coordinator._workspace_root_registry = registry
        actions = [
            {"name": "ls", "args": {"path": str(outside)}},
            {"name": "read_file", "args": {"file_path": str(outside / "a.txt")}},
        ]
        run = _serial_run()
        assert run.approval_presentations.remember(
            "ls",
            actions[0]["args"],
            {
                "kind": "directory_trust",
                "directory": str(outside),
                "target_path": str(outside),
                "tool_name": "ls",
                "access": "read",
                "shadows_workspace": False,
            },
        )
        assert run.approval_presentations.remember(
            "read_file",
            actions[1]["args"],
            {
                "kind": "directory_trust",
                "directory": str(outside),
                "target_path": str(outside / "a.txt"),
                "tool_name": "read_file",
                "access": "read",
                "shadows_workspace": False,
            },
        )

        result = self._collect(coordinator, run, _serial_spec(actions, [], [0, 1]))

        assert result["int-serial"]["decisions"] == [
            {"type": "approve"},
            {"type": "approve"},
        ]
        # 第二个调用因目录已信任而免弹；只弹了一次且仅允许/拒绝两个选项
        assert len(port.requests) == 1
        assert port.requests[0].type == "directory_trust"
        assert port.requests[0].payload["decisions"] == ["allow_session", "deny"]
        assert run.batch_rejected is False


class _FakeInterrupt:
    """模拟 LangGraph interrupt 对象，携带 value 与 id。"""

    def __init__(self, value: object, interrupt_id: str = "int-schema") -> None:
        self.value = value
        self.id = interrupt_id


def test_extract_interaction_approval_payload_passes_schema() -> None:
    """interrupt 提取出的审批请求 payload 必须通过协议 schema 校验。

    回归：历史实现把 all_action_requests/safe_indices 等串行元数据塞进
    payload，客户端 additionalProperties: false 校验失败后整次审批静默
    降级为 reject，用户看不到任何弹窗。
    """
    spec, auto_resume = _extract_interaction((
        "updates",
        {
            "__interrupt__": [
                _FakeInterrupt({
                    "action_requests": [
                        {
                            "name": "execute",
                            "args": {"command": "npm test"},
                            "description": "run tests",
                        },
                        {"name": "read_file", "args": {"file_path": "a.txt"}},
                    ]
                })
            ]
        },
    ))
    assert auto_resume is None
    assert spec is not None
    _assert_approval_params_schema_compliant(spec)
    # 串行元数据只走 serial_context，不进入 wire payload
    assert "all_action_requests" not in spec.payload
    assert "safe_indices" not in spec.payload
    assert "unsafe_indices" not in spec.payload
    assert spec.serial_context is not None
    assert spec.serial_context["unsafe_indices"] == [0]
    assert spec.serial_context["safe_indices"] == [1]


def test_extract_interaction_all_safe_tools_auto_resume() -> None:
    """全部并发安全工具直接自动放行，不产生审批交互。"""
    spec, auto_resume = _extract_interaction((
        "updates",
        {
            "__interrupt__": [
                _FakeInterrupt({
                    "action_requests": [
                        {"name": "read_file", "args": {"file_path": "a.txt"}},
                        {"name": "glob", "args": {"pattern": "*.py"}},
                    ]
                })
            ]
        },
    ))
    assert spec is None
    assert auto_resume == {
        "int-schema": {"decisions": [{"type": "approve"}, {"type": "approve"}]}
    }


def test_extract_interaction_directory_trust_not_auto_resumed() -> None:
    """需要目录信任的只读工具不得按并发安全自动放行。

    回归：ZC-142 把只读工具纳入 HITL 后，它们进入 interrupt 的唯一原因是
    需要目录信任决策；此前 _CONCURRENCY_SAFE_TOOLS 会将其自动 approve，
    信任卡片被静默跳过且信任不会注册，执行层只能硬拒绝，用户看不到弹窗。
    """
    spec, auto_resume = _extract_interaction(
        (
            "updates",
            {
                "__interrupt__": [
                    _FakeInterrupt({
                        "action_requests": [
                            {"name": "ls", "args": {"path": "C:/Users/PC/Desktop/x"}},
                            {"name": "glob", "args": {"pattern": "*.py"}},
                        ]
                    })
                ]
            },
        ),
        needs_user_decision=lambda name, args: name == "ls",
    )
    assert auto_resume is None
    assert spec is not None
    assert spec.serial_context is not None
    assert spec.serial_context["unsafe_indices"] == [0]
    assert spec.serial_context["safe_indices"] == [1]
    _assert_approval_params_schema_compliant(spec)


def test_generate_permission_rule_execute_uses_command_prefix() -> None:
    """execute 工具按词分类生成纯前缀规则。"""
    rules = _generate_permission_rule("execute", {"command": "git commit -m 'x'"})
    assert rules == [
        PermissionRule(tool="execute", resource="git commit", effect="allow")
    ]


def test_generate_permission_rule_file_tools_use_project_wildcard() -> None:
    """文件写/编辑/删除工具按规范生成项目级通配规则。"""
    for tool_name in ("write_file", "edit_file", "delete_file"):
        rules = _generate_permission_rule(tool_name, {"file_path": "src/app/main.py"})
        assert rules == [PermissionRule(tool=tool_name, resource="*", effect="allow")]


def test_generate_permission_rule_web_fetch_uses_domain_extraction() -> None:
    """web_fetch 按 URL 域名生成规则。"""
    rules = _generate_permission_rule("web_fetch", {"url": "https://example.com"})
    assert rules == [
        PermissionRule(tool="web_fetch", resource="domain:example.com", effect="allow")
    ]


def test_generate_permission_rule_chained_command_produces_per_segment_rules() -> None:
    """链式命令逐段生成规则，且去重。"""
    rules = _generate_permission_rule(
        "execute",
        {"command": 'echo hello > a.txt && dir /b "C:\\tmp"'},
    )
    resources = [r.resource for r in rules]
    assert "echo hello" in resources
    assert "dir" in resources
    assert len(resources) == len(set(resources))


def test_generate_permission_rule_rm_produces_no_bare_root() -> None:
    """rm foo.txt 命中裸根禁令，不产生规则。"""
    rules = _generate_permission_rule("execute", {"command": "rm foo.txt"})
    assert rules == []


def _coordinator(
    project_dir: Path | None = None,
    *,
    workspace_root_registry: object | None = None,
) -> RunCoordinator:
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
        project_dir=project_dir,
        workspace_root_registry=workspace_root_registry,
    )


def _coordinator_with_port(
    port: object, project_dir: Path | None = None
) -> RunCoordinator:
    """构造携带脚本化交互端口的 RunCoordinator，用于串行审批测试。"""
    coordinator = _coordinator(project_dir=project_dir)
    coordinator._interaction_port = port  # type: ignore[assignment]
    return coordinator


def test_approve_thread_stores_session_rule_in_memory() -> None:
    """approve_thread 生成规则并保存到会话内存列表，不写文件。"""
    coordinator = _coordinator()
    coordinator._record_approval_rule(
        "execute", {"command": "git status"}, "approve_thread"
    )
    assert coordinator.session_rules == [
        PermissionRule(tool="execute", resource="git status", effect="allow")
    ]


def test_approve_project_persists_rule_to_project_layer(tmp_path: Path) -> None:
    """approve_project 生成规则并通过 save_rule 持久化到 project 层。"""
    coordinator = _coordinator(project_dir=tmp_path)
    coordinator._record_approval_rule(
        "execute", {"command": "git status"}, "approve_project"
    )
    assert coordinator.session_rules == []
    saved = json.loads(
        (tmp_path / ".harness" / "settings.json").read_text(encoding="utf-8")
    )
    assert saved["permissions"] == ["Bash(git status)"]


def test_other_decisions_do_not_record_rules() -> None:
    """approve_once 与 reject 类决策不产生权限规则。"""
    coordinator = _coordinator()
    for decision in ("approve_once", "reject", "reject_with_feedback"):
        coordinator._record_approval_rule(
            "execute", {"command": "git status"}, decision
        )
    assert coordinator.session_rules == []


def test_directory_trust_allow_session_registers_session_root(tmp_path: Path) -> None:
    """directory_trust + allow_session 注册会话额外根，不写 PermissionRule。"""
    from harness_agent.policy.workspace_roots import WorkspaceRootRegistry, normalize_host_path

    outside = tmp_path.parent / f"zc142-trust-session-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    registry = WorkspaceRootRegistry(tmp_path, project_dir=tmp_path, load_persisted=False)
    coordinator = _coordinator(project_dir=tmp_path, workspace_root_registry=registry)
    presentation = {
        "kind": "directory_trust",
        "directory": str(outside),
        "target_path": str(outside / "a.toml"),
        "tool_name": "read_file",
        "access": "read",
        "shadows_workspace": False,
    }
    coordinator._record_directory_trust(
        "allow_session", presentation, run_id="run-1"
    )
    assert coordinator.session_rules == []
    resolved = registry.resolve(str(outside / "a.toml"))
    assert resolved.root.scope == "session"
    assert normalize_host_path(outside) == resolved.root.path
    settings = tmp_path / ".harness" / "settings.json"
    assert not settings.is_file()


def test_directory_trust_already_granted_skips_repeat_prompt(tmp_path: Path) -> None:
    """session/project 信任后同目录排队调用免弹；once 与未信任不免弹。"""
    from harness_agent.policy.workspace_roots import WorkspaceRootRegistry

    outside = tmp_path.parent / f"zc142-trust-skip-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "a.toml"
    target.write_text("x", encoding="utf-8")
    registry = WorkspaceRootRegistry(tmp_path, project_dir=tmp_path, load_persisted=False)
    coordinator = _coordinator(project_dir=tmp_path, workspace_root_registry=registry)
    presentation = {
        "kind": "directory_trust",
        "directory": str(outside),
        "target_path": str(target),
        "tool_name": "read_file",
        "access": "read",
        "shadows_workspace": False,
    }
    # 未信任：必须弹窗
    assert coordinator._directory_trust_already_granted(presentation, run_id="run-1") is False
    # once 单次消费，不能作为后续排队调用的免弹依据
    registry.trust(outside, "once", run_id="run-1")
    assert coordinator._directory_trust_already_granted(presentation, run_id="run-1") is False
    registry.consume_once(outside, run_id="run-1")
    # session 信任稳定有效：同目录后续调用免弹
    registry.trust(outside, "session")
    assert coordinator._directory_trust_already_granted(presentation, run_id="run-1") is True


def test_generate_permission_rule_top_level_file_uses_tool_wildcard() -> None:
    """顶层文件与绝对根文件同样生成工具级通配，硬保护由预检兜底。"""
    rules = _generate_permission_rule("edit_file", {"file_path": "main.py"})
    assert rules == [PermissionRule(tool="edit_file", resource="*", effect="allow")]

    rules = _generate_permission_rule("write_file", {"file_path": "/main.py"})
    assert rules == [PermissionRule(tool="write_file", resource="*", effect="allow")]


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
        """本线程允许一次删除后，工作区内其他文件的删除也自动放行。"""
        coordinator = _coordinator()
        coordinator._record_approval_rule(
            "delete_file",
            {"file_path": str(tmp_path / "a.txt")},
            "approve_thread",
        )

        preflight = self._preflight(coordinator, tmp_path)
        # 项目级通配规则：同路径和工作区内其他路径都不再弹窗；真实越界
        # 路径仍由独立安全用例保证必须重新确认。
        assert preflight(self._delete_request(str(tmp_path / "a.txt"))) is False
        assert preflight(self._delete_request(str(tmp_path / "other.txt"))) is False
        assert preflight(self._delete_request(str(tmp_path / "src" / "b.txt"))) is False

    def test_thread_approval_still_asks_for_sensitive_delete(
        self, tmp_path: Path
    ) -> None:
        """allow 规则命中敏感路径删除时仍强制弹窗确认。"""
        coordinator = _coordinator()
        coordinator._record_approval_rule(
            "delete_file", {"file_path": "/tmp/a.txt"}, "approve_thread"
        )

        preflight = self._preflight(coordinator, tmp_path)
        assert preflight(self._delete_request("/.git/index")) is True
        assert preflight(self._delete_request("/.harness/settings.json")) is True
