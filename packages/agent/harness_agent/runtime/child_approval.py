"""内置 child 的审批桥：不走 LangGraph interrupt，直接请求 Host Interaction。

general-purpose 在父 default 下按 auto-edit 运行：工作区普通写入自动放行，
execute / 敏感路径 / 目录信任仍询问。拒绝结果作为 ToolMessage 回到 child，
不取消父 Run。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.policy.approval_policy import evaluate_permission
from harness_agent.policy.permission_rules import PermissionRule
from harness_agent.runtime.agent_delegation import DelegateAgent, child_execution_ref, current_delegation_call
from harness_agent.runtime.interactions import InteractionRequest, InteractionResult

_CURRENT_CHILD_COMMAND: ContextVar[DelegateAgent | None] = ContextVar(
    "harness_current_child_command",
    default=None,
)

GENERAL_PURPOSE_TASK_APPROVAL = (
    "可在工作区改文件，普通写入过程中不再询问；执行命令和敏感路径仍会询问。"
)


def bind_child_command(command: DelegateAgent) -> Token[DelegateAgent | None]:
    """在 Inline runner 期间绑定当前 DelegateAgent，供 HITL 取 child 身份。"""
    return _CURRENT_CHILD_COMMAND.set(command)


def reset_child_command(token: Token[DelegateAgent | None]) -> None:
    """结束 Inline runner 时恢复 ContextVar。"""
    _CURRENT_CHILD_COMMAND.reset(token)


def task_dispatch_description(tool_args: Mapping[str, Any]) -> str:
    """派出 task 时给用户看的审批说明。"""
    target = str(tool_args.get("subagent_type") or tool_args.get("agent") or "").strip()
    summary = str(tool_args.get("description") or tool_args.get("prompt") or "").strip()
    clipped = summary if len(summary) <= 160 else f"{summary[:157]}…"
    if target == "general-purpose":
        body = f"派出子代理 general-purpose"
        if clipped:
            body = f"{body}：{clipped}"
        return f"{body}\n{GENERAL_PURPOSE_TASK_APPROVAL}"
    if target:
        return f"派出子代理 {target}" + (f"：{clipped}" if clipped else "")
    return clipped or "派出子代理"


class ChildHitlMiddleware(AgentMiddleware):
    """按 child 有效审批模式拦截工具调用，并把询问转到 Host Interaction。"""

    def __init__(
        self,
        *,
        agent_id: str,
        approval_mode: str,
        rules_provider: Callable[[], list[PermissionRule]] | None = None,
        workspace_guard: Any | None = None,
    ) -> None:
        """冻结角色 ID 与审批模式；规则与目录信任在调用时读取。"""
        self._agent_id = agent_id
        self._approval_mode = approval_mode
        self._rules_provider = rules_provider
        self._workspace_guard = workspace_guard

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        """同步路径不用于生产 child。"""
        raise RuntimeError("CHILD_HITL_ASYNC_REQUIRED")

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """deny 硬拒绝；allow 直接执行；ask 则请求 Host 审批。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name") or "unknown")
        raw_args = tool_call.get("args") or {}
        tool_args = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        rules = self._rules_provider() if self._rules_provider is not None else []
        decision = evaluate_permission(tool_name, tool_args, self._approval_mode, rules)
        if decision == "deny":
            return self._reject(tool_call, f"权限规则拒绝 {tool_name}：该操作已被 deny 规则禁止，不可覆盖。")

        trust = None
        if self._workspace_guard is not None:
            trust = self._workspace_guard.needs_directory_trust(request)
        if trust is not None:
            allowed = await self._ask_directory_trust(tool_name, trust)
            if not allowed:
                return self._reject(
                    tool_call,
                    f"用户拒绝信任目录 {trust.directory}，不能访问 {trust.target_path}。",
                )
            self._workspace_guard.registry.trust(trust.directory, "session")

        if decision == "allow" or self._approval_mode == "yolo":
            return await handler(request)

        approved = await self._ask_approval(tool_name, tool_args)
        if not approved:
            return self._reject(tool_call, f"用户拒绝了子代理 {self._agent_id} 的 {tool_name}。请改用其他方案，不要取消整个任务。")
        return await handler(request)

    async def _ask_approval(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """向 Host 请求审批；无通道时 fail closed。"""
        result = await self._request(
            "approval",
            {
                "interrupt_id": str(uuid.uuid4()),
                "description": f"子代理 {self._agent_id} 需要执行 {tool_name}",
                "requests": {"action_requests": [{"name": tool_name, "args": tool_args}]},
                "decisions": [
                    "approve_once",
                    "approve_thread",
                    "approve_project",
                    "reject",
                    "reject_with_feedback",
                ],
            },
        )
        if result is None:
            return False
        value = result.value if isinstance(result.value, Mapping) else {}
        decision = str(value.get("decision") or "")
        try:
            recorder = getattr(current_delegation_call().run_context, "record_approval", None)
        except Exception:
            recorder = None
        if callable(recorder):
            recorder(tool_name, tool_args, decision)
        return decision in {"approve_once", "approve_thread", "approve_project"}

    async def _ask_directory_trust(self, tool_name: str, trust: Any) -> bool:
        """向 Host 请求目录信任。"""
        result = await self._request(
            "directory_trust",
            {
                "interrupt_id": str(uuid.uuid4()),
                "directory": str(trust.directory),
                "target_path": str(trust.target_path),
                "tool_name": tool_name,
                "access": str(getattr(trust, "access", "read") or "read"),
                "shadows_workspace": bool(getattr(trust, "shadows_workspace", False)),
                "decisions": ["allow_session", "deny"],
            },
        )
        if result is None:
            return False
        value = result.value if isinstance(result.value, Mapping) else {}
        return str(value.get("decision") or "") == "allow_session"

    async def _request(self, kind: str, payload: dict[str, object]) -> InteractionResult | None:
        """构造带 child 身份的 InteractionRequest 并交给父 RunContext 的 port。"""
        try:
            parent = current_delegation_call().run_context
        except Exception:
            return None
        port = getattr(parent, "interaction_port", None)
        if not callable(port):
            return None
        command = _CURRENT_CHILD_COMMAND.get()
        child_ref = child_execution_ref(command) if command is not None else None
        spec = InteractionRequest(
            request_id=str(uuid.uuid4()),
            type=kind,
            payload=payload,
            interrupt_id=str(payload.get("interrupt_id") or ""),
            execution_id=None if child_ref is None else child_ref.execution_id,
            parent_execution_id=None if child_ref is None else child_ref.parent_execution_id,
            agent_id=self._agent_id,
        )
        return await port(spec)

    @staticmethod
    def _reject(tool_call: Mapping[str, Any], message: str) -> ToolMessage:
        """把拒绝写回 child tool result，让模型改方案。"""
        name = str(tool_call.get("name") or "unknown")
        return ToolMessage(
            content=message,
            name=name,
            tool_call_id=str(tool_call.get("id") or "child-hitl"),
            status="error",
        )
