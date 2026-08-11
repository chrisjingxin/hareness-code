"""Run execution adapter seam：Build/Compose 共用的 Run 内执行契约。

RunCoordinator 保持唯一拥有受理、owner、busy、取消、Interaction、sequence、
Transcript、资源释放和终态；本模块的 adapter 只通过 RunLifecyclePort 与
生命周期通信。adapter 不能分配 sequence、不能发终态事件、不能绕过共享
取消 token，也不能在成功路径外自行结束 Run。

事件名常量与工具/Transcript 捕获辅助函数属于执行层词汇，因此与 adapter
同住本模块；RunCoordinator 从本模块导入它们，避免 coordinator 继续膨胀。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from harness_agent.threads.thread_persistence import TranscriptAppend
from harness_agent.compose.workflow import (
    ComposeOutcome,
    ComposeServices,
    ComposeWorkflow,
    ComposeWorkflowError,
)

if TYPE_CHECKING:
    from harness_agent.host.run_coordinator import (
        InteractionRequest,
        InteractionResult,
        RunRuntime,
        RunState,
    )

# 单条工具/交互 payload 的 1 MiB wire 上限；与 schema x-harness 同步。
MAX_TOOL_PAYLOAD_BYTES = 1 * 1024 * 1024

RUN_STARTED = "run.started"
RUN_PROGRESS = "run.progress"
SKILL_LOADED = "skill.loaded"
CONTENT_DELTA = "content.delta"
REASONING_DELTA = "reasoning.delta"
TOOL_STARTED = "tool.started"
TOOL_DELTA = "tool.delta"
TOOL_COMPLETED = "tool.completed"
CONTEXT_UPDATED = "context.updated"
COMPOSE_STATE = "compose.state"
INTERACTION_RESOLVED = "interaction.resolved"
RUN_COMPLETED = "run.completed"
RUN_CANCELLED = "run.cancelled"
RUN_FAILED = "run.failed"


def _run_error(code: str, message: str | None = None) -> Exception:
    """延迟构造 RunError，避免 run_execution ↔ run_coordinator 的模块导入环。"""
    from harness_agent.host.run_coordinator import RunError

    return RunError(code, message)


class RunLifecyclePort(Protocol):
    """RunCoordinator 暴露给 execution adapter 的最小受控能力。

    adapter 只能：发非终态 typed signal、请求 Interaction、刷新 Transcript、
    读取取消状态、解析 Runtime。终态、sequence 和资源释放不在此接口内。
    """

    def emit(self, run: RunState, event_type: str, payload: Mapping[str, object]) -> None: ...

    def is_cancelled(self, run: RunState) -> bool: ...

    def mark_running(self, run: RunState) -> None: ...

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
    """现有 Build 路径的执行 adapter：迁移 RunCoordinator 的历史 Build stream。

    行为与迁移前完全一致：先发 run.started/run.progress，处理显式 Skill，
    解析 Runtime 后流式调用主 Agent，期间把 Interaction 委托给 port，
    结束前按语义边界刷新 Transcript 并完成持久化 checkpoint。
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

        run.runtime = await port.resolve_runtime(run)
        if port.is_cancelled(run):
            raise asyncio.CancelledError
        if run.runtime.agent is None:
            port.emit(run, CONTENT_DELTA, {"text": run.message})
            _queue_assistant_transcript(run, run.message)
        else:
            resume: object | None = None
            while True:
                resume = await self._stream_agent(run, port, resume)
                if resume is None:
                    break

        if run.persistence is not None:
            _flush_assistant_transcript(run)
            await port.flush_transcript(run)
            await run.persistence.complete_run(run.ref.thread_id)
        port.drain_context_updates(run)

    async def _stream_agent(
        self,
        run: RunState,
        port: RunLifecyclePort,
        resume: object | None,
    ) -> object | None:
        """流式执行主 Agent 一个模型阶段，Interaction 全部经由 port 收敛。"""
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        runtime = run.runtime
        if runtime is None or runtime.agent is None:
            return None
        port.emit(run, RUN_PROGRESS, _run_progress_payload(run, "model"))
        if resume is not None and run.run_context is not None:
            # Interaction resume is an explicit non-initial model phase.  The
            # pressure middleware must not reinterpret the resume as a new
            # idle top-level Run merely because the user took time to answer.
            run.run_context.model_call_lifecycle.schedule("interaction_resume")
        stream_input: Any = (
            Command(resume=resume)
            if resume is not None
            else {"messages": [HumanMessage(content=run.message)]}
        )
        stream_kwargs: dict[str, Any] = {
            "config": runtime.graph_config(run.ref.thread_id),
            "stream_mode": ["messages", "updates"],
            "subgraphs": True,
        }
        if runtime.run_context is not None:
            stream_kwargs["context"] = runtime.run_context
        async for event in runtime.agent.astream(stream_input, **stream_kwargs):
            port.drain_context_updates(run)
            interaction, auto_resume = _extract_interaction(event)
            if auto_resume is not None:
                # 全部是并发安全工具，直接放行
                return auto_resume
            if interaction is not None:
                if interaction.type == "approval":
                    # 多工具串行审批：interrupt 只能整体 resume 一次，
                    # 由本地循环逐个收集决策，最后一次提交完整 decisions。
                    return await port.collect_serial_approvals(run, interaction)
                result = await port.request_interaction(run, interaction)
                return _resume_value(interaction, result.value)
            chunk = _message_stream_chunk(event)
            if chunk is not None:
                complete_tool = _capture_transcript_message(run, chunk)
                if complete_tool:
                    # ToolMessage 到达时，前置 assistant 与完整 tool 结果已经
                    # 越过内存 pending 边界；后续模型阶段即使阻塞也不应延迟
                    # 这一个幂等 typed lifecycle 批次的提交。
                    await port.flush_transcript(run)
            for event_type, payload in _translate_stream_event(event, run):
                port.emit(run, event_type, payload)
        _flush_assistant_transcript(run)
        await port.flush_transcript(run)
        _finish_model_round(run)
        return None


