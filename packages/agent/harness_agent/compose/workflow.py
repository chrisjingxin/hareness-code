"""Compose workflow：代码驱动的 Understand → Plan → 用户确认流程。

流程只由 ComposeStateMachine、严格 artifact 校验和真实 Interaction 推进：
可发现事实由 stage Agent 查询，真正产品决策走 question，整体方案走
批准/修改/取消门禁。当前工作包实现到 Plan 批准为止，Build 阶段由后续
工作包接管；未批准前不会发生任何 Builder 写入。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from harness_agent.compose.context_pack import build_plan_pack, build_understand_pack
from harness_agent.compose.models import (
    ArtifactKind,
    ComposeRunState,
    ComposeRunStatus,
    ComposeStage,
    PlanArtifact,
    UnderstandingArtifact,
    make_artifact,
    validate_plan_artifact,
    validate_understanding_artifact,
)
from harness_agent.compose.stage_agents import StageAgentPort, StageRequest
from harness_agent.compose.state_machine import (
    SCHEMA_INVALID_RETRY_ALLOWED,
    ComposeEvent,
    ComposeStateMachine,
)
from harness_agent.protocol.generated import EVENT_TYPE

if TYPE_CHECKING:
    from harness_agent.threads.compose_artifact_store import ComposeArtifactStore

RUN_STARTED = EVENT_TYPE["RUN_STARTED"]
RUN_PROGRESS = EVENT_TYPE["RUN_PROGRESS"]
COMPOSE_STATE = EVENT_TYPE["COMPOSE_STATE"]


class ComposeWorkflowError(RuntimeError):
    """Compose 流程的稳定错误；由 host adapter 映射为 RunError。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定错误码与诊断文案。"""
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ComposeOutcome:
    """workflow 提议的终态；host adapter 映射为 coordinator 可收敛的事实。"""

    status: str
    code: str | None = None
    message: str = ""
    retryable: bool = False

MAX_PLAN_SUMMARY_CHARS = 4_000
_EMPTY_ANSWER_MARK = "(未回答)"


def _progress_payload(run: Any, phase: str) -> dict[str, object]:
    """生成只包含事实阶段和活动时长的运行进度 payload。"""
    safe_phase = phase if phase in {"preparing", "model"} else "preparing"
    started = getattr(run, "started_at", None) or 0
    import time as _time

    return {
        "phase": safe_phase,
        "elapsed_ms": max(0, round((_time.monotonic() - started) * 1000)),
    }


def load_method_assets() -> dict[str, str]:
    """读取 compose/methods/*.md 私有方法资产；缺失文件按 fail closed 处理。"""
    base = Path(__file__).parent / "methods"
    assets: dict[str, str] = {}
    for path in sorted(base.glob("*.md")):
        assets[path.stem] = path.read_text(encoding="utf-8")
    return assets


@dataclass(frozen=True, slots=True)
class ComposeServices:
    """Host 提供的 Compose workflow 依赖；adapter 只经此 seam 使用它们。"""

    stage_agent: StageAgentPort
    method_assets: Mapping[str, str]
    workspace_root: str = ""
    now_ms: Callable[[], int] = field(
        default=lambda: int(time.time() * 1000)
    )


def _understanding_payload(artifact: UnderstandingArtifact) -> dict[str, object]:
    """把 UnderstandingArtifact 序列化为 artifact payload。"""
    return {
        "goal": artifact.goal,
        "constraints": list(artifact.constraints),
        "acceptance": list(artifact.acceptance),
        "out_of_scope": list(artifact.out_of_scope),
        "open_decisions": list(artifact.open_decisions),
        "change_kind": artifact.change_kind,
    }


def _plan_payload(plan: PlanArtifact) -> dict[str, object]:
    """把 PlanArtifact 序列化为 artifact payload（含全部任务事实）。"""
    return {
        "solution": plan.solution,
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "kind": task.kind.value,
                "acceptance": task.acceptance,
                "depends_on": list(task.depends_on),
                "verification_commands": list(task.verification_commands),
            }
            for task in plan.tasks
        ],
        "relevant_pointers": list(plan.relevant_pointers),
    }


def _plan_summary(plan: PlanArtifact) -> str:
    """生成有界的方案摘要，用于批准门禁展示，不含内部字段。"""
    lines = [f"方案：{plan.solution[:500]}"]
    for index, task in enumerate(plan.tasks, start=1):
        lines.append(
            f"{index}. {task.title}（{task.kind.value}）验收：{task.acceptance[:200]}"
        )
        if task.verification_commands:
            lines.append(
                "   验证：" + "；".join(command[:120] for command in task.verification_commands[:3])
            )
    summary = "\n".join(lines)
    if len(summary) > MAX_PLAN_SUMMARY_CHARS:
        return summary[:MAX_PLAN_SUMMARY_CHARS] + "\n[摘要已截断]"
    return summary


class ComposeWorkflow:
    """一个 Compose Run 的驱动循环；每次合法 transition 发布完整 projection。"""

    def __init__(self, *, services: ComposeServices, store: ComposeArtifactStore) -> None:
        """注入 stage port、方法资产与 artifact store。"""
        self._services = services
        self._store = store
        self._pending_answers: tuple[tuple[str, str], ...] = ()
        self._feedback = ""
        self._failure_code = "COMPOSE_FAILED"
        self._failure_message = "Compose workflow failed"

    async def run(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeOutcome:
        """执行状态机直到唯一终态，并发布 run.started 与每帧 projection。"""
        port.emit(
            run,
            RUN_STARTED,
            {
                "resumed": False,
                "mode": run.start.mode,
                "skills_snapshot_id": run.preparation.skill_snapshot_id,
            },
        )
        run.status = "running"
        port.emit(run, RUN_PROGRESS, _progress_payload(run, "preparing"))
        self._emit_state(run, port, state)
        while not state.terminal:
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            state = await self._drive_stage(run, port, state)
        return self._outcome(state)

    async def _drive_stage(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """按当前 stage 分发；stage 只由状态机推进。"""
        if state.stage is ComposeStage.UNDERSTAND:
            return await self._understand(run, port, state)
        if state.stage is ComposeStage.PLAN:
            if state.status is ComposeRunStatus.WAITING_USER:
                return await self._plan_gate(run, port, state)
            return await self._plan(run, port, state)
        if state.stage is ComposeStage.BUILD:
            # WP4 tracer bullet 边界：Build 阶段由后续工作包实现。
            raise ComposeWorkflowError(
                "COMPOSE_BUILD_STAGE_PENDING",
                "Build stage lands in a later work package",
            )
        raise ComposeWorkflowError(
            "COMPOSE_WORKFLOW_STATE_INVALID",
            f"stage {state.stage.value} cannot be driven",
        )

    async def _understand(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """Understand：短 artifact、产品决策回写、schema invalid 单次重试。"""
        schema_retries = 0
        while True:
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            pack = build_understand_pack(
                user_request=run.message,
                revision=state.revision,
                method_asset=self._method("understand"),
                workspace_root=self._services.workspace_root,
                answers=self._pending_answers,
            )
            try:
                result = await self._run_stage(run, "understand", pack.render())
                raw = result.output if isinstance(result.output, Mapping) else {}
                artifact = validate_understanding_artifact(raw)
            except ValueError as exc:
                schema_retries += 1
                if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                    return self._fail(
                        run, port, state, "COMPOSE_ARTIFACT_INVALID", str(exc)
                    )
                state = self._apply(run, port, state, ComposeEvent.STAGE_RETRY)
                continue
            if artifact.open_decisions:
                self._pending_answers = await self._ask_questions(
                    run, port, state, artifact.open_decisions
                )
                continue
            artifact_row = make_artifact(
                ArtifactKind.UNDERSTANDING,
                run_id=run.ref.run_id,
                source_execution_id=result.execution_id,
                created_at_ms=self._services.now_ms(),
                payload=_understanding_payload(artifact),
            )
            await self._store.save_artifact(artifact_row)
            return self._apply(
                run,
                port,
                state,
                ComposeEvent.UNDERSTAND_COMPLETE,
                artifact_id=artifact_row.artifact_id,
            )

    async def _plan(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """Plan：基于已确认 Understanding 产出无占位符、无环的方案。"""
        schema_retries = 0
        while True:
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            understanding = await self._load_understanding(run, state)
            pack = build_plan_pack(
                user_request=run.message,
                revision=state.revision,
                method_asset=self._method("plan"),
                understanding=understanding,
                workspace_root=self._services.workspace_root,
                feedback=self._feedback,
            )
            try:
                result = await self._run_stage(run, "plan", pack.render())
                raw = result.output if isinstance(result.output, Mapping) else {}
                plan = validate_plan_artifact(raw)
            except ValueError as exc:
                schema_retries += 1
                if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                    return self._fail(
                        run, port, state, "COMPOSE_ARTIFACT_INVALID", str(exc)
                    )
                state = self._apply(run, port, state, ComposeEvent.STAGE_RETRY)
                continue
            artifact_row = make_artifact(
                ArtifactKind.PLAN,
                run_id=run.ref.run_id,
                source_execution_id=result.execution_id,
                created_at_ms=self._services.now_ms(),
                payload=_plan_payload(plan),
            )
            await self._store.save_artifact(artifact_row)
            return self._apply(
                run,
                port,
                state,
                ComposeEvent.PLAN_COMPLETE,
                artifact_id=artifact_row.artifact_id,
            )

    async def _plan_gate(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """整体方案门禁：批准 / 携 feedback 修改 / 取消。"""
        plan = await self._load_plan(run, state)
        spec = await self._approval_spec(state, _plan_summary(plan))
        result = await port.request_approval(
            run,
            request_id=spec["request_id"],
            interrupt_id=spec["interrupt_id"],
            description=spec["description"],
            decisions=spec["decisions"],
            action_requests=spec["action_requests"],
        )
        value = result.value if isinstance(result.value, Mapping) else {}
        decision = str(value.get("decision") or "")
        feedback = str(value.get("feedback") or "")
        if decision in {"approve_once", "approve_thread", "approve_project"}:
            return self._apply(
                run, port, state, ComposeEvent.PLAN_APPROVE, tasks=plan.tasks
            )
        if decision == "reject_with_feedback" and feedback.strip():
            self._feedback = feedback
            return self._apply(run, port, state, ComposeEvent.PLAN_REVISE)
        # reject / 无 feedback 的修改请求都视为取消整体方案。
        return self._apply(run, port, state, ComposeEvent.PLAN_CANCEL)

    async def _ask_questions(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        decisions: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        """把真实产品决策作为 typed question 交给用户，回写为 (决策, 答案)。"""
        interrupt_id = f"compose-understand-{state.revision}"
        questions = [
            {
                "id": f"question-{index + 1}",
                "question": decision,
                "header": "",
                "body": "",
                "options": [],
                "multi_select": False,
                "allow_other": True,
            }
            for index, decision in enumerate(decisions)
        ]
        result = await port.request_question(
            run,
            request_id=interrupt_id,
            interrupt_id=interrupt_id,
            questions=questions,
        )
        answers_by_id = result.value.get("answers", {}) if isinstance(result.value, Mapping) else {}
        answers: list[tuple[str, str]] = []
        for index, decision in enumerate(decisions):
            raw = answers_by_id.get(f"question-{index + 1}", []) if isinstance(answers_by_id, Mapping) else []
            answer = str(raw[0]) if isinstance(raw, list) and raw else _EMPTY_ANSWER_MARK
            answers.append((decision, answer))
        return tuple(answers)

    async def _approval_spec(self, state: ComposeRunState, summary: str) -> Any:
        """构造整体方案批准请求；decisions 白名单与协议一致。"""
        interrupt_id = f"compose-plan-{state.revision}"
        return {
            "request_id": interrupt_id,
            "interrupt_id": interrupt_id,
            "description": summary,
            "decisions": ["approve_once", "reject_with_feedback", "reject"],
            "action_requests": [
                {
                    "name": "compose.plan.approve",
                    "description": "批准整体方案进入执行；选择修改必须填写意见；选择拒绝将取消本次 Run",
                    "args": {},
                }
            ],
        }

    async def _load_understanding(
        self, run: Any, state: ComposeRunState
    ) -> UnderstandingArtifact:
        """从 store 读回 Understanding artifact；缺失按状态机非法处理。"""
        artifact_id = state.understanding_artifact_id
        if artifact_id is None:
            raise ComposeWorkflowError(
                "COMPOSE_WORKFLOW_STATE_INVALID", "understanding artifact missing"
            )
        row = await self._store.load_artifact(run.ref.run_id, artifact_id)
        if row is None or row.kind is not ArtifactKind.UNDERSTANDING:
            raise ComposeWorkflowError(
                "COMPOSE_WORKFLOW_STATE_INVALID", "understanding artifact unavailable"
            )
        return validate_understanding_artifact(row.payload)

    async def _load_plan(self, run: Any, state: ComposeRunState) -> PlanArtifact:
        """从 store 读回 Plan artifact；缺失按状态机非法处理。"""
        artifact_id = state.plan_artifact_id
        if artifact_id is None:
            raise ComposeWorkflowError("COMPOSE_WORKFLOW_STATE_INVALID", "plan artifact missing")
        row = await self._store.load_artifact(run.ref.run_id, artifact_id)
        if row is None or row.kind is not ArtifactKind.PLAN:
            raise ComposeWorkflowError("COMPOSE_WORKFLOW_STATE_INVALID", "plan artifact unavailable")
        return validate_plan_artifact(row.payload)

    async def _run_stage(
        self, run: Any, stage: str, task: str
    ) -> Any:
        """启动一次 fresh Managed stage execution。"""
        profile = run.preparation.agent_engine_profile
        return await self._services.stage_agent.run(
            StageRequest(
                stage=stage,
                task=task,
                parent_ref=run.root_execution_ref,
                profile_key=profile.profile_key if profile is not None else "",
                cancellation_token=run.cancellation_token,
            )
        )

    def _method(self, stage: str) -> str:
        """读取私有方法资产；缺失直接 fail closed。"""
        asset = self._services.method_assets.get(stage)
        if not asset:
            raise ComposeWorkflowError(
                "COMPOSE_METHOD_ASSET_MISSING", f"method asset {stage} missing"
            )
        return asset

    def _apply(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        event: ComposeEvent,
        **payload: Any,
    ) -> ComposeRunState:
        """应用一次合法 transition 并发布 revision 递增的完整 projection。"""
        next_state = ComposeStateMachine.apply(state, event, **payload)
        self._emit_state(run, port, next_state)
        return next_state

    def _emit_state(self, run: Any, port: Any, state: ComposeRunState) -> None:
        """发布有界 compose.state projection；不含 artifact 正文。"""
        port.emit(run, COMPOSE_STATE, state.projection())

    def _fail(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        code: str,
        message: str,
    ) -> ComposeRunState:
        """记录稳定错误码并收敛为 failed 终态。"""
        self._failure_code = code
        self._failure_message = message
        return self._apply(run, port, state, ComposeEvent.FAIL)

    def _outcome(self, state: ComposeRunState) -> ComposeOutcome:
        """把 workflow 终态映射为 host 可收敛的终态提议。"""
        if state.status is ComposeRunStatus.COMPLETED:
            return ComposeOutcome("completed")
        if state.status is ComposeRunStatus.CANCELLED:
            return ComposeOutcome("cancelled", message="Cancelled by user")
        if state.status is ComposeRunStatus.BLOCKED:
            return ComposeOutcome(
                "failed",
                code="COMPOSE_BLOCKED",
                message=state.blocked_reason or "Compose workflow blocked",
                retryable=False,
            )
        return ComposeOutcome(
            "failed",
            code=self._failure_code,
            message=self._failure_message,
            retryable=False,
        )
