"""根 Agent 的 update_goal 与 grader 受控复验工具。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from langgraph.runtime import get_runtime
from pydantic import BaseModel, Field

from harness_agent.goals.models import GoalStoreError
from harness_agent.goals.repository import GoalRepositoryBudget, create_goal_repository_tools
from harness_agent.goals.rubric_adapter import grader_tool_names
from harness_agent.runtime.run_context import plan_constraint_active, require_run_context


class UpdateGoalInput(BaseModel):
    """update_goal 的结构化参数。"""

    status: Literal["complete", "blocked"] = Field(description="complete 只提交完成说明；blocked 立即阻塞目标。")
    note: str = Field(description="完成说明或阻塞原因。")


class RerunVerificationInput(BaseModel):
    """rerun_verification 只能引用当前 Run 已执行过的证据。"""

    evidence_id: str = Field(description="当前 Run 中已记录的验证证据 ID。")


def create_update_goal_tool() -> StructuredTool:
    """根 Build Agent 常驻；complete 只申请，blocked 立即 CAS。"""

    async def update_goal(status: Literal["complete", "blocked"], note: str) -> str:
        context = require_run_context(get_runtime())
        binding = getattr(context, "goal_binding", None)
        store = getattr(context, "goal_store", None)
        if binding is None or store is None:
            return "GOAL_NOT_ACTIVE: 当前没有可操作的目标。"
        if plan_constraint_active(context) and status == "complete":
            return "GOAL_PLAN_COMPLETE_FORBIDDEN: 计划阶段不能把目标标为完成。"
        now_ms = int(__import__("time").time() * 1000)
        try:
            if status == "blocked":
                await store.block_goal(
                    thread_id=context.thread_id,
                    goal_id=binding.goal_id,
                    goal_revision=binding.goal_revision,
                    note=note,
                    now_ms=now_ms,
                )
                return "目标已标记为 blocked，本轮不再验收。"
            await store.request_completion(
                thread_id=context.thread_id,
                run_id=context.run_id,
                goal_id=binding.goal_id,
                goal_revision=binding.goal_revision,
                note=note,
                now_ms=now_ms,
            )
            return "完成说明已保存，等待独立验收。目标仍为 active。"
        except GoalStoreError as exc:
            return f"{exc.code}: 无法更新目标。"

    return StructuredTool.from_function(
        coroutine=update_goal,
        name="update_goal",
        description="提交当前目标完成说明或标记阻塞。complete 不会自行完成目标。",
        args_schema=UpdateGoalInput,
    )


def create_rerun_verification_tool() -> StructuredTool:
    """grader 专用：只重跑当前 Run 已有的安全验证命令。"""

    def rerun_verification(evidence_id: str) -> str:
        context = require_run_context(get_runtime())
        registry = getattr(context, "verification_registry", None)
        if registry is None:
            raise GoalStoreError("GOAL_VERIFICATION_FORBIDDEN")
        evidence = registry.rerun(evidence_id)
        return (
            "证据已定位，必须由当前审批与沙箱重跑原命令："
            f" command={evidence.command!r} cwd={evidence.cwd!r} sandbox={evidence.sandbox!r}"
        )

    return StructuredTool.from_function(
        func=rerun_verification,
        name="rerun_verification",
        description="按 evidence_id 重跑当前 Run 已执行过的验证命令，不能提交新命令。",
        args_schema=RerunVerificationInput,
    )


def create_grader_tools(
    workspace: Path,
    primary_names: Iterable[str],
) -> tuple[Any, ...]:
    """构造 grader 最小只读工具视图。"""
    allowed = grader_tool_names(primary_names)
    tools: list[Any] = []
    if allowed & {"ls", "read_file", "glob", "grep"}:
        budget = GoalRepositoryBudget()
        for tool in create_goal_repository_tools(workspace, budget):
            if tool.name in allowed:
                tools.append(tool)
    if "rerun_verification" in allowed:
        tools.append(create_rerun_verification_tool())
    return tuple(tools)