class ComposeRunAdapter:
    """Compose 工作模式的执行 adapter；由 ComposeWorkflow 驱动五阶段。

    Compose workflow 位于 harness_agent.compose（不反向依赖 host）；
    本 adapter 负责 host 边界：把 ComposeWorkflowError 映射为 RunError，
    把 ComposeOutcome 映射为 coordinator 可收敛的 AdapterOutcome。
    未注入 ComposeServices 时保持稳定失败空壳。
    """

    def __init__(self, services: ComposeServices | None = None) -> None:
        """保存 Host 提供的 workflow 依赖（可为空壳）。"""
        self._services = services

    async def execute(
        self,
        run: RunState,
        port: RunLifecyclePort,
    ) -> AdapterOutcome | None:
        """执行 Compose workflow 直到唯一终态提议；终态仍归 coordinator。"""
        if self._services is None:
            raise _run_error(
                "COMPOSE_ADAPTER_NOT_READY", "Compose mode is not available yet"
            )
        if run.persistence is None:
            raise _run_error(
                "COMPOSE_PERSISTENCE_REQUIRED",
                "Compose mode requires thread persistence",
            )
        from harness_agent.compose.state_machine import ComposeStateMachine

        store = run.persistence.compose_artifact_store()
        workflow = ComposeWorkflow(services=self._services, store=store)
        state = ComposeStateMachine.initial(run.ref.thread_id, run.ref.run_id)
        try:
            outcome = await workflow.run(run, port, state)
        except ComposeWorkflowError as exc:
            raise _run_error(exc.code, str(exc)) from exc
        return _adapter_outcome(outcome)


def _adapter_outcome(outcome: ComposeOutcome) -> AdapterOutcome:
    """把 compose-owned 终态提议映射为 adapter 提议。"""
    return AdapterOutcome(
        status=outcome.status,
        code=outcome.code,
        message=outcome.message,
        retryable=outcome.retryable,
    )


_CONCURRENCY_SAFE_TOOLS = frozenset({
    "ls", "read_file", "glob", "grep", "web_search",
    "lsp", "tool_search", "memory_search", "task_output",
    "ask_user", "write_todos", "memory_save",
    "enter_plan_mode", "exit_plan_mode",
})


