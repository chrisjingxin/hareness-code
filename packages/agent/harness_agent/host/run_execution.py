"""Run execution adapter seam：Build/Compose 共用的 Run 内执行契约。

RunCoordinator 保持唯一拥有受理、owner、busy、取消、Interaction、sequence、
Transcript 和终态；ManagedAgentExecutor 负责其已取得 runtime 的 release。
本模块的 adapter 只通过 RunLifecyclePort 与生命周期通信。adapter 不能分配
sequence、不能发终态事件、不能绕过共享取消 token，也不能在成功路径外自行结束
Run。

LangGraph stream 解释、Tool 关联与 Interaction resume 已收敛到
``runtime.execution_stream``；本模块只负责 Host 边界：Transcript、
Skill/Runtime 准备、以及把 stream signal 投影为 root Event。
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from harness_agent.compose.engine_services import EngineDriverServices
from harness_agent.runtime.execution_stream import (
    CONTENT_DELTA,
    ExecutionSignal,
    ExecutionStreamError,
    MAX_TOOL_PAYLOAD_BYTES,
    REASONING_DELTA,
    RUN_PROGRESS,
    StreamInteractionRequest,
    StreamSession,
    TOOL_COMPLETED,
    TOOL_DELTA,
    TOOL_STARTED,
    bounded_json,
    content_text,
    ensure_model_round_for_assistant,
    extract_interaction,
    message_text,
    resolve_tool_result_id,
    resolve_tool_stream_id,
    translate_stream_event,
    truncate_text,
)
from harness_agent.runtime.managed_agent_executor import (
    ManagedAgentExecutionError,
    ManagedAgentExecutor,
    ManagedAgentRequest,
    ManagedAgentResult,
)
from harness_agent.threads.thread_persistence import TranscriptAppend

if TYPE_CHECKING:
    from harness_agent.host.run_coordinator import (
        RunRuntime,
        RunState,
    )
    from harness_agent.runtime.interactions import InteractionRequest, InteractionResult

# 事件名常量：Host / Protocol 词汇；与 execution_stream 对齐。
RUN_STARTED = "run.started"
SKILL_LOADED = "skill.loaded"
CONTEXT_UPDATED = "context.updated"
COMPOSE_STATE = "compose.state"
COMPOSE_SUMMARY = "compose.summary"
INTERACTION_RESOLVED = "interaction.resolved"
RUN_COMPLETED = "run.completed"
RUN_CANCELLED = "run.cancelled"
RUN_FAILED = "run.failed"

# 兼容旧测试与 coordinator 的 re-export。
__all__ = [
    "MAX_TOOL_PAYLOAD_BYTES",
    "RUN_STARTED",
    "RUN_PROGRESS",
    "SKILL_LOADED",
    "CONTENT_DELTA",
    "REASONING_DELTA",
    "TOOL_STARTED",
    "TOOL_DELTA",
    "TOOL_COMPLETED",
    "CONTEXT_UPDATED",
    "COMPOSE_STATE",
    "INTERACTION_RESOLVED",
    "RUN_COMPLETED",
    "RUN_CANCELLED",
    "RUN_FAILED",
    "AdapterOutcome",
    "BuildRunAdapter",
    "ComposeRunAdapter",
    "RunExecutionAdapter",
    "RunLifecyclePort",
]


def _run_error(code: str, message: str | None = None) -> Exception:
    """延迟构造 RunError，避免 run_execution ↔ run_coordinator 的模块导入环。"""
    from harness_agent.host.run_coordinator import RunError

    return RunError(code, message)


class RunLifecyclePort(Protocol):
    """RunCoordinator 暴露给 execution adapter 的最小受控能力。

    adapter 只能：发非终态 typed signal、请求 Interaction、刷新 Transcript、
    读取取消状态、解析 Runtime，并把 root execution start 交给 Managed
    executor 的 callback。终态、sequence 和资源释放不在 adapter 接口内。
    """

    def emit(
        self,
        run: RunState,
        event_type: str,
        payload: Mapping[str, object],
        *,
        execution_id: str | None = None,
        parent_execution_id: str | None = None,
        agent_id: str | None = None,
        compose_scope: Mapping[str, object] | None = None,
    ) -> None: ...

    def is_cancelled(self, run: RunState) -> bool: ...

    def mark_running(self, run: RunState) -> None: ...

    async def start_execution(self, run: RunState) -> None: ...

    def append_transcript(self, run: RunState, record: TranscriptAppend) -> None: ...

    async def resolve_runtime(self, run: RunState) -> RunRuntime: ...

    async def request_interaction(
        self, run: RunState, spec: InteractionRequest
    ) -> InteractionResult: ...

    async def request_question(
        self,
        run: RunState,
        *,
        request_id: str,
        interrupt_id: str,
        questions: list[dict[str, object]],
    ) -> InteractionResult: ...

    async def request_approval(
        self,
        run: RunState,
        *,
        request_id: str,
        interrupt_id: str,
        description: str,
        decisions: list[str],
        action_requests: list[dict[str, object]],
        execution_id: str | None = None,
        parent_execution_id: str | None = None,
        agent_id: str | None = None,
        compose_scope: Mapping[str, object] | None = None,
    ) -> InteractionResult: ...

    async def collect_serial_approvals(
        self, run: RunState, spec: InteractionRequest
    ) -> dict[str, object]: ...

    def drain_context_updates(self, run: RunState) -> None: ...

    async def flush_transcript(self, run: RunState) -> None: ...


class RunExecutionAdapter(Protocol):
    """一次 Run 的执行策略；按 run.start.mode 选择，与 coordinator 解耦。

    成功返回 None 时由 RunCoordinator 发默认 completed 终态；返回
    AdapterOutcome 时 coordinator 按提议收敛终态。adapter 不得自己发终态。
    """

    async def execute(
        self, run: RunState, port: RunLifecyclePort
    ) -> AdapterOutcome | None: ...


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    """adapter 提议的终态；RunCoordinator 是唯一终态 owner。"""

    status: Literal["completed", "failed", "cancelled"]
    code: str | None = None
    message: str = ""
    retryable: bool = False


class BuildRunAdapter:
    """Build 路径的执行 adapter。

    负责 Skill loaded 与 root Transcript/Event 投影。runtime lease、LangGraph
    输入、shared execution stream、Interaction resume 和 release 全部委托给
    ManagedAgentExecutor；adapter 不再读取 graph 或管理 resume loop。
    """

    async def execute(self, run: RunState, port: RunLifecyclePort) -> None:
        started_payload: dict[str, object] = {
            "resumed": False,
            "mode": run.start.mode,
            "skills_snapshot_id": run.preparation.skill_snapshot_id,
        }
        binding = run.preparation.execution_binding
        if binding is not None:
            started_payload["primary_model"] = binding.protocol_primary_model()
            started_payload["runtime_profile_id"] = binding.runtime_profile_id
        port.emit(run, RUN_STARTED, started_payload)
        port.mark_running(run)
        port.emit(run, RUN_PROGRESS, _run_progress_payload(run, "preparing"))

        loaded = run.preparation.requested_skill
        if loaded is not None:
            if loaded.snapshot_id != run.preparation.skill_snapshot_id:
                raise _run_error("RUN_PREPARATION_REQUESTED_SKILL_SNAPSHOT_MISMATCH")
            port.emit(
                run,
                SKILL_LOADED,
                {
                    "skill_id": loaded.record.skill_id,
                    "source": loaded.record.source,
                    "version": loaded.record.version,
                    "snapshot_id": loaded.snapshot_id,
                },
            )
            run.message = (
                f"The user explicitly selected Skill `{loaded.record.skill_id}`. "
                f"Read `/.harness/skills/{loaded.record.skill_id}/SKILL.md` with read_file before using it.\n\n"
                f"User request:\n{run.message}"
            )

        async def acquire_runtime():
            """将 Host runtime provider 收敛到 Managed executor 的唯一入口。"""
            return await port.resolve_runtime(run)

        async def start_execution(_execution_ref: str) -> None:
            """由 executor 触发 root registry 进入 running，adapter 不接触 registry。"""
            await port.start_execution(run)

        observer = _BuildStreamPorts(run=run, port=port)
        skill_snapshot_id = run.preparation.skill_snapshot_id

        def needs_user_decision(
            tool_name: str, tool_args: Mapping[str, object]
        ) -> bool:
            """中断动作若携带目录信任审批，禁止按并发安全自动放行。"""
            presentation = run.approval_presentations.lookup(tool_name, tool_args)
            return bool(presentation and presentation.get("kind") == "directory_trust")

        request = ManagedAgentRequest(
            execution_ref=run.root_execution_ref.execution_id,
            parent_execution_ref=None,
            run_id=run.ref.run_id,
            input=run.message,
            checkpoint_namespace=run.ref.thread_id,
            output_policy="passthrough",
            runtime_provider=acquire_runtime,
            is_cancelled=lambda: port.is_cancelled(run),
            idempotency_key=f"run:{run.ref.run_id}",
            execution_starter=start_execution,
            agent_spec=run.preparation.execution_binding,
            required_skill_snapshot_ids=(skill_snapshot_id,)
            if isinstance(skill_snapshot_id, str) and skill_snapshot_id
            else (),
            usage=run.usage,
            started_at=run.started_at,
            needs_user_decision=needs_user_decision,
        )
        try:
            await ManagedAgentExecutor().execute(request, observer)
        except ManagedAgentExecutionError as exc:
            if exc.code == "RUN_CANCELLED":
                raise asyncio.CancelledError from exc
            raise _run_error(exc.code, exc.message) from exc


class _BuildStreamPorts:
    """Build root 的 passthrough observer：投影 Event 并捕获根 Transcript。"""

    def __init__(
        self,
        *,
        run: RunState,
        port: RunLifecyclePort,
    ) -> None:
        self._run = run
        self._port = port

    def on_model_round(self) -> None:
        """由 executor 在 initial/resume 回合开始时投影 Build 进度。"""
        self._port.emit(self._run, RUN_PROGRESS, _run_progress_payload(self._run, "model"))

    async def on_execution_complete(self, result: ManagedAgentResult) -> None:
        """在 runtime release 前落盘 root Transcript，并完成 Build echo 投影。"""
        if not result.used_agent:
            self._port.emit(self._run, CONTENT_DELTA, {"text": result.final_content})
            _queue_assistant_transcript(self._run, result.final_content)
        if self._run.persistence is not None:
            _flush_assistant_transcript(self._run)
            await self._port.flush_transcript(self._run)
            await self._run.persistence.complete_run(self._run.ref.thread_id)
        self._port.drain_context_updates(self._run)

    def emit(self, signal: ExecutionSignal) -> None:
        self._port.emit(self._run, signal.type, dict(signal.payload))

    async def interact(self, request: StreamInteractionRequest) -> object:
        host_spec = _to_host_interaction(request)
        if request.type == "approval":
            return await self._port.collect_serial_approvals(self._run, host_spec)
        result = await self._port.request_interaction(self._run, host_spec)
        return result.value

    async def observe_message(self, chunk: object, session: StreamSession) -> bool:
        return _capture_transcript_on_session(self._run, session, chunk)

    async def after_tool_boundary(self) -> None:
        await self._port.flush_transcript(self._run)

    def on_stream_event(self) -> None:
        self._port.drain_context_updates(self._run)



class _ComposeStageObserver:
    """Compose Work Item stage 观察者：把 stage signal/Interaction 映射到 RunLifecyclePort。

    与 Build 不同：stage 不捕获根 Transcript、不落盘上下文更新；Approval
    走 collect_serial_approvals（auto-edit 下 Shell 等 HITL 工具仍需要 owner
    决策），Question 通道由 headless spec 关闭，仅保留兜底映射。
    """

    def __init__(self, *, run: RunState, port: RunLifecyclePort) -> None:
        self._run = run
        self._port = port
        self._execution_id: str | None = None
        self._agent_id: str | None = None

    def bind_execution(
        self,
        *,
        execution_id: str,
        parent_execution_id: str | None,
        agent_id: str,
    ) -> None:
        """记录 child provenance，供后续事件投影归属。"""
        self._execution_id = execution_id
        self._agent_id = agent_id

    def emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        """发送带 child provenance 的非终态事件。"""
        self._port.emit(
            self._run,
            event_type,
            dict(payload),
            execution_id=self._execution_id,
            agent_id=self._agent_id,
        )

    def record_tool_terminal(self, *, tool_name: str, status: str) -> None:
        """Work Item 路径不持久化 Tool 终态记录；保留 seam 一致性。"""
        return None

    async def interact(self, request: StreamInteractionRequest) -> object:
        """Approval 走串行审批通道；Question 兜底映射 owner 交互。"""
        host_spec = _to_host_interaction(request)
        if request.type == "approval":
            return await self._port.collect_serial_approvals(self._run, host_spec)
        result = await self._port.request_interaction(self._run, host_spec)
        return result.value

def _stream_session_for(run: RunState) -> StreamSession:
    """为 Build Run 取得跨 resume 复用的 stream session。

    usage dict 与 run.usage 共享引用，保证终态事件读到同一计数。
    """
    existing = run.stream_session
    if isinstance(existing, StreamSession):
        return existing
    session = StreamSession(run_id=run.ref.run_id, started_at=run.started_at)
    session.usage = run.usage
    run.stream_session = session
    return session


def _to_host_interaction(request: StreamInteractionRequest) -> InteractionRequest:
    """把 stream Interaction 映射为 Host InteractionRequest。"""
    from harness_agent.runtime.interactions import InteractionRequest

    return InteractionRequest(
        request_id=request.request_id,
        type=request.type,
        payload=request.payload,
        interrupt_id=request.interrupt_id,
        questions=request.questions,
        action_count=request.action_count,
        serial_context=request.serial_context,
    )


class ComposeRunAdapter:
    """Compose 工作模式的执行 adapter；由 ComposeWorkItemEngine 驱动 Work Item 流程。

    本 adapter 负责 host 边界：组装生产 ComposeTurnPorts、把 typed decision
    映射到 RunLifecyclePort.request_question、把 engine 结果映射为
    coordinator 可收敛的 AdapterOutcome 与 compose.work_item 事件。
    未注入 EngineDriverServices 时保持稳定失败空壳。
    """

    def __init__(self, services: EngineDriverServices | None = None) -> None:
        """保存 Host 提供的 engine 依赖（可为空壳）。"""
        self._services = services

    async def execute(
        self,
        run: RunState,
        port: RunLifecyclePort,
    ) -> AdapterOutcome | None:
        """执行一次 Work Item Turn 直到收敛；终态仍归 coordinator。"""
        if self._services is None:
            raise _run_error(
                "COMPOSE_ADAPTER_NOT_READY", "Compose mode is not available yet"
            )
        if run.persistence is None:
            raise _run_error(
                "COMPOSE_PERSISTENCE_REQUIRED",
                "Compose mode requires thread persistence",
            )
        # services 按 Run 组装且只被本 adapter 消费；此处绑定当前 Run/Port，
        # 让 stage 执行中的 Shell 审批等 Interaction 走 owner 交互通道。
        self._services.stage_observer = _ComposeStageObserver(run=run, port=port)
        from harness_agent.compose.document_store import ComposeDocumentStore
        from harness_agent.compose.engine_services import (
            ManagedGrillDriver,
            ManagedImplementDriver,
            ManagedPlanDriver,
            ManagedReportDriver,
            ManagedReviewDriver,
            ManagedSpecDriver,
            ManagedTurnIntentClassifier,
        )
        from harness_agent.compose.work_item_engine import (
            ComposeTurnOutcome,
            ComposeTurnPorts,
            ComposeTurnRequest,
            ComposeWorkItemEngine,
            ComposeWorkItemEngineError,
        )

        services = self._services
        engine = ComposeWorkItemEngine(
            ComposeTurnPorts(
                store=run.persistence.compose_work_item_store(),
                documents=ComposeDocumentStore(services.workspace_root),
                classifier=ManagedTurnIntentClassifier(services),
                interaction=_HostTypedDecisionPort(run, port),
                workspace_revision=self._workspace_revision,
                now_ms=services.now_ms,
                task_driver=ManagedGrillDriver(services),
                spec_driver=ManagedSpecDriver(services),
                plan_driver=ManagedPlanDriver(services),
                implement_driver=ManagedImplementDriver(services),
                verify_port=(
                    _WorkItemVerificationPort(services.verification, run=run, port=port)
                    if services.verification is not None
                    else None
                ),
                review_driver=ManagedReviewDriver(services),
                report_driver=ManagedReportDriver(services),
            )
        )
        request = ComposeTurnRequest(
            thread_id=run.ref.thread_id,
            run_id=run.ref.run_id,
            message=run.message,
            cancelled=port.is_cancelled(run),
        )
        try:
            result = await engine.execute_turn(request)
        except ComposeWorkItemEngineError as exc:
            raise _run_error(exc.code, str(exc)) from exc
        if result.work_item is not None:
            port.emit(
                run,
                "compose.work_item",
                {
                    "thread_id": run.ref.thread_id,
                    "work_item": _work_item_wire(result.work_item),
                },
            )
        if result.status is ComposeTurnOutcome.BLOCKED:
            return AdapterOutcome(
                status="failed",
                code="COMPOSE_WORK_ITEM_BLOCKED",
                message=result.work_item.blocked_reason or "Work Item 需要用户处理",
                retryable=True,
            )
        if result.status is ComposeTurnOutcome.RETRYABLE_FAILED:
            return AdapterOutcome(
                status="failed",
                code="COMPOSE_ACTIVITY_RETRYABLE_FAILED",
                message=result.pending_decision or "Compose Activity 执行失败，可重试",
                retryable=True,
            )
        return None

    async def _workspace_revision(self) -> str | None:
        """把当前 workspace 的 Git HEAD 作为证据新鲜度 revision。"""
        services = self._services
        if services is None or services.verification is None:
            return None
        return await services.verification.workspace_revision("compose-work-item")


class _HostTypedDecisionPort:
    """把引擎 typed decision 映射到 host 的 typed question 通道。"""

    def __init__(self, run: RunState, port: RunLifecyclePort) -> None:
        self._run = run
        self._port = port

    async def request_decision(self, request: object):
        """单个 typed question；回答形状与引擎 TypedDecisionResult 一致。"""
        from harness_agent.compose.work_item_engine import TypedDecisionResult

        result = await self._port.request_question(
            self._run,
            request_id=request.request_id,
            interrupt_id=request.interrupt_id,
            questions=[
                {
                    "id": request.question_id,
                    "question": request.body,
                    "header": request.header,
                    "body": "",
                    "options": [
                        {
                            "label": option.label,
                            "value": option.value,
                            "description": option.description,
                        }
                        for option in request.options
                    ],
                    "multi_select": False,
                    "allow_other": request.allow_other,
                }
            ],
        )
        return TypedDecisionResult(result.value, expired=result.expired)


class _WorkItemVerificationPort:
    """把 canonical VerificationPort 适配为 Work Item verify 端口。

    非白名单验证命令必须经 RunLifecyclePort.request_approval 走 owner
    审批通道；绝不静默放行或降级到其他执行器。
    """

    def __init__(self, verification: object, *, run: RunState, port: RunLifecyclePort) -> None:
        self._verification = verification
        self._run = run
        self._port = port

    async def run_command(self, command: str, *, work_item_id: str):
        """执行一条 required command 并返回 engine 期望的事实形状。"""
        from harness_agent.compose.activities.verify import VerificationCommandResult
        from harness_agent.compose.verification import VerificationRequest

        evidence = await self._verification.run(
            VerificationRequest(
                command=command,
                label=command[:80],
                resource_key=f"compose:{work_item_id}",
                approve=lambda description, c=command: self._approve(c, description),
            )
        )
        return VerificationCommandResult(
            command=command,
            exit_code=evidence.exit_code,
            output_digest=evidence.output_digest,
            execution_id=f"verify:{evidence.finished_at_ms}",
        )

    async def _approve(self, command: str, description: str) -> bool:
        """把审批弹窗映射为 owner 决策；仅 approve_once 视为批准。"""
        command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:8]
        request_id = f"compose-verify-{command_digest}"
        result = await self._port.request_approval(
            self._run,
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
        )
        value = result.value if isinstance(result.value, Mapping) else {}
        return str(value.get("decision") or "") == "approve_once"


def _work_item_wire(projection: object) -> dict[str, object]:
    """把引擎 projection 转换为 compose.work_item 事件 payload。"""
    return {
        "work_item_id": projection.work_item_id,
        "slug": projection.slug,
        "title": projection.title,
        "revision": projection.revision,
        "status": projection.status,
        "current_activity": projection.current_activity,
        "pending_decision": projection.pending_decision,
        "blocked_reason": projection.blocked_reason,
    }


def _capture_transcript_on_session(
    run: RunState,
    session: StreamSession,
    chunk: object,
) -> bool:
    """在 1 MiB wire 截断前收集完整助手/工具语义，不收集 delta 事件。"""
    try:
        return _capture_transcript_on_session_impl(run, session, chunk)
    except ExecutionStreamError as exc:
        raise _run_error(exc.code, exc.message) from exc


def _capture_transcript_on_session_impl(
    run: RunState,
    session: StreamSession,
    chunk: object,
) -> bool:
    """Transcript 捕获实现；Tool 关联失败以 ExecutionStreamError 抛出。"""
    chunk_type = type(chunk).__name__
    if chunk_type in {"AIMessage", "AIMessageChunk"}:
        ensure_model_round_for_assistant(session)
        full_tool_calls = getattr(chunk, "tool_calls", None)
        if chunk_type == "AIMessage" and isinstance(full_tool_calls, list) and full_tool_calls:
            for index, tool_call in enumerate(full_tool_calls):
                if not isinstance(tool_call, Mapping):
                    continue
                _capture_full_tool_call(run, session, tool_call, index=index, source_message=chunk)
        else:
            for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
                if not isinstance(tool_chunk, Mapping):
                    continue
                tool_id = resolve_tool_stream_id(
                    session, tool_chunk, source_message=chunk
                )
                name = tool_chunk.get("name")
                if name:
                    session.tool_names[tool_id] = str(name)
                _merge_assistant_tool_call(
                    run,
                    tool_id,
                    name=name,
                    arguments=tool_chunk.get("args"),
                    is_full=False,
                    call_type=tool_chunk.get("type"),
                )
        text = message_text(chunk)
        if not text and not run.assistant_tool_calls:
            return False
        if text:
            run.assistant_buffer.append(text)
        session.last_captured_message = chunk
        return False
    if chunk_type != "ToolMessage":
        return False
    _flush_assistant_transcript(run)
    tool_id = resolve_tool_result_id(session, chunk)
    run.pending_transcript.append(
        TranscriptAppend(
            thread_id=run.ref.thread_id,
            record_id=f"run:{run.ref.run_id}:tool:{tool_id}",
            kind="tool",
            content=content_text(getattr(chunk, "content", None)),
            run_id=run.ref.run_id,
            execution_id=run.root_execution_ref.execution_id,
            tool_call_id=tool_id,
            tool_name=session.tool_names.get(
                tool_id, str(getattr(chunk, "name", None) or "tool")
            ),
            tool_status=(
                "error" if getattr(chunk, "status", None) == "error" else "success"
            ),
        )
    )
    return True


def _queue_assistant_transcript(
    run: RunState,
    content: str,
    tool_calls: tuple[Mapping[str, object], ...] = (),
) -> None:
    """将一个完整助手回答排入当前 Run 的原子提交批次。"""
    if not content and not tool_calls:
        return
    run.assistant_turn_count += 1
    run.pending_transcript.append(
        TranscriptAppend(
            thread_id=run.ref.thread_id,
            record_id=f"run:{run.ref.run_id}:assistant:{run.assistant_turn_count}",
            kind="assistant",
            content=content,
            run_id=run.ref.run_id,
            execution_id=run.root_execution_ref.execution_id,
            tool_calls=tool_calls,
        )
    )


def _flush_assistant_transcript(run: RunState) -> None:
    """结束一个模型消息边界；只把非空完整文本转成助手记录。"""
    content = "".join(run.assistant_buffer)
    tool_calls = _finalize_assistant_tool_calls(run)
    if content or tool_calls:
        _queue_assistant_transcript(run, content, tool_calls)
    run.assistant_buffer.clear()


def _capture_full_tool_call(
    run: RunState,
    session: StreamSession,
    tool_call: Mapping[str, object],
    *,
    index: int,
    source_message: object | None = None,
) -> None:
    """捕获完整 AIMessage.tool_calls，不从 ToolMessage 反推参数。"""
    raw_id = tool_call.get("id")
    tool_id = resolve_tool_stream_id(
        session,
        {
            "index": tool_call.get("index", index),
            "id": raw_id,
            "name": tool_call.get("name"),
            "args": tool_call.get("args", tool_call.get("arguments")),
        },
        source_message=source_message,
    )
    name = tool_call.get("name")
    if name:
        session.tool_names[tool_id] = str(name)
    _merge_assistant_tool_call(
        run,
        tool_id,
        name=name,
        arguments=tool_call.get("args", tool_call.get("arguments")),
        is_full=True,
        call_type=tool_call.get("type"),
    )


def _merge_assistant_tool_call(
    run: RunState,
    tool_id: str,
    *,
    name: object,
    arguments: object,
    is_full: bool,
    call_type: object,
) -> None:
    """在当前模型回合内按稳定调用 ID 合并参数分片。"""
    entry = run.assistant_tool_calls.setdefault(
        tool_id,
        {
            "id": tool_id,
            "name": str(name or "tool"),
            "_argument_fragments": [],
            "_argument_invalid": False,
            "_full_arguments_present": False,
        },
    )
    if name:
        entry["name"] = str(name)
    if call_type:
        entry["type"] = str(call_type)
    if is_full:
        entry["_full_arguments_present"] = True
        entry["_full_arguments"] = arguments
        entry["_argument_fragments"] = []
        return
    if entry.get("_full_arguments_present") or arguments in (None, ""):
        return
    fragments = entry.setdefault("_argument_fragments", [])
    if isinstance(arguments, str):
        fragments.append(arguments)
    else:
        try:
            fragments.append(_canonical_tool_argument(arguments))
        except (TypeError, ValueError):
            fragments.append(repr(arguments))
            entry["_argument_invalid"] = True


def _finalize_assistant_tool_calls(
    run: RunState,
) -> tuple[Mapping[str, object], ...]:
    """将当前 assistant 的完整/分片参数定型为可恢复的 typed payload。"""
    calls: list[Mapping[str, object]] = []
    for entry in run.assistant_tool_calls.values():
        call: dict[str, object] = {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or "tool"),
        }
        if entry.get("type") is not None:
            call["type"] = str(entry["type"])
        if entry.get("_full_arguments_present"):
            _set_tool_call_arguments(call, entry.get("_full_arguments"), partial=False)
        else:
            fragments = entry.get("_argument_fragments")
            raw = "".join(str(fragment) for fragment in fragments or ())
            _set_tool_call_arguments(
                call,
                raw if raw else None,
                partial=not bool(entry.get("_argument_invalid")),
            )
        calls.append(call)
    run.assistant_tool_calls.clear()
    return tuple(calls)


def _set_tool_call_arguments(
    call: dict[str, object],
    value: object,
    *,
    partial: bool,
) -> None:
    """保留参数对象、原文和显式校验状态，供后续恢复层 fail closed。"""
    if value is None:
        call["arguments_status"] = "unavailable"
        return
    if isinstance(value, str):
        call["arguments_raw"] = value
        if not value:
            call["arguments_status"] = "unavailable"
            return
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            call["arguments_status"] = "partial" if partial else "invalid"
            call["arguments_error"] = type(exc).__name__
            return
    else:
        parsed = value
    try:
        encoded = _canonical_tool_argument(parsed)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if not isinstance(value, str):
            call["arguments_raw"] = repr(value)
        call["arguments_status"] = "invalid"
        call["arguments_error"] = type(exc).__name__
        return
    call["arguments"] = normalized
    call["arguments_json"] = encoded
    call["arguments_status"] = "valid" if isinstance(normalized, Mapping) else "invalid"


def _canonical_tool_argument(value: object) -> str:
    """以稳定 JSON 编码工具参数，避免把 provider 对象直接写入 Transcript。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _run_progress_payload(run: RunState, phase: str) -> dict[str, object]:
    """生成只包含事实阶段和活动时长的运行进度 payload。"""
    safe_phase = phase if phase in {"preparing", "model"} else "preparing"
    return {
        "phase": safe_phase,
        "elapsed_ms": max(0, round((time.monotonic() - run.started_at) * 1000)),
    }


