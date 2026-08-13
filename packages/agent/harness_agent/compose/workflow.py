"""Compose workflow：代码驱动的 Understand → Plan → 用户确认流程。

流程只由 ComposeStateMachine、严格 artifact 校验和真实 Interaction 推进：
可发现事实由 stage Agent 查询，真正产品决策走 question，整体方案走
批准/修改/取消门禁。当前工作包实现到 Plan 批准为止，Build 阶段由后续
工作包接管；未批准前不会发生任何 Builder 写入。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from harness_agent.compose.context_pack import (
    build_plan_pack,
    build_review_pack,
    build_task_pack,
    build_understand_pack,
)
from harness_agent.compose.models import (
    ArtifactKind,
    ChangeKind,
    ComposeRunState,
    ComposeRunStatus,
    ComposeStage,
    ComposeTask,
    EvidenceItem,
    EvidenceStatus,
    FindingSeverity,
    PlanArtifact,
    TaskStatus,
    UnderstandingArtifact,
    make_artifact,
    validate_plan_artifact,
    validate_review_report,
    validate_task_result_artifact,
    validate_understanding_artifact,
    validate_verification_evidence,
)
from harness_agent.compose.verification import (
    VerificationError,
    VerificationPort,
    VerificationRequest,
)
from harness_agent.compose.stage_agents import (
    StageAgentPort,
    StageRequest,
    compose_scope_stage,
    make_activity_scope,
    summarize_build,
    summarize_plan,
    summarize_review,
    summarize_stage_failure,
    summarize_understanding,
    summarize_verify,
)
from harness_agent.compose.state_machine import (
    SCHEMA_INVALID_RETRY_ALLOWED,
    TASK_ATTEMPT_BUDGET,
    ComposeEvent,
    ComposeStateMachine,
)
from harness_agent.protocol.generated import EVENT_TYPE
from harness_agent.runtime.execution_stream import StreamInteractionRequest

if TYPE_CHECKING:
    from harness_agent.threads.compose_artifact_store import ComposeArtifactStore

RUN_STARTED = EVENT_TYPE["RUN_STARTED"]
RUN_PROGRESS = EVENT_TYPE["RUN_PROGRESS"]
COMPOSE_STATE = EVENT_TYPE["COMPOSE_STATE"]
COMPOSE_SUMMARY = EVENT_TYPE["COMPOSE_SUMMARY"]
CONTENT_DELTA = EVENT_TYPE["CONTENT_DELTA"]
TOOL_STARTED = EVENT_TYPE["TOOL_STARTED"]
TOOL_COMPLETED = EVENT_TYPE["TOOL_COMPLETED"]


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
    return {
        "phase": safe_phase,
        "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
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
    verification: VerificationPort | None = None
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
        self._changed_file_count: int | None = None
        self._review_verdict: tuple[str, str] | None = None
        self._unresolved_risks: tuple[str, ...] = ()

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
        port.mark_running(run)
        port.emit(run, RUN_PROGRESS, _progress_payload(run, "preparing"))
        self._emit_state(run, port, state)
        await self._store.save_run(state)
        try:
            while not state.terminal:
                if port.is_cancelled(run):
                    raise asyncio.CancelledError
                state = await self._drive_stage(run, port, state)
                await self._flush_activity_records(run)
        except asyncio.CancelledError:
            # 外部取消也必须先发布/持久化唯一 cancelled projection，不能只让
            # Coordinator 发 terminal event 而把 Compose 面板留在 running。
            if not state.terminal:
                state = await self._apply(run, port, state, ComposeEvent.CANCEL)
            await self._flush_activity_records(run)
            await self._record_final_summary(run, port, state)
            raise
        except ComposeWorkflowError:
            # 阶段基础设施或 schema retry 耗尽可能在 handler 内直接抛错；
            # 统一把当前阶段收敛为 failed，再交给 Host 映射稳定错误码。
            if not state.terminal:
                state = await self._apply(run, port, state, ComposeEvent.FAIL)
            await self._flush_activity_records(run)
            await self._record_final_summary(run, port, state)
            raise
        await self._flush_activity_records(run)
        await self._record_final_summary(run, port, state)
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
            return await self._build(run, port, state)
        if state.stage is ComposeStage.VERIFY:
            return await self._verify(run, port, state)
        if state.stage is ComposeStage.REVIEW:
            return await self._review(run, port, state)
        raise ComposeWorkflowError(
            "COMPOSE_WORKFLOW_STATE_INVALID",
            f"stage {state.stage.value} cannot be driven",
        )

    async def _build(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """按依赖顺序执行 Plan task；每个 task 只拿到自己的 ContextPack。"""
        while True:
            task = self._next_pending_task(state)
            if task is None:
                return await self._apply(run, port, state, ComposeEvent.BUILD_COMPLETE)
            state = await self._apply(
                run, port, state, ComposeEvent.TASK_STARTED, task_id=task.id
            )
            failed = await self._execute_task(run, port, state, task)
            if failed:
                return await self._apply(
                    run, port, state, ComposeEvent.TASK_FAIL, task_id=task.id
                )
            state = await self._apply(
                run, port, state, ComposeEvent.TASK_COMPLETE, task_id=task.id
            )

    def _next_pending_task(self, state: ComposeRunState) -> ComposeTask | None:
        """按 Plan 顺序返回第一个依赖已满足且未完成的任务。"""
        by_id = {task.id: task for task in state.tasks}
        for task in state.tasks:
            if task.status in (TaskStatus.PASSED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                continue
            if all(
                by_id[dependency].status is TaskStatus.PASSED
                for dependency in task.depends_on
                if dependency in by_id
            ):
                return task
        return None

    async def _execute_task(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        task: ComposeTask,
    ) -> bool:
        """执行一个 task；按 change_kind 选择 TDD/direct，失败注入 Debug。

        返回 True 表示任务最终失败（attempt 耗尽），False 表示通过。
        """
        understanding = await self._load_understanding(run, state)
        plan = await self._load_plan(run, state)
        requires_tdd = task.kind in (ChangeKind.BEHAVIOR, ChangeKind.BUG, ChangeKind.REFACTOR)
        previous_failure = ""
        for attempt in range(1, TASK_ATTEMPT_BUDGET + 1):
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            use_debug = attempt > 1
            method = self._method("build")
            if requires_tdd:
                method += "\n\n" + self._method("tdd")
            if use_debug:
                method += "\n\n" + self._method("debug")
            pack = build_task_pack(
                user_request=run.message,
                revision=state.revision,
                method_asset=method,
                task=task,
                understanding=understanding,
                relevant_pointers=plan.relevant_pointers,
                workspace_root=self._services.workspace_root,
                previous_failure=previous_failure,
            )
            attempt_failed = False
            schema_retries = 0
            while True:
                try:
                    result = await self._run_stage(
                        run, port, state, "build", pack.render(),
                        task_id=task.id,
                        task_title=task.title,
                        attempt=attempt,
                    )
                    raw = result.output if isinstance(result.output, Mapping) else {}
                    task_result = validate_task_result_artifact(raw)
                    break
                except ValueError as exc:
                    self._emit_stage_summary(
                        run,
                        port,
                        stage="build",
                        attempt=attempt,
                        status="failed",
                        text=summarize_stage_failure(str(exc)),
                        execution_id=getattr(exc, "execution_id", None),
                        compose_scope=getattr(exc, "compose_scope", None),
                        agent_id="build",
                    )
                    schema_retries += 1
                    if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                        attempt_failed = True
                        previous_failure = "builder 输出不符合 TaskResultArtifact schema"
                        break
                    continue
            if attempt_failed:
                continue
            if task_result.task_id != task.id:
                attempt_failed = True
                previous_failure = f"builder 返回了错误 task_id：{task_result.task_id}"
                continue
            if requires_tdd and not task_result.red_evidence.strip():
                attempt_failed = True
                previous_failure = "行为/Bug/refactor 任务缺少 RED evidence"
                continue
            if task_result.remaining_issue.strip():
                attempt_failed = True
                previous_failure = task_result.remaining_issue
                continue
            self._emit_stage_summary(
                run,
                port,
                stage="build",
                attempt=attempt,
                status="passed",
                text=summarize_build(task_title=task.title, task_result=task_result),
                execution_id=result.execution_id,
                agent_id=result.agent_id,
                compose_scope=getattr(result, "compose_scope", None),
            )
            await self._store.save_artifact(
                make_artifact(
                    ArtifactKind.TASK_RESULT,
                    run_id=run.ref.run_id,
                    source_execution_id=result.execution_id,
                    created_at_ms=self._services.now_ms(),
                    payload={
                        "task_id": task_result.task_id,
                        "changed_paths": list(task_result.changed_paths),
                        "focused_test_evidence": task_result.focused_test_evidence,
                        "red_evidence": task_result.red_evidence,
                        "remaining_issue": task_result.remaining_issue,
                    },
                )
            )
            return False
        return True

    async def _verify(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """运行全部 required 命令；无 fresh pass evidence 不能进入 Review。"""
        verification = self._services.verification
        if verification is None:
            raise ComposeWorkflowError(
                "COMPOSE_VERIFICATION_UNAVAILABLE", "verification port missing"
            )
        plan = await self._load_plan(run, state)
        commands: list[str] = []
        for task in plan.tasks:
            for command in task.verification_commands:
                if command not in commands:
                    commands.append(command)
        profile = run.preparation.agent_engine_profile
        resource_key = (
            profile.sandbox_config_fingerprint
            if profile is not None
            else "compose-default"
        )
        items: list[EvidenceItem] = []
        failed_command = ""
        evidence_anchor_id = ""
        source_execution_id = f"verify-{state.revision}"
        passed_count = 0
        blocked_count = 0
        for index, command in enumerate(commands):
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            label = command[:200]
            attempt = index + 1
            command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
            scope = make_activity_scope(
                stage_agent_id="verify",
                attempt=attempt,
                invocation_id=f"{state.revision}-{command_digest}",
                task_title=label,
            )
            tool_call_id = f"verify-{command_digest}"
            # UI 将每条 Verify 命令投影为 scoped Tool；通过条件仍只读 fresh evidence。
            port.emit(
                run,
                TOOL_STARTED,
                {"tool_call_id": tool_call_id, "name": "execute"},
                execution_id=run.root_execution_ref.execution_id,
                agent_id="verify",
                compose_scope=scope,
            )
            request = VerificationRequest(
                command=command,
                label=label,
                resource_key=resource_key,
                approve=lambda description, c=command, s=scope: self._approve_verify(
                    run, port, state, description, c, compose_scope=s
                ),
            )
            try:
                evidence = await verification.run(request)
            except VerificationError as exc:
                blocked_count += 1
                port.emit(
                    run,
                    TOOL_COMPLETED,
                    {
                        "tool_call_id": tool_call_id,
                        "result": {
                            "content": f"blocked: {exc.code}"[:500],
                            "is_error": True,
                            "truncated": False,
                            "original_bytes": 0,
                        },
                    },
                    execution_id=run.root_execution_ref.execution_id,
                    agent_id="verify",
                    compose_scope=scope,
                )
                self._emit_stage_summary(
                    run,
                    port,
                    stage="verify",
                    attempt=attempt,
                    status="blocked",
                    text=summarize_verify(
                        commands=len(items) + 1,
                        passed=passed_count,
                        failed=0,
                        blocked=blocked_count,
                        failed_label=label,
                    ),
                    execution_id=run.root_execution_ref.execution_id,
                    agent_id="verify",
                    compose_scope=scope,
                )
                # 策略拒绝/用户拒绝/后端缺失不可修复：直接 blocked。
                return await self._apply(
                    run,
                    port,
                    state,
                    ComposeEvent.BLOCK,
                    reason=f"验证命令被拒绝：{exc.code}",
                )
            validate_verification_evidence(
                {
                    "command": evidence.command,
                    "working_dir": evidence.working_dir,
                    "started_at_ms": evidence.started_at_ms,
                    "finished_at_ms": evidence.finished_at_ms,
                    "exit_code": evidence.exit_code,
                    "output_digest": evidence.output_digest,
                    "output_summary": evidence.output_summary,
                    "truncated": evidence.truncated,
                }
            )
            evidence_row = make_artifact(
                ArtifactKind.VERIFICATION,
                run_id=run.ref.run_id,
                source_execution_id=source_execution_id,
                created_at_ms=self._services.now_ms(),
                payload={
                    "command": evidence.command,
                    "working_dir": evidence.working_dir,
                    "started_at_ms": evidence.started_at_ms,
                    "finished_at_ms": evidence.finished_at_ms,
                    "exit_code": evidence.exit_code,
                    "output_digest": evidence.output_digest,
                    "output_summary": evidence.output_summary,
                    "truncated": evidence.truncated,
                },
            )
            await self._store.save_artifact(evidence_row)
            evidence_anchor_id = evidence_row.artifact_id
            passed = evidence.exit_code == 0
            if passed:
                passed_count += 1
            items.append(
                EvidenceItem(
                    label=label,
                    status=EvidenceStatus.PASSED if passed else EvidenceStatus.FAILED,
                )
            )
            summary_text = str(evidence.output_summary or "")[:500]
            port.emit(
                run,
                TOOL_COMPLETED,
                {
                    "tool_call_id": tool_call_id,
                    "result": {
                        "content": summary_text,
                        "is_error": not passed,
                        "truncated": bool(evidence.truncated),
                        "original_bytes": len(summary_text.encode("utf-8")),
                    },
                },
                execution_id=run.root_execution_ref.execution_id,
                agent_id="verify",
                compose_scope=scope,
            )
            self._queue_activity_record(
                run,
                kind="tool_terminal",
                label="execute",
                status="failed" if not passed else "completed",
                bounded_text=summary_text,
                compose_scope=scope,
                execution_id=run.root_execution_ref.execution_id,
                agent_id="verify",
                event_sequence=int(getattr(run, "sequence", 0) or 0),
            )
            self._emit_stage_summary(
                run,
                port,
                stage="verify",
                attempt=attempt,
                status="passed" if passed else "failed",
                text=summarize_verify(
                    commands=len(items),
                    passed=passed_count,
                    failed=0 if passed else 1,
                    blocked=blocked_count,
                    failed_label="" if passed else label,
                ),
                execution_id=run.root_execution_ref.execution_id,
                agent_id="verify",
                compose_scope=scope,
            )
            if not passed:
                failed_command = command
                break
        if not failed_command:
            return await self._apply(
                run,
                port,
                state,
                ComposeEvent.VERIFY_PASS,
                evidence_id=evidence_anchor_id,
                evidence=tuple(items),
            )
        # 失败创建来源明确的 fix task；不能修改原 acceptance。
        fix_task = ComposeTask(
            id=f"fix-verify-{state.verify_fix_round + 1}",
            title=f"修复验证失败：{failed_command[:60]}",
            kind=ChangeKind.BUG,
            acceptance=f"验证命令通过：{failed_command[:120]}",
            verification_commands=(failed_command,),
        )
        return await self._apply(
            run,
            port,
            state,
            ComposeEvent.VERIFY_FAIL,
            evidence=tuple(items),
            fix_tasks=(fix_task,),
        )

    async def _approve_verify(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        description: str,
        command: str,
        *,
        compose_scope: Mapping[str, object] | None = None,
    ) -> bool:
        """验证命令的审批弹窗；approve 语义与工具审批一致。"""
        command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:8]
        request_id = f"compose-verify-{state.revision}-{command_digest}"
        result = await port.request_approval(
            run,
            request_id=request_id,
            interrupt_id=request_id,
            description=description,
            decisions=["approve_once", "reject"],
            action_requests=[
                {
                    "name": "execute",
                    "args": {"command": command},
                    "description": description,
                }
            ],
            execution_id=run.root_execution_ref.execution_id,
            agent_id="verify",
            compose_scope=compose_scope,
        )
        value = result.value if isinstance(result.value, Mapping) else {}
        return str(value.get("decision") or "") == "approve_once"

    async def _review(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
    ) -> ComposeRunState:
        """双轴独立 Review；Required finding 回到 Build→Verify→Review。"""
        understanding = await self._load_understanding(run, state)
        plan = await self._load_plan(run, state)
        task_results = await self._load_task_results(run)
        evidence = await self._load_verification_evidence(run, state)
        verification = self._services.verification
        if verification is None:
            raise ComposeWorkflowError(
                "COMPOSE_REVIEW_INPUT_UNAVAILABLE",
                "workspace change capture unavailable",
            )
        profile = run.preparation.agent_engine_profile
        resource_key = (
            profile.sandbox_config_fingerprint
            if profile is not None
            else "compose-default"
        )
        try:
            workspace_changes = await verification.capture_workspace_changes(
                resource_key
            )
        except VerificationError as exc:
            raise ComposeWorkflowError(
                "COMPOSE_REVIEW_INPUT_UNAVAILABLE",
                exc.code,
            ) from exc

        requirement = await self._run_reviewer(
            run, port, state, "requirement-reviewer", understanding, plan,
            task_results, evidence, workspace_changes,
        )
        code = await self._run_reviewer(
            run, port, state, "code-reviewer", understanding, plan,
            task_results, evidence, workspace_changes,
        )
        report_payload = {
            "requirement_verdict": requirement["verdict"],
            "code_verdict": code["verdict"],
            "findings": requirement["findings"] + code["findings"],
        }
        try:
            report = validate_review_report(report_payload)
        except ValueError as exc:
            return await self._fail(
                run, port, state, "COMPOSE_ARTIFACT_INVALID", str(exc)
            )
        required = [
            finding
            for finding in report.findings
            if finding.severity in (FindingSeverity.CRITICAL, FindingSeverity.REQUIRED)
        ]
        if (
            report.requirement_verdict != "pass"
            or report.code_verdict != "pass"
        ) and not required:
            return await self._fail(
                run,
                port,
                state,
                "COMPOSE_ARTIFACT_INVALID",
                "failed reviewer verdict requires a Critical or Required finding",
            )
        # 终态摘要只使用本轮实际 workspace snapshot 与已校验 Review report，
        # 不依赖 Builder 自报路径，也不让 UI 再拼一套事实。
        self._changed_file_count = sum(
            1 for line in workspace_changes.status_summary.splitlines() if line.strip()
        )
        self._review_verdict = (
            report.requirement_verdict,
            report.code_verdict,
        )
        self._unresolved_risks = tuple(
            finding.message[:160] for finding in report.findings[:5]
        )
        report_row = make_artifact(
            ArtifactKind.REVIEW,
            run_id=run.ref.run_id,
            source_execution_id=f"review-{state.revision}",
            created_at_ms=self._services.now_ms(),
            payload={
                "requirement_verdict": report.requirement_verdict,
                "code_verdict": report.code_verdict,
                "findings": [
                    {
                        "axis": finding.axis.value,
                        "severity": finding.severity.value,
                        "message": finding.message,
                        "location": finding.location,
                    }
                    for finding in report.findings
                ],
            },
        )
        await self._store.save_artifact(report_row)
        if required:
            fix_tasks = tuple(
                ComposeTask(
                    id=f"fix-review-{state.review_fix_round + 1}-{index + 1}",
                    title=f"修复评审发现：{finding.message[:60]}",
                    kind=ChangeKind.BUG,
                    acceptance=f"评审发现已修复：{finding.message[:120]}",
                    verification_commands=tuple(
                        command
                        for task in plan.tasks
                        for command in task.verification_commands
                    ),
                )
                for index, finding in enumerate(required)
            )
            return await self._apply(
                run,
                port,
                state,
                ComposeEvent.REVIEW_FAIL,
                report_id=report_row.artifact_id,
                fix_tasks=fix_tasks,
            )
        return await self._apply(
            run,
            port,
            state,
            ComposeEvent.REVIEW_PASS,
            report_id=report_row.artifact_id,
        )

    async def _run_reviewer(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        stage: str,
        understanding: UnderstandingArtifact,
        plan: PlanArtifact,
        task_results: tuple[dict[str, object], ...],
        evidence: tuple[dict[str, object], ...],
        workspace_changes: Any,
    ) -> dict[str, object]:
        """运行一个 Reviewer；schema invalid 只结构化重试一次。"""
        axis = "requirement" if stage == "requirement-reviewer" else "code"
        schema_retries = 0
        while True:
            if port.is_cancelled(run):
                raise asyncio.CancelledError
            pack = build_review_pack(
                axis=axis,
                user_request=run.message,
                revision=state.revision,
                method_asset=self._method("code-review"),
                understanding=understanding,
                plan=plan,
                task_results=task_results,
                evidence=evidence,
                workspace_status=workspace_changes.status_summary,
                workspace_diff=workspace_changes.diff,
                workspace_root=self._services.workspace_root,
            )
            try:
                result = await self._run_stage(
                    run,
                    port,
                    state,
                    stage,
                    pack.render(),
                    attempt=schema_retries + 1,
                )
                raw = result.output if isinstance(result.output, Mapping) else {}
                parsed = self._parse_reviewer_axis(raw, axis)
                findings = parsed.get("findings", [])
                self._emit_stage_summary(
                    run,
                    port,
                    stage=stage,
                    attempt=schema_retries + 1,
                    status="passed" if parsed.get("verdict") == "pass" else "failed",
                    text=summarize_review(
                        requirement_verdict=(
                            str(parsed.get("verdict"))
                            if axis == "requirement"
                            else "n/a"
                        ),
                        code_verdict=(
                            str(parsed.get("verdict")) if axis == "code" else "n/a"
                        ),
                        findings=findings,
                    ),
                    execution_id=result.execution_id,
                    agent_id=result.agent_id,
                    compose_scope=getattr(result, "compose_scope", None),
                )
                return parsed
            except ValueError as exc:
                self._emit_stage_summary(
                    run,
                    port,
                    stage=stage,
                    attempt=schema_retries + 1,
                    status="failed",
                    text=summarize_stage_failure(str(exc)),
                    execution_id=getattr(exc, "execution_id", None),
                    agent_id=stage,
                    compose_scope=getattr(exc, "compose_scope", None),
                )
                schema_retries += 1
                if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                    raise ComposeWorkflowError(
                        "COMPOSE_ARTIFACT_INVALID", str(exc)
                    ) from exc

    def _parse_reviewer_axis(
        self, raw: Mapping[str, object], axis: str
    ) -> dict[str, object]:
        """校验单个 Reviewer 的输出：verdict 白名单 + findings 结构。"""
        verdict = raw.get("verdict")
        if verdict not in {"pass", "fail"}:
            raise ValueError(f"{axis} reviewer verdict must be pass or fail")
        raw_findings = raw.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValueError("reviewer findings must be a list")
        findings: list[dict[str, object]] = []
        for finding in raw_findings:
            if not isinstance(finding, Mapping):
                raise ValueError("reviewer finding must be an object")
            severity = finding.get("severity")
            if severity not in {
                FindingSeverity.CRITICAL.value,
                FindingSeverity.REQUIRED.value,
                FindingSeverity.OPTIONAL.value,
                FindingSeverity.NIT.value,
            }:
                raise ValueError("finding severity must be known")
            message = finding.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("finding message must be non-empty")
            findings.append(
                {
                    "axis": axis,
                    "severity": severity,
                    "message": message[:2_000],
                    "location": str(finding.get("location", ""))[:500],
                }
            )
        if verdict == "fail" and not any(
            finding["severity"]
            in {FindingSeverity.CRITICAL.value, FindingSeverity.REQUIRED.value}
            for finding in findings
        ):
            raise ValueError(
                f"{axis} reviewer fail verdict requires a Critical or Required finding"
            )
        return {"verdict": str(verdict), "findings": findings}

    async def _load_task_results(
        self, run: Any
    ) -> tuple[dict[str, object], ...]:
        """读取全部 TaskResult artifact，供 Reviewer 消费有界 diff 摘要。"""
        artifacts = await self._store.list_artifacts(run.ref.run_id)
        results: list[dict[str, object]] = []
        for row in artifacts:
            if row.kind is not ArtifactKind.TASK_RESULT:
                continue
            payload = dict(row.payload)
            results.append(
                {
                    "task_id": str(payload.get("task_id", "")),
                    "changed_paths": payload.get("changed_paths", ()),
                    "focused_test_evidence": str(
                        payload.get("focused_test_evidence", "")
                    ),
                }
            )
        return tuple(results)

    async def _load_verification_evidence(
        self, run: Any, state: ComposeRunState
    ) -> tuple[dict[str, object], ...]:
        """沿当前状态的 anchor 只读取本轮 Verification evidence。"""
        anchor_id = state.verification_evidence_id
        if not anchor_id:
            raise ComposeWorkflowError(
                "COMPOSE_WORKFLOW_STATE_INVALID",
                "verification evidence anchor missing",
            )
        anchor = await self._store.load_artifact(run.ref.run_id, anchor_id)
        if anchor is None or anchor.kind is not ArtifactKind.VERIFICATION:
            raise ComposeWorkflowError(
                "COMPOSE_WORKFLOW_STATE_INVALID",
                "verification evidence anchor unavailable",
            )
        artifacts = await self._store.list_artifacts(run.ref.run_id)
        evidence: list[dict[str, object]] = []
        for row in artifacts:
            if (
                row.kind is not ArtifactKind.VERIFICATION
                or row.source_execution_id != anchor.source_execution_id
            ):
                continue
            payload = dict(row.payload)
            evidence.append(
                {
                    "command": str(payload.get("command", "")),
                    "exit_code": payload.get("exit_code", -1),
                    "output_digest": str(payload.get("output_digest", "")),
                    "output_summary": str(payload.get("output_summary", ""))[:300],
                }
            )
        return tuple(evidence)

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
                result = await self._run_stage(
                    run, port, state, "understand", pack.render()
                )
                raw = result.output if isinstance(result.output, Mapping) else {}
                artifact = validate_understanding_artifact(raw)
            except ValueError as exc:
                self._emit_stage_summary(
                    run,
                    port,
                    stage="understand",
                    attempt=state.stage_attempts.get(ComposeStage.UNDERSTAND, 1),
                    execution_id=getattr(exc, "execution_id", None),
                    status="failed",
                    text=summarize_stage_failure(str(exc)),
                    compose_scope=getattr(exc, "compose_scope", None),
                )
                schema_retries += 1
                if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                    return await self._fail(
                        run, port, state, "COMPOSE_ARTIFACT_INVALID", str(exc)
                    )
                state = await self._apply(run, port, state, ComposeEvent.STAGE_RETRY)
                continue
            self._emit_stage_summary(
                run,
                port,
                stage="understand",
                attempt=state.stage_attempts.get(ComposeStage.UNDERSTAND, 1),
                execution_id=result.execution_id,
                status="passed",
                text=summarize_understanding(artifact),
                agent_id=result.agent_id,
                compose_scope=getattr(result, "compose_scope", None),
            )
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
            return await self._apply(
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
                result = await self._run_stage(run, port, state, "plan", pack.render())
                raw = result.output if isinstance(result.output, Mapping) else {}
                plan = validate_plan_artifact(raw)
            except ValueError as exc:
                self._emit_stage_summary(
                    run,
                    port,
                    stage="plan",
                    attempt=state.stage_attempts.get(ComposeStage.PLAN, 1),
                    execution_id=getattr(exc, "execution_id", None),
                    status="failed",
                    text=summarize_stage_failure(str(exc)),
                    compose_scope=getattr(exc, "compose_scope", None),
                )
                schema_retries += 1
                if schema_retries > SCHEMA_INVALID_RETRY_ALLOWED:
                    return await self._fail(
                        run, port, state, "COMPOSE_ARTIFACT_INVALID", str(exc)
                    )
                state = await self._apply(run, port, state, ComposeEvent.STAGE_RETRY)
                continue
            self._emit_stage_summary(
                run,
                port,
                stage="plan",
                attempt=state.stage_attempts.get(ComposeStage.PLAN, 1),
                execution_id=result.execution_id,
                status="passed",
                text=summarize_plan(plan),
                agent_id=result.agent_id,
                compose_scope=getattr(result, "compose_scope", None),
            )
            artifact_row = make_artifact(
                ArtifactKind.PLAN,
                run_id=run.ref.run_id,
                source_execution_id=result.execution_id,
                created_at_ms=self._services.now_ms(),
                payload=_plan_payload(plan),
            )
            await self._store.save_artifact(artifact_row)
            return await self._apply(
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
        """整体方案门禁（workflow question）：批准 / 携 feedback 修改 / 取消。"""
        plan = await self._load_plan(run, state)
        interrupt_id = f"compose-plan-{state.revision}"
        result = await port.request_question(
            run,
            request_id=interrupt_id,
            interrupt_id=interrupt_id,
            questions=[
                {
                    "id": "question-1",
                    "question": (
                        "请批准、修改或取消整体方案。\n"
                        + _plan_summary(plan)
                        + "\n选择「批准」或「取消」；要修改方案，请直接输入修改意见。"
                    ),
                    "header": "整体方案确认",
                    "body": "",
                    "options": [
                        {
                            "label": "批准",
                            "value": "approve",
                            "description": "按当前方案继续进入构建阶段",
                        },
                        {
                            "label": "取消",
                            "value": "cancel",
                            "description": "终止本次 Compose 执行",
                        },
                    ],
                    "multi_select": False,
                    "allow_other": True,
                }
            ],
        )
        if result.expired:
            raise ComposeWorkflowError(
                "COMPOSE_INTERACTION_UNAVAILABLE",
                "Plan confirmation expired or could not reach the active client",
            )
        value = result.value if isinstance(result.value, Mapping) else {}
        answers_by_id = value.get("answers", {})
        raw = (
            answers_by_id.get("question-1", [])
            if isinstance(answers_by_id, Mapping)
            else []
        )
        answer = str(raw[0]) if isinstance(raw, list) and raw else ""
        if answer == "approve":
            return await self._apply(
                run, port, state, ComposeEvent.PLAN_APPROVE, tasks=plan.tasks
            )
        if answer == "cancel":
            return await self._apply(run, port, state, ComposeEvent.PLAN_CANCEL)
        if answer.strip():
            # 其它输入即修改意见（必须携带 feedback 才能回到 Plan）。
            self._feedback = answer
            return await self._apply(run, port, state, ComposeEvent.PLAN_REVISE)
        raise ComposeWorkflowError(
            "COMPOSE_INTERACTION_UNAVAILABLE",
            "Plan confirmation returned no answer",
        )

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
        if result.expired:
            raise ComposeWorkflowError(
                "COMPOSE_INTERACTION_UNAVAILABLE",
                "Understanding questions expired or could not reach the active client",
            )
        answers_by_id = result.value.get("answers", {}) if isinstance(result.value, Mapping) else {}
        answers: list[tuple[str, str]] = []
        for index, decision in enumerate(decisions):
            raw = answers_by_id.get(f"question-{index + 1}", []) if isinstance(answers_by_id, Mapping) else []
            answer = str(raw[0]) if isinstance(raw, list) and raw else _EMPTY_ANSWER_MARK
            answers.append((decision, answer))
        return tuple(answers)

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
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        stage: str,
        task: str,
        *,
        task_id: str | None = None,
        task_title: str | None = None,
        attempt: int | None = None,
    ) -> Any:
        """启动一次 fresh Managed stage execution；基础设施失败收敛稳定错误码。

        为每次 invocation 创建稳定 activity scope，并由 HostStageObserver
        把 capture_only stream 事件投影为带 child provenance 的 scoped Event。
        """
        profile = run.preparation.agent_engine_profile
        # Activity scope 与状态机 stage 都从固定内置 RoleBindingRegistry 解析；
        # 不能让未知/Plugin role 静默降级为 build。
        compose_stage = compose_scope_stage(stage)
        stage_key = ComposeStage(compose_stage)
        resolved_attempt = (
            max(1, int(attempt))
            if attempt is not None
            else max(1, int(state.stage_attempts.get(stage_key, 1) or 1))
        )
        # invocation_id 在 StageRequest 内生成；先用临时 id 占位 activity，
        # 真正 activity_id 在 request 创建后重写 scope。
        request = StageRequest(
            stage=stage,
            task=task,
            parent_ref=run.root_execution_ref,
            profile_key=profile.profile_key if profile is not None else "",
            cancellation_token=run.cancellation_token,
        )
        scope = make_activity_scope(
            stage_agent_id=stage,
            attempt=resolved_attempt,
            invocation_id=request.invocation_id,
            task_id=task_id,
            task_title=task_title,
        )
        request.compose_scope = scope
        observer = _HostStageObserver(
            run=run,
            port=port,
            compose_scope=scope,
            record_activity=lambda **kwargs: self._queue_activity_record(
                run,
                bounded_text=None,
                compose_scope=scope,
                event_sequence=int(getattr(run, "sequence", 0) or 0),
                **kwargs,
            ),
        )
        try:
            result = await self._services.stage_agent.run(request, observer)
            # 供 compose.summary 复用同一 activity scope，避免 live/摘要分叉。
            setattr(result, "compose_scope", scope)
            return result
        except ValueError as exc:
            # 把最近 child provenance 挂到异常上，便于失败摘要带 scope。
            if observer.execution_id is not None:
                setattr(exc, "execution_id", observer.execution_id)
            setattr(exc, "compose_scope", scope)
            # schema-invalid 结构化重试路径由各阶段循环处理。
            raise
        except ComposeWorkflowError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 原始异常文本不越过 wire：只保留稳定错误码与异常类型名，
            # AgentDelegationError 额外带其稳定 code 便于诊断。
            from harness_agent.runtime.agent_delegation import AgentDelegationError

            if isinstance(exc, AgentDelegationError):
                detail = f"{stage} stage failed: {exc.code}"
            else:
                detail = f"{stage} stage failed: {type(exc).__name__}"
            raise ComposeWorkflowError(
                "COMPOSE_STAGE_EXECUTION_FAILED", detail
            ) from exc

    def _emit_stage_summary(
        self,
        run: Any,
        port: Any,
        *,
        stage: str,
        attempt: int,
        status: str,
        text: str,
        execution_id: str | None = None,
        agent_id: str | None = None,
        compose_scope: Mapping[str, object] | None = None,
    ) -> None:
        """发布 Runtime 生成的有界 compose.summary（非 assistant 消息）。"""
        exec_id = execution_id or run.root_execution_ref.execution_id
        agent = agent_id or stage
        scope = (
            dict(compose_scope)
            if compose_scope is not None
            else make_activity_scope(
                stage_agent_id=stage,
                attempt=attempt,
                invocation_id=f"summary-{stage}-{attempt}",
            )
        )
        port.emit(
            run,
            COMPOSE_SUMMARY,
            {"status": status, "text": text[:1000]},
            execution_id=exec_id,
            parent_execution_id=run.root_execution_ref.execution_id,
            agent_id=agent,
            compose_scope=scope,
        )
        # 审计落盘失败 fail closed；不假装已保存。
        self._queue_activity_record(
            run,
            kind="summary",
            label=str(scope.get("stage") or stage),
            status=status,
            bounded_text=text[:1000],
            compose_scope=scope,
            execution_id=exec_id,
            agent_id=agent,
            event_sequence=int(getattr(run, "sequence", 0) or 0),
        )

    def _queue_activity_record(
        self,
        run: Any,
        *,
        kind: str,
        label: str,
        status: str,
        bounded_text: str | None,
        compose_scope: Mapping[str, object],
        execution_id: str | None,
        agent_id: str | None,
        event_sequence: int,
    ) -> None:
        """把 activity 记录挂到 workflow 实例，由异步 flush 写入 store。

        RunState 使用 slots，不能动态 setattr；因此用 workflow 侧缓冲。
        """
        if not hasattr(self, "_compose_activity_pending"):
            self._compose_activity_pending: list[dict[str, object]] = []
        self._compose_activity_pending.append(
            {
                "run_id": run.ref.run_id,
                "kind": kind,
                "label": label,
                "status": status,
                "bounded_text": bounded_text,
                "compose_scope": dict(compose_scope),
                "execution_id": execution_id,
                "agent_id": agent_id,
                "event_sequence": event_sequence,
            }
        )

    async def _flush_activity_records(self, run: Any) -> None:
        """将挂起的 activity 审计写入 store；失败 fail closed。"""
        pending = getattr(self, "_compose_activity_pending", None) or []
        if not pending:
            return
        self._compose_activity_pending = []
        persistence = getattr(run, "persistence", None)
        if persistence is None or not hasattr(persistence, "append_compose_activity"):
            return
        from harness_agent.threads.compose_activity_store import ComposeActivityRecord
        import time as _time

        for item in pending:
            scope = item["compose_scope"]  # type: ignore[assignment]
            if not isinstance(scope, Mapping):
                continue
            try:
                await persistence.append_compose_activity(
                    ComposeActivityRecord(
                        run_id=str(item.get("run_id") or run.ref.run_id),
                        event_sequence=int(item["event_sequence"]),  # type: ignore[arg-type]
                        activity_id=str(scope.get("activity_id") or "unknown"),
                        stage=str(scope.get("stage") or "build"),
                        attempt=max(1, int(scope.get("attempt") or 1)),
                        kind=str(item["kind"]),  # type: ignore[arg-type]
                        label=str(item["label"])[:200],
                        status=str(item["status"])[:64],
                        created_at_ms=int(_time.time() * 1000),
                        task_id=(
                            str(scope["task_id"])
                            if scope.get("task_id") is not None
                            else None
                        ),
                        task_title=(
                            str(scope["task_title"])[:200]
                            if scope.get("task_title") is not None
                            else None
                        ),
                        execution_id=(
                            str(item["execution_id"])
                            if item.get("execution_id") is not None
                            else None
                        ),
                        agent_id=(
                            str(item["agent_id"])
                            if item.get("agent_id") is not None
                            else None
                        ),
                        bounded_text=(
                            str(item["bounded_text"])
                            if item.get("bounded_text") is not None
                            else None
                        ),
                    )
                )
            except Exception as exc:
                raise ComposeWorkflowError(
                    "COMPOSE_ACTIVITY_PERSIST_FAILED",
                    type(exc).__name__,
                ) from exc

    def _method(self, stage: str) -> str:
        """读取私有方法资产；缺失直接 fail closed。"""
        asset = self._services.method_assets.get(stage)
        if not asset:
            raise ComposeWorkflowError(
                "COMPOSE_METHOD_ASSET_MISSING", f"method asset {stage} missing"
            )
        return asset

    async def _apply(
        self,
        run: Any,
        port: Any,
        state: ComposeRunState,
        event: ComposeEvent,
        **payload: Any,
    ) -> ComposeRunState:
        """应用一次合法 transition，持久化投影并发布 revision 递增的完整帧。"""
        next_state = ComposeStateMachine.apply(state, event, **payload)
        self._emit_state(run, port, next_state)
        await self._store.save_run(next_state)
        return next_state

    async def _record_final_summary(
        self, run: Any, port: Any, state: ComposeRunState
    ) -> None:
        """终态时把一条有界结果摘要写入 Transcript（invariant 9 的可见结果）。"""
        from harness_agent.threads.thread_persistence import TranscriptAppend

        summary = self._final_summary_text(state)
        if not summary:
            return
        port.append_transcript(
            run,
            TranscriptAppend(
                thread_id=run.ref.thread_id,
                record_id=f"run:{run.ref.run_id}:compose:summary",
                kind="assistant",
                content=summary,
                run_id=run.ref.run_id,
                execution_id=run.root_execution_ref.execution_id,
            ),
        )
        await port.flush_transcript(run)
        port.emit(run, CONTENT_DELTA, {"text": summary})

    def _final_summary_text(self, state: ComposeRunState) -> str:
        """生成终态摘要：阶段状态、任务/证据计数与评审结论，全部有界。"""
        projection = state.projection()
        stage_summary = " → ".join(
            f"{stage['id']}:{stage['status']}" for stage in projection["stages"]
        )
        tasks = projection["tasks"]
        passed = sum(1 for task in tasks if task["status"] == "passed")
        evidence = projection["evidence"]
        passed_evidence = sum(1 for item in evidence if item["status"] == "passed")
        lines = [
            f"Compose {state.status.value}：{stage_summary}",
            (
                f"改动文件：{self._changed_file_count if self._changed_file_count is not None else '未统计'}；"
                f"任务：{passed}/{len(tasks)} 通过；验证：{passed_evidence}/{len(evidence)} 通过"
            ),
        ]
        if self._review_verdict is None:
            lines.append("Review：未完成")
        else:
            requirement, code = self._review_verdict
            lines.append(f"Review：需求 {requirement}；代码 {code}")
        lines.append(
            "未解决风险："
            + ("；".join(self._unresolved_risks) if self._unresolved_risks else "无")
        )
        if state.blocked_reason:
            lines.append(f"阻塞：{state.blocked_reason[:300]}")
        return "\n".join(lines)[:1_000]

    def _emit_state(self, run: Any, port: Any, state: ComposeRunState) -> None:
        """发布有界 compose.state projection；不含 artifact 正文。"""
        port.emit(run, COMPOSE_STATE, state.projection())

    async def _fail(
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
        return await self._apply(run, port, state, ComposeEvent.FAIL)

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


class _HostStageObserver:
    """把 Stage stream signal 映射到 RunLifecyclePort 的 scoped Event / Interaction。"""

    def __init__(
        self,
        *,
        run: Any,
        port: Any,
        compose_scope: Mapping[str, object],
        record_activity: Callable[..., None] | None = None,
    ) -> None:
        self._run = run
        self._port = port
        self._scope = dict(compose_scope)
        self._record_activity = record_activity
        self.execution_id: str | None = None
        self.parent_execution_id: str | None = None
        self.agent_id: str | None = None

    def bind_execution(
        self,
        *,
        execution_id: str,
        parent_execution_id: str | None,
        agent_id: str,
    ) -> None:
        self.execution_id = execution_id
        self.parent_execution_id = parent_execution_id
        self.agent_id = agent_id

    def emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        self._port.emit(
            self._run,
            event_type,
            dict(payload),
            execution_id=self.execution_id,
            parent_execution_id=self.parent_execution_id,
            agent_id=self.agent_id,
            compose_scope=self._scope,
        )

    def record_tool_terminal(self, *, tool_name: str, status: str) -> None:
        """把 Stage 工具终态写入有界 activity 审计（不保存参数/结果正文）。"""
        if self._record_activity is None:
            return
        self._record_activity(
            kind="tool_terminal",
            label=tool_name,
            status=status,
            execution_id=self.execution_id,
            agent_id=self.agent_id,
        )

    async def interact(self, request: StreamInteractionRequest) -> object:
        from harness_agent.runtime.interactions import InteractionRequest

        host_spec = InteractionRequest(
            request_id=request.request_id,
            type=request.type,
            payload=request.payload,
            interrupt_id=request.interrupt_id,
            questions=request.questions,
            action_count=request.action_count,
            serial_context=request.serial_context,
            execution_id=self.execution_id,
            parent_execution_id=self.parent_execution_id,
            agent_id=self.agent_id,
            compose_scope=self._scope,
        )
        if request.type == "approval":
            return await self._port.collect_serial_approvals(self._run, host_spec)
        result = await self._port.request_interaction(self._run, host_spec)
        return result.value