def _is_concurrency_safe(tool_name: str) -> bool:
    """并发安全工具无需审批，可直接并行执行。"""
    return tool_name in _CONCURRENCY_SAFE_TOOLS


def _extract_interaction(
    event: tuple[Any, ...],
) -> tuple[InteractionRequest | None, dict[str, object] | None]:
    """从 DeepAgents updates 流提取首个 AskUser 或 HITL interrupt。

    返回 (InteractionRequest, None) 表示需要用户交互；
    返回 (None, dict) 表示全部并发安全工具，自动放行，dict 为 resume 值；
    返回 (None, None) 表示没有交互需要处理。
    """
    # 延迟导入以打破 run_execution ↔ run_coordinator 的模块导入环。
    from harness_agent.host.run_coordinator import InteractionRequest

    if len(event) == 3:
        namespace, stream_mode, data = event
        # Protocol v3 has no execution/provenance field for child graph
        # updates.  A non-empty namespace is therefore never a root
        # interaction request; treating it as one would surface a child
        # interrupt as a root approval/question.
        if namespace:
            return None, None
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return None, None
    if stream_mode != "updates" or not isinstance(data, Mapping):
        return None, None
    interrupts = data.get("__interrupt__")
    if not interrupts:
        return None, None
    interrupt = (interrupts if isinstance(interrupts, (list, tuple)) else [interrupts])[0]
    value = getattr(interrupt, "value", interrupt)
    interrupt_id = str(getattr(interrupt, "id", uuid.uuid4()))
    if isinstance(value, Mapping) and value.get("type") == "ask_user":
        raw_questions = value.get("questions")
        questions = tuple(q for q in raw_questions or [] if isinstance(q, Mapping))
        normalized = []
        for index, question in enumerate(questions):
            options = [
                {
                    "label": str(choice.get("value", "")),
                    "value": str(choice.get("value", "")),
                    "description": "",
                }
                for choice in question.get("choices", [])
                if isinstance(choice, Mapping) and choice.get("value")
            ]
            normalized.append(
                {
                    "id": f"question-{index + 1}",
                    "question": str(question.get("question", "Agent needs input")),
                    "header": "",
                    "body": "",
                    "options": options,
                    "multi_select": False,
                    "allow_other": True,
                }
            )
        return (
            InteractionRequest(
                request_id=interrupt_id,
                type="question",
                payload={"interrupt_id": interrupt_id, "questions": normalized},
                interrupt_id=interrupt_id,
                questions=questions,
            ),
            None,
        )

    # --- 审批分支：分离并发安全与非并发安全工具 ---
    description = "A tool execution requires approval"
    safe_indices: list[int] = []
    unsafe_indices: list[int] = []
    action_requests_list: list[dict[str, object]] = []
    if isinstance(value, Mapping):
        action_requests = value.get("action_requests", [])
        if isinstance(action_requests, list) and action_requests:
            action_requests_list = [r for r in action_requests if isinstance(r, Mapping)]
            for i, request in enumerate(action_requests_list):
                tool_name = str(request.get("name", ""))
                if _is_concurrency_safe(tool_name):
                    safe_indices.append(i)
                else:
                    unsafe_indices.append(i)

            # 全部是并发安全工具 → 直接放行，不产生审批交互
            if not unsafe_indices:
                total = len(action_requests_list)
                decisions: list[dict[str, object]] = [{"type": "approve"}] * total
                auto_resume = {interrupt_id: {"decisions": decisions}}
                return None, auto_resume

            # 只取第一个非并发安全工具的描述
            first_unsafe_index = unsafe_indices[0]
            first_request = action_requests_list[first_unsafe_index]
            description = str(first_request.get("description", description))

    # 构造首个 unsafe 工具的审批请求；串行元数据（完整动作列表与索引）
    # 放入 serial_context 由 _collect_serial_approvals 消费，wire payload
    # 只保留协议 schema 允许的四个字段，附加字段会触发客户端 schema 校验
    # 失败并导致整次审批静默降级为 reject。
    current_unsafe_index = unsafe_indices[0] if unsafe_indices else 0
    current_action_requests = []
    if action_requests_list:
        # 预览包含所有 safe 工具与当前要审批的 unsafe 工具
        for i in safe_indices:
            current_action_requests.append(action_requests_list[i])
        current_action_requests.append(action_requests_list[current_unsafe_index])

    return (
        InteractionRequest(
            request_id=interrupt_id,
            type="approval",
            payload={
                "interrupt_id": interrupt_id,
                "description": description,
                "requests": _bounded_json({"action_requests": current_action_requests}),
                "decisions": [
                    "approve_once",
                    "approve_thread",
                    "approve_project",
                    "reject",
                    "reject_with_feedback",
                ],
            },
            interrupt_id=interrupt_id,
            action_count=1,  # 每次只审批一个工具
            serial_context={
                "all_action_requests": action_requests_list,
                "safe_indices": safe_indices,
                "unsafe_indices": unsafe_indices,
            },
        ),
        None,
    )