def _bounded_json(value: object) -> object:
    """Host 侧 re-export；实现位于 execution_stream。"""
    return bounded_json(value)


# ---------------------------------------------------------------------------
# 测试兼容包装：既有 host 测试仍从 run_execution 导入这些符号。
# 关联状态统一走 StreamSession；不再维护第二条 translator 路径。
# ---------------------------------------------------------------------------


def _translate_stream_event(
    event: tuple[Any, ...], run: RunState
) -> list[tuple[str, dict[str, object]]]:
    """测试兼容：返回 (type, payload) 列表，内部使用共享 stream translator。"""
    session = _stream_session_for(run)
    try:
        return [
            (signal.type, dict(signal.payload))
            for signal in translate_stream_event(
                event, session, content_visibility="passthrough"
            )
        ]
    except ExecutionStreamError as exc:
        raise _run_error(exc.code, exc.message) from exc


def _extract_interaction(
    event: tuple[Any, ...],
    *,
    needs_user_decision: Callable[[str, Mapping[str, object]], bool] | None = None,
) -> tuple[Any, dict[str, object] | None]:
    """测试兼容：返回 Host InteractionRequest 或 None。"""
    request, auto = extract_interaction(
        event, needs_user_decision=needs_user_decision
    )
    if request is None:
        return None, auto
    return _to_host_interaction(request), auto


def _message_text(message: object) -> str:
    """测试兼容。"""
    return message_text(message)


def _capture_transcript_message(run: RunState, chunk: object) -> bool:
    """测试与 Build 兼容入口：自动绑定 stream session。"""
    return _capture_transcript_on_session(run, _stream_session_for(run), chunk)


def _truncate_text(value: str) -> tuple[str, bool, int]:
    """测试兼容。"""
    return truncate_text(value)


def _resume_value(spec: Any, response: object) -> dict[str, object]:
    """测试兼容：接受 Host InteractionRequest。"""
    from harness_agent.runtime.execution_stream import resume_value

    if isinstance(spec, StreamInteractionRequest):
        return resume_value(spec, response)
    stream_spec = StreamInteractionRequest(
        request_id=spec.request_id,
        type=spec.type,
        payload=spec.payload,
        interrupt_id=spec.interrupt_id,
        questions=spec.questions,
        action_count=spec.action_count,
        serial_context=spec.serial_context,
    )
    return resume_value(stream_spec, response)