def _resume_value(spec: InteractionRequest, response: object) -> dict[str, object]:
    """将语言无关的提问结果映射回 LangGraph interrupt resume 契约。

    审批类交互的 resume 值由 ``_collect_serial_approvals`` 串行收集后直接
    构造；本函数仅处理 ask_user 提问。
    """
    if not isinstance(response, dict):
        response = {}
    answers_by_id = response.get("answers", {})
    answers: list[str] = []
    if isinstance(answers_by_id, Mapping):
        for index, _question in enumerate(spec.questions):
            values = answers_by_id.get(f"question-{index + 1}", [])
            answers.append(str(values[0]) if isinstance(values, list) and values else "")
    status = "answered" if any(answers) else "cancelled"
    return {spec.interrupt_id: {"status": status, "answers": answers}}


def _message_stream_chunk(event: tuple[Any, ...]) -> object | None:
    """从 messages stream 取出原始消息块，供 wire 截断前的语义捕获使用。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        # ``subgraphs=True`` uses a non-empty namespace for child graph
        # messages.  ZC-101 only owns the linear root Transcript; those
        # messages are explicitly suppressed at the v3 adapter boundary until
        # a later execution/provenance projection can represent them safely.
        if namespace:
            return None
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return None
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return None
    return data[0]


def _capture_transcript_message(run: RunState, chunk: object) -> bool:
    """在 1 MiB wire 截断前收集完整助手/工具语义，不收集 delta 事件。"""
    chunk_type = type(chunk).__name__
    if chunk_type in {"AIMessage", "AIMessageChunk"}:
        _ensure_model_round_for_assistant(run)
        full_tool_calls = getattr(chunk, "tool_calls", None)
        if chunk_type == "AIMessage" and isinstance(full_tool_calls, list) and full_tool_calls:
            for index, tool_call in enumerate(full_tool_calls):
                if not isinstance(tool_call, Mapping):
                    continue
                _capture_full_tool_call(run, tool_call, index=index, source_message=chunk)
        else:
            for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
                if not isinstance(tool_chunk, Mapping):
                    continue
                tool_id = _resolve_tool_stream_id(
                    run, tool_chunk, source_message=chunk
                )
                name = tool_chunk.get("name")
                if name:
                    run.tool_names[tool_id] = str(name)
                _merge_assistant_tool_call(
                    run,
                    tool_id,
                    name=name,
                    arguments=tool_chunk.get("args"),
                    is_full=False,
                    call_type=tool_chunk.get("type"),
                )
        text = _message_text(chunk)
        if not text and not run.assistant_tool_calls:
            return False
        # Provider chunk IDs are optional and may change between deltas.  The
        # stream/tool boundary, not an ID, defines one complete assistant turn.
        if text:
            run.assistant_buffer.append(text)
        run.last_captured_message = chunk
        return False
    if chunk_type != "ToolMessage":
        return False
    _flush_assistant_transcript(run)
    tool_id = _resolve_tool_result_id(run, chunk)
    run.pending_transcript.append(
        TranscriptAppend(
            thread_id=run.ref.thread_id,
            record_id=f"run:{run.ref.run_id}:tool:{tool_id}",
            kind="tool",
            content=_content_text(getattr(chunk, "content", None)),
            run_id=run.ref.run_id,
            execution_id=run.root_execution_ref.execution_id,
            tool_call_id=tool_id,
            tool_name=run.tool_names.get(
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
    tool_call: Mapping[str, object],
    *,
    index: int,
    source_message: object | None = None,
) -> None:
    """捕获完整 AIMessage.tool_calls，不从 ToolMessage 反推参数。"""
    raw_id = tool_call.get("id")
    tool_id = _resolve_tool_stream_id(
        run,
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
        run.tool_names[tool_id] = str(name)
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
            "name": str(name or run.tool_names.get(tool_id) or "tool"),
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
            # Preserve an explicit invalid marker instead of coercing a
            # provider object to a string that could be mistaken for JSON.
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


def _translate_stream_event(
    event: tuple[Any, ...], run: RunState
) -> Iterable[tuple[str, dict[str, object]]]:
    """把 LangChain message stream 转换为统一领域事件。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        if namespace:
            # ZC-101 keeps root canonical/tool-correlation state separate from
            # child graph state.  Child messages may still be observed by a
            # future execution/provenance projection, but dropping them here
            # is safer than mutating root IDs while preserving a v3 wire shape.
            return []
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return []
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return []
    chunk = data[0]
    if type(chunk).__name__ in {"AIMessage", "AIMessageChunk"}:
        _ensure_model_round_for_assistant(run)
    _update_usage(run, getattr(chunk, "usage_metadata", None))
    events: list[tuple[str, dict[str, object]]] = []
    content = _message_text(chunk)
    if content and type(chunk).__name__ != "ToolMessage":
        events.append((CONTENT_DELTA, {"text": content}))
    reasoning = _reasoning_text(chunk)
    if reasoning:
        # 供应商明确返回的思维内容（如 Chat Completions reasoning_content）。
        # 只走运行期事件，绝不进入 assistant 正文或 Transcript。
        events.append((REASONING_DELTA, {"text": reasoning}))
    elif (
        type(chunk).__name__ in {"AIMessage", "AIMessageChunk"}
        and not content
        and not getattr(chunk, "tool_call_chunks", None)
        and _has_reasoning_block(chunk)
    ):
        # Translator 也可能被独立调用（例如恢复/测试 seam），此时仍需给
        # reasoning-only chunk 一个事实进度事件，而不是依赖 _stream_agent。
        events.append((RUN_PROGRESS, _run_progress_payload(run, "model")))
    for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
        tool_id = _resolve_tool_stream_id(run, tool_chunk, source_message=chunk)
        if tool_chunk.get("name") and tool_id not in run.started_tool_ids:
            run.started_tool_ids.add(tool_id)
            events.append((TOOL_STARTED, {"tool_call_id": tool_id, "name": str(tool_chunk["name"])}))
        if tool_chunk.get("args"):
            arguments = _truncate_text(str(tool_chunk["args"]))
            events.append(
                (
                    TOOL_DELTA,
                    {
                        "tool_call_id": tool_id,
                        "arguments_delta": arguments[0],
                        "truncated": arguments[1],
                        "original_bytes": arguments[2],
                    },
                )
            )
    if type(chunk).__name__ == "ToolMessage":
        result = _truncate_text(_content_text(getattr(chunk, "content", None)))
        if run.last_tool_result_chunk is chunk and run.last_tool_result_id:
            tool_id = run.last_tool_result_id
        else:
            tool_id = _resolve_tool_result_id(run, chunk)
        events.append(
            (
                TOOL_COMPLETED,
                {
                    "tool_call_id": tool_id,
                    "result": {
                        "content": result[0],
                        "is_error": getattr(chunk, "status", None) == "error",
                        "truncated": result[1],
                        "original_bytes": result[2],
                    },
                },
            )
        )
    return events


def _resolve_tool_stream_id(
    run: RunState,
    chunk: Mapping[str, Any],
    *,
    source_message: object | None = None,
) -> str:
    """为当前模型回合的工具续片建立稳定且不会跨回合复用的 ID。"""
    _ensure_model_round_for_assistant(run)
    index = chunk.get("index")
    raw_id = str(chunk.get("id") or "")
    if raw_id:
        tool_id = run.tool_stream_ids.get(f"id:{raw_id}")
        if tool_id is None and index is not None:
            # 某些 provider 先发无 ID 分片、后发带 ID 分片；同一 index
            # 仍属于同一调用，不能因 ID 后出现而拆成两条记录。
            tool_id = run.tool_stream_ids.get(f"index:{index}")
        if tool_id is None:
            tool_id = _allocate_tool_id(run, raw_id)
        run.tool_result_ids[raw_id] = tool_id
        run.tool_stream_ids[f"id:{raw_id}"] = tool_id
        if index is not None:
            run.tool_stream_ids[f"index:{index}"] = tool_id
    else:
        key = f"index:{index}" if index is not None else "current"
        tool_id = run.tool_stream_ids.get(key)
        if (
            tool_id is not None
            and index is None
            and not raw_id
            and chunk.get("name")
            and run.tool_names.get(tool_id)
            and source_message is not run.last_captured_message
        ):
            raise _run_error(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool calls without index cannot be associated safely",
            )
        if tool_id is None:
            tool_id = _allocate_tool_id(run)
            run.tool_stream_ids[key] = tool_id
    run.last_tool_id = tool_id
    return tool_id


def _resolve_tool_result_id(run: RunState, chunk: object) -> str:
    """把 ToolMessage 归属到当前回合，无法可靠关联时明确失败。"""
    if not run.model_round_active:
        _start_model_round(run)
    result_id = str(getattr(chunk, "tool_call_id", "") or "")
    if result_id:
        tool_id = run.tool_result_ids.get(result_id)
        if tool_id is None:
            tool_id = run.tool_stream_ids.get(f"id:{result_id}")
        if tool_id is None:
            candidates = _current_tool_candidates(run)
            if len(candidates) == 1:
                candidate = next(iter(candidates))
                if _candidate_has_provider_id(run, candidate):
                    raise _run_error(
                        "TOOL_CALL_ID_UNAVAILABLE",
                        "Tool result ID does not match the known provider call ID",
                    )
                # The only safe late-binding case is an assistant chunk whose
                # call had no provider ID at all. Bind the result ID to that
                # existing internal call, without inventing a call when no
                # assistant declaration was observed.
                tool_id = candidate
            elif len(candidates) > 1:
                raise _run_error(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result cannot be associated with parallel calls without stable IDs",
                )
            else:
                raise _run_error(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result has no preceding assistant tool call",
                )
            run.tool_result_ids[result_id] = tool_id
            run.tool_stream_ids[f"id:{result_id}"] = tool_id
        run.seen_tool_provider_ids.add(result_id)
    else:
        candidates = _current_tool_candidates(run)
        if len(candidates) != 1:
            raise _run_error(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Tool result has no stable ID and cannot be associated safely",
            )
        tool_id = next(iter(candidates))
        if tool_id in run.completed_tool_ids:
            raise _run_error(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool results cannot be associated safely",
            )
    run.completed_tool_ids.add(tool_id)
    run.model_round_has_tool_results = True
    run.last_tool_id = tool_id
    run.last_tool_result_id = tool_id
    run.last_tool_result_chunk = chunk
    return tool_id


def _current_tool_candidates(run: RunState) -> set[str]:
    """返回当前模型回合去重后的工具调用候选。"""
    return set(run.tool_stream_ids.values())


def _candidate_has_provider_id(run: RunState, tool_id: str) -> bool:
    """判断候选是否已经由 assistant 明确声明过 provider tool-call ID。"""
    return any(
        key.startswith("id:") and candidate == tool_id
        for key, candidate in run.tool_stream_ids.items()
    )


def _allocate_tool_id(run: RunState, preferred: str | None = None) -> str:
    """分配 Run 内单调唯一 ID，稳定 provider ID 只在首次出现时直接复用。"""
    run.tool_call_ordinal += 1
    if preferred and preferred not in run.seen_tool_provider_ids:
        candidate = preferred
    else:
        candidate = f"tool-{run.ref.run_id}-{run.tool_call_ordinal}"
    while candidate in run.allocated_tool_ids:
        run.tool_call_ordinal += 1
        candidate = f"tool-{run.ref.run_id}-{run.tool_call_ordinal}"
    run.allocated_tool_ids.add(candidate)
    if preferred:
        run.seen_tool_provider_ids.add(preferred)
    return candidate


def _start_model_round(run: RunState) -> None:
    """清理只属于当前模型/工具回合的临时映射。"""
    run.model_round_active = True
    run.model_round_has_tool_results = False
    run.tool_stream_ids.clear()
    run.tool_result_ids.clear()
    run.tool_names.clear()
    run.started_tool_ids.clear()
    run.completed_tool_ids.clear()
    run.assistant_tool_calls.clear()
    run.last_tool_id = None
    run.last_tool_result_id = None
    run.last_tool_result_chunk = None
    run.last_captured_message = None


def _ensure_model_round_for_assistant(run: RunState) -> None:
    """模型在收到上一回合工具结果后开始新回合并重置临时索引。"""
    if not run.model_round_active or run.model_round_has_tool_results:
        _start_model_round(run)


def _finish_model_round(run: RunState) -> None:
    """流正常结束后丢弃回合映射，但保留 Run 级 ordinal 和去重事实。"""
    run.model_round_active = False
    run.model_round_has_tool_results = False
    run.tool_stream_ids.clear()
    run.tool_result_ids.clear()
    run.tool_names.clear()
    run.started_tool_ids.clear()
    run.completed_tool_ids.clear()
    run.assistant_tool_calls.clear()
    run.last_tool_id = None
    run.last_tool_result_id = None
    run.last_tool_result_chunk = None
    run.last_captured_message = None


def _update_usage(run: RunState, usage: Any) -> None:
    """合并流式 usage，避免分片计数回退。"""
    if not isinstance(usage, Mapping):
        return
    run.usage["input_tokens"] = max(
        run.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0)
    )
    run.usage["output_tokens"] = max(
        run.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0)
    )


def _truncate_text(value: str) -> tuple[str, bool, int]:
    """按 UTF-8 字节安全截断工具输出，并保留原始大小。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return value, False, len(encoded)
    clipped = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return clipped, True, len(encoded)


def _content_text(content: object) -> str:
    """提取 LangChain 内容字段中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return "" if content is None else str(content)


def _message_text(message: object) -> str:
    """只读取显式 text block；不透明 content_blocks 不得回退成正文。"""
    blocks = getattr(message, "content_blocks", None)
    if isinstance(blocks, list):
        return "".join(
            block["text"]
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    content = getattr(message, "content", None)
    if isinstance(content, list):
        return "".join(
            item
            if isinstance(item, str)
            else item["text"]
            for item in content
            if isinstance(item, str)
            or (
                isinstance(item, Mapping)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
        )
    return _content_text(content)


def _reasoning_text(message: object) -> str:
    """提取供应商明确返回的思维文本，只用于运行期 reasoning.delta。

    优先读取 Chat Completions 的 ``additional_kwargs.reasoning_content``
    （由 gateway 适配器在 raw SSE 解析时注入）；其次兼容 content blocks 中
    ``type=reasoning`` 的 ``text``/``reasoning`` 字段。提取结果不会进入
    assistant 正文、日志或 Transcript。
    """
    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        value = kwargs.get("reasoning_content")
        if isinstance(value, str) and value:
            return value
    blocks = getattr(message, "content_blocks", None)
    if not isinstance(blocks, list):
        blocks = getattr(message, "content", None)
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("type") != "reasoning":
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text:
            text = block.get("reasoning")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _has_reasoning_block(message: object) -> bool:
    """判断消息是否包含 reasoning block，但不读取其私有内容。"""
    blocks = getattr(message, "content_blocks", None)
    return isinstance(blocks, list) and any(
        isinstance(block, Mapping) and block.get("type") == "reasoning"
        for block in blocks
    )


def _run_progress_payload(run: RunState, phase: str) -> dict[str, object]:
    """生成只包含事实阶段和活动时长的运行进度 payload。"""
    safe_phase = phase if phase in {"preparing", "model"} else "preparing"
    return {
        "phase": safe_phase,
        "elapsed_ms": max(0, round((time.monotonic() - run.started_at) * 1000)),
    }


def _json_safe(value: object) -> object:
    """确保中断详情可 JSON 编码，复杂对象降级为字符串。"""
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _bounded_json(value: object) -> object:
    """限制交互详情的 JSON 大小，避免工具参数撑爆 stdio。"""
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
