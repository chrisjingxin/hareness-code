"""Build/Compose 共用的 execution stream deep module。

外部 interface 保持为一次执行调用：

    execute(ExecutionStreamRequest, ExecutionStreamPorts)
      → ExecutionStreamResult

module 隐藏 LangGraph astream、Reasoning 安全翻译、Tool 复合身份关联、
interrupt 提取与 resume、usage 合并、取消检查和 final content 捕获。

内容可见策略：
- passthrough：安全 text 产生 content.delta（Build root）
- capture_only：正文只进入 final_content，不产生 content.delta（Compose Stage）

Transcript / Compose activity 不进入本 module 的 public interface；
由 adapter 通过 Ports 观察 raw message 或 signal 后自行处理。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# 单条工具 payload 的 1 MiB wire 上限；与 schema x-harness / host 同步。
MAX_TOOL_PAYLOAD_BYTES = 1 * 1024 * 1024

RUN_PROGRESS = "run.progress"
CONTENT_DELTA = "content.delta"
REASONING_DELTA = "reasoning.delta"
TOOL_STARTED = "tool.started"
TOOL_DELTA = "tool.delta"
TOOL_COMPLETED = "tool.completed"

ContentVisibility = Literal["passthrough", "capture_only"]


class ExecutionStreamError(Exception):
    """execution stream 基础设施或 Tool 关联失败。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(slots=True)
class StreamSession:
    """一次 execution stream 的 Tool 关联、usage 与 final content 状态。

    Build 可在多次 resume 之间复用同一 session，以保留 Run 级
    tool_call_ordinal / allocated_tool_ids / seen_tool_provider_ids。
    """

    run_id: str
    started_at: float = field(default_factory=time.monotonic)
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    tool_stream_ids: dict[str, str] = field(default_factory=dict)
    tool_result_ids: dict[str, str] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)
    started_tool_ids: set[str] = field(default_factory=set)
    completed_tool_ids: set[str] = field(default_factory=set)
    seen_tool_provider_ids: set[str] = field(default_factory=set)
    allocated_tool_ids: set[str] = field(default_factory=set)
    tool_call_ordinal: int = 0
    last_tool_id: str | None = None
    last_tool_result_id: str | None = None
    last_tool_result_chunk: object | None = None
    last_captured_message: object | None = None
    model_round_active: bool = False
    model_round_has_tool_results: bool = False
    # 只保留当前（也就是最终）模型回合的正文。Tool 前的说明已经通过
    # content.delta 展示，但不能与 Tool 后的最终 artifact/回答拼接。
    content_parts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExecutionSignal:
    """共享 stream 发出的领域信号；adapter 决定如何投影到 Host Event。"""

    type: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StreamInteractionRequest:
    """stream 内的 Interaction 请求；不依赖 Host 类型。"""

    request_id: str
    type: str
    payload: Mapping[str, object]
    interrupt_id: str
    questions: tuple[Mapping[str, object], ...] = ()
    action_count: int = 1
    serial_context: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ExecutionStreamRequest:
    """一次 stream 调用的可信输入。"""

    agent: Any
    stream_input: object
    graph_config: Mapping[str, object]
    context: object | None
    content_visibility: ContentVisibility
    session: StreamSession
    is_cancelled: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ExecutionStreamResult:
    """一次 stream 调用的结果；resume 非空时表示需要 Interaction 后继续。"""

    final_content: str
    usage: Mapping[str, int]
    resume: object | None = None


class ExecutionStreamPorts(Protocol):
    """stream 对外的双向 seam：发信号与请求 Interaction。"""

    def emit(self, signal: ExecutionSignal) -> None:
        """接收领域信号；由 adapter 分配 sequence / 写 wire。"""

    async def interact(self, request: StreamInteractionRequest) -> object:
        """请求审批或问答；返回语言无关 resume 值或回答对象。

        审批类请求应返回可直接用于 LangGraph Command(resume=...) 的完整
        resume dict（含串行审批收集结果）；提问类返回 answers 对象，
        由 module 映射为 interrupt resume 契约。
        """

    async def observe_message(self, chunk: object, session: StreamSession) -> bool:
        """观察 raw message（Build Transcript 等）；返回是否完成 tool 边界。

        默认实现应返回 False。Compose capture_only 可忽略。
        """

    async def after_tool_boundary(self) -> None:
        """Tool 语义越过内存边界后的可选 flush 钩子。"""

    def on_stream_event(self) -> None:
        """每个 LangGraph 事件后的可选钩子（例如 drain context updates）。"""


@dataclass(slots=True)
class _NullObserverPorts:
    """仅用于单元测试的空 Ports。"""

    emitted: list[ExecutionSignal] = field(default_factory=list)
    interactions: list[StreamInteractionRequest] = field(default_factory=list)
    _interact_result: object = None

    def emit(self, signal: ExecutionSignal) -> None:
        self.emitted.append(signal)

    async def interact(self, request: StreamInteractionRequest) -> object:
        self.interactions.append(request)
        return self._interact_result if self._interact_result is not None else {}

    async def observe_message(self, chunk: object, session: StreamSession) -> bool:
        return False

    async def after_tool_boundary(self) -> None:
        return None

    def on_stream_event(self) -> None:
        return None


async def execute(
    request: ExecutionStreamRequest,
    ports: ExecutionStreamPorts,
) -> ExecutionStreamResult:
    """执行一次 LangGraph stream 阶段，直到结束或需要 Interaction resume。"""
    if request.is_cancelled():
        raise ExecutionStreamError("RUN_CANCELLED", "Run was cancelled before stream")

    session = request.session
    stream_kwargs: dict[str, Any] = {
        "config": dict(request.graph_config),
        "stream_mode": ["messages", "updates"],
        "subgraphs": True,
    }
    if request.context is not None:
        stream_kwargs["context"] = request.context

    async for event in request.agent.astream(request.stream_input, **stream_kwargs):
        ports.on_stream_event()
        if request.is_cancelled():
            raise ExecutionStreamError("RUN_CANCELLED", "Run was cancelled during stream")

        interaction, auto_resume = extract_interaction(event)
        if auto_resume is not None:
            # Interaction resume 前不清理 model round：下一阶段 astream
            # 仍可能依赖尚未结束的关联状态，与历史 Build 行为一致。
            return ExecutionStreamResult(
                final_content="".join(session.content_parts),
                usage=dict(session.usage),
                resume=auto_resume,
            )
        if interaction is not None:
            response = await ports.interact(interaction)
            if interaction.type == "approval":
                # 串行审批由 adapter 收集完整 decisions resume dict。
                resume = response
            else:
                resume = resume_value(interaction, response)
            return ExecutionStreamResult(
                final_content="".join(session.content_parts),
                usage=dict(session.usage),
                resume=resume,
            )

        chunk = message_stream_chunk(event)
        if chunk is not None:
            complete_tool = await ports.observe_message(chunk, session)
            if complete_tool:
                await ports.after_tool_boundary()

        for signal in translate_stream_event(
            event,
            session,
            content_visibility=request.content_visibility,
        ):
            ports.emit(signal)

    finish_model_round(session)
    return ExecutionStreamResult(
        final_content="".join(session.content_parts),
        usage=dict(session.usage),
        resume=None,
    )


_CONCURRENCY_SAFE_TOOLS = frozenset({
    "ls", "read_file", "glob", "grep", "web_search",
    "lsp", "tool_search", "memory_search", "task_output",
    "ask_user", "write_todos", "memory_save",
    "enter_plan_mode", "exit_plan_mode",
})


def is_concurrency_safe(tool_name: str) -> bool:
    """并发安全工具无需审批，可直接并行执行。"""
    return tool_name in _CONCURRENCY_SAFE_TOOLS


def extract_interaction(
    event: tuple[Any, ...],
) -> tuple[StreamInteractionRequest | None, dict[str, object] | None]:
    """从 DeepAgents updates 流提取首个 AskUser 或 HITL interrupt。

    返回 (request, None) 表示需要用户交互；
    返回 (None, dict) 表示全部并发安全工具，自动放行；
    返回 (None, None) 表示没有交互需要处理。
    """
    if len(event) == 3:
        namespace, stream_mode, data = event
        # 非空 namespace 属于 child graph；root 路径不得把它当 root 审批。
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
            StreamInteractionRequest(
                request_id=interrupt_id,
                type="question",
                payload={"interrupt_id": interrupt_id, "questions": normalized},
                interrupt_id=interrupt_id,
                questions=questions,
            ),
            None,
        )

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
                if is_concurrency_safe(tool_name):
                    safe_indices.append(i)
                else:
                    unsafe_indices.append(i)

            if not unsafe_indices:
                total = len(action_requests_list)
                decisions: list[dict[str, object]] = [{"type": "approve"}] * total
                auto_resume = {interrupt_id: {"decisions": decisions}}
                return None, auto_resume

            first_unsafe_index = unsafe_indices[0]
            first_request = action_requests_list[first_unsafe_index]
            description = str(first_request.get("description", description))

    current_unsafe_index = unsafe_indices[0] if unsafe_indices else 0
    current_action_requests = []
    if action_requests_list:
        for i in safe_indices:
            current_action_requests.append(action_requests_list[i])
        current_action_requests.append(action_requests_list[current_unsafe_index])

    return (
        StreamInteractionRequest(
            request_id=interrupt_id,
            type="approval",
            payload={
                "interrupt_id": interrupt_id,
                "description": description,
                "requests": bounded_json({"action_requests": current_action_requests}),
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
                "all_action_requests": action_requests_list,
                "safe_indices": safe_indices,
                "unsafe_indices": unsafe_indices,
            },
        ),
        None,
    )


def resume_value(spec: StreamInteractionRequest, response: object) -> dict[str, object]:
    """将语言无关的提问结果映射回 LangGraph interrupt resume 契约。"""
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


def message_stream_chunk(event: tuple[Any, ...]) -> object | None:
    """从 messages stream 取出原始消息块。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        if namespace:
            return None
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return None
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return None
    return data[0]


def translate_stream_event(
    event: tuple[Any, ...],
    session: StreamSession,
    *,
    content_visibility: ContentVisibility = "passthrough",
) -> Iterable[ExecutionSignal]:
    """把 LangChain message stream 转换为统一领域信号。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        if namespace:
            return []
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return []
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return []
    chunk = data[0]
    if type(chunk).__name__ in {"AIMessage", "AIMessageChunk"}:
        ensure_model_round_for_assistant(session)
    update_usage(session, getattr(chunk, "usage_metadata", None))
    events: list[ExecutionSignal] = []
    content = message_text(chunk)
    if content and type(chunk).__name__ != "ToolMessage":
        session.content_parts.append(content)
        if content_visibility == "passthrough":
            events.append(ExecutionSignal(CONTENT_DELTA, {"text": content}))
    reasoning = reasoning_text(chunk)
    if reasoning:
        events.append(ExecutionSignal(REASONING_DELTA, {"text": reasoning}))
    elif (
        type(chunk).__name__ in {"AIMessage", "AIMessageChunk"}
        and not content
        and not getattr(chunk, "tool_call_chunks", None)
        and has_reasoning_block(chunk)
    ):
        events.append(ExecutionSignal(RUN_PROGRESS, run_progress_payload(session, "model")))
    for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
        tool_id = resolve_tool_stream_id(session, tool_chunk, source_message=chunk)
        if tool_chunk.get("name") and tool_id not in session.started_tool_ids:
            session.started_tool_ids.add(tool_id)
            events.append(
                ExecutionSignal(
                    TOOL_STARTED,
                    {"tool_call_id": tool_id, "name": str(tool_chunk["name"])},
                )
            )
        if tool_chunk.get("args"):
            arguments = truncate_text(str(tool_chunk["args"]))
            events.append(
                ExecutionSignal(
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
        result = truncate_text(content_text(getattr(chunk, "content", None)))
        if session.last_tool_result_chunk is chunk and session.last_tool_result_id:
            tool_id = session.last_tool_result_id
        else:
            tool_id = resolve_tool_result_id(session, chunk)
        events.append(
            ExecutionSignal(
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


def resolve_tool_stream_id(
    session: StreamSession,
    chunk: Mapping[str, Any],
    *,
    source_message: object | None = None,
) -> str:
    """为当前模型回合的工具续片建立稳定且不会跨回合复用的 ID。"""
    ensure_model_round_for_assistant(session)
    index = chunk.get("index")
    raw_id = str(chunk.get("id") or "")
    if raw_id:
        tool_id = session.tool_stream_ids.get(f"id:{raw_id}")
        if tool_id is None and index is not None:
            tool_id = session.tool_stream_ids.get(f"index:{index}")
        if tool_id is None:
            tool_id = allocate_tool_id(session, raw_id)
        session.tool_result_ids[raw_id] = tool_id
        session.tool_stream_ids[f"id:{raw_id}"] = tool_id
        if index is not None:
            session.tool_stream_ids[f"index:{index}"] = tool_id
    else:
        key = f"index:{index}" if index is not None else "current"
        tool_id = session.tool_stream_ids.get(key)
        if (
            tool_id is not None
            and index is None
            and not raw_id
            and chunk.get("name")
            and session.tool_names.get(tool_id)
            and source_message is not session.last_captured_message
        ):
            raise ExecutionStreamError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool calls without index cannot be associated safely",
            )
        if tool_id is None:
            tool_id = allocate_tool_id(session)
            session.tool_stream_ids[key] = tool_id
    session.last_tool_id = tool_id
    return tool_id


def resolve_tool_result_id(session: StreamSession, chunk: object) -> str:
    """把 ToolMessage 归属到当前回合，无法可靠关联时明确失败。"""
    if not session.model_round_active:
        start_model_round(session)
    result_id = str(getattr(chunk, "tool_call_id", "") or "")
    if result_id:
        tool_id = session.tool_result_ids.get(result_id)
        if tool_id is None:
            tool_id = session.tool_stream_ids.get(f"id:{result_id}")
        if tool_id is None:
            candidates = current_tool_candidates(session)
            if len(candidates) == 1:
                candidate = next(iter(candidates))
                if candidate_has_provider_id(session, candidate):
                    raise ExecutionStreamError(
                        "TOOL_CALL_ID_UNAVAILABLE",
                        "Tool result ID does not match the known provider call ID",
                    )
                tool_id = candidate
            elif len(candidates) > 1:
                raise ExecutionStreamError(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result cannot be associated with parallel calls without stable IDs",
                )
            else:
                raise ExecutionStreamError(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result has no preceding assistant tool call",
                )
            session.tool_result_ids[result_id] = tool_id
            session.tool_stream_ids[f"id:{result_id}"] = tool_id
        session.seen_tool_provider_ids.add(result_id)
    else:
        candidates = current_tool_candidates(session)
        if len(candidates) != 1:
            raise ExecutionStreamError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Tool result has no stable ID and cannot be associated safely",
            )
        tool_id = next(iter(candidates))
        if tool_id in session.completed_tool_ids:
            raise ExecutionStreamError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool results cannot be associated safely",
            )
    session.completed_tool_ids.add(tool_id)
    session.model_round_has_tool_results = True
    session.last_tool_id = tool_id
    session.last_tool_result_id = tool_id
    session.last_tool_result_chunk = chunk
    return tool_id


def current_tool_candidates(session: StreamSession) -> set[str]:
    """返回当前模型回合去重后的工具调用候选。"""
    return set(session.tool_stream_ids.values())


def candidate_has_provider_id(session: StreamSession, tool_id: str) -> bool:
    """判断候选是否已经由 assistant 明确声明过 provider tool-call ID。"""
    return any(
        key.startswith("id:") and candidate == tool_id
        for key, candidate in session.tool_stream_ids.items()
    )


def allocate_tool_id(session: StreamSession, preferred: str | None = None) -> str:
    """分配 execution 内单调唯一 ID，稳定 provider ID 只在首次出现时直接复用。"""
    session.tool_call_ordinal += 1
    if preferred and preferred not in session.seen_tool_provider_ids:
        candidate = preferred
    else:
        candidate = f"tool-{session.run_id}-{session.tool_call_ordinal}"
    while candidate in session.allocated_tool_ids:
        session.tool_call_ordinal += 1
        candidate = f"tool-{session.run_id}-{session.tool_call_ordinal}"
    session.allocated_tool_ids.add(candidate)
    if preferred:
        session.seen_tool_provider_ids.add(preferred)
    return candidate


def start_model_round(session: StreamSession) -> None:
    """开始新模型回合，并清理上一回合的正文与 Tool 临时状态。"""
    session.model_round_active = True
    session.model_round_has_tool_results = False
    session.content_parts.clear()
    session.tool_stream_ids.clear()
    session.tool_result_ids.clear()
    session.tool_names.clear()
    session.started_tool_ids.clear()
    session.completed_tool_ids.clear()
    session.last_tool_id = None
    session.last_tool_result_id = None
    session.last_tool_result_chunk = None
    session.last_captured_message = None


def ensure_model_round_for_assistant(session: StreamSession) -> None:
    """模型在收到上一回合工具结果后开始新回合并重置临时索引。"""
    if not session.model_round_active or session.model_round_has_tool_results:
        start_model_round(session)


def finish_model_round(session: StreamSession) -> None:
    """流正常结束后丢弃回合映射，但保留 execution 级 ordinal 和去重事实。"""
    session.model_round_active = False
    session.model_round_has_tool_results = False
    session.tool_stream_ids.clear()
    session.tool_result_ids.clear()
    session.tool_names.clear()
    session.started_tool_ids.clear()
    session.completed_tool_ids.clear()
    session.last_tool_id = None
    session.last_tool_result_id = None
    session.last_tool_result_chunk = None
    session.last_captured_message = None


def update_usage(session: StreamSession, usage: Any) -> None:
    """合并流式 usage，避免分片计数回退。"""
    if not isinstance(usage, Mapping):
        return
    session.usage["input_tokens"] = max(
        session.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0)
    )
    session.usage["output_tokens"] = max(
        session.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0)
    )


def truncate_text(value: str) -> tuple[str, bool, int]:
    """按 UTF-8 字节安全截断工具输出，并保留原始大小。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return value, False, len(encoded)
    clipped = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return clipped, True, len(encoded)


def content_text(content: object) -> str:
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


def message_text(message: object) -> str:
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
    return content_text(content)


def reasoning_text(message: object) -> str:
    """提取供应商明确返回的思维文本，只用于运行期 reasoning.delta。"""
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


def has_reasoning_block(message: object) -> bool:
    """判断消息是否包含 reasoning block，但不读取其私有内容。"""
    blocks = getattr(message, "content_blocks", None)
    return isinstance(blocks, list) and any(
        isinstance(block, Mapping) and block.get("type") == "reasoning"
        for block in blocks
    )


def run_progress_payload(session: StreamSession, phase: str) -> dict[str, object]:
    """生成只包含事实阶段和活动时长的运行进度 payload。"""
    safe_phase = phase if phase in {"preparing", "model"} else "preparing"
    return {
        "phase": safe_phase,
        "elapsed_ms": max(0, round((time.monotonic() - session.started_at) * 1000)),
    }


def json_safe(value: object) -> object:
    """确保中断详情可 JSON 编码，复杂对象降级为字符串。"""
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def bounded_json(value: object) -> object:
    """限制交互详情的 JSON 大小，避免工具参数撑爆 stdio。"""
    safe = json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
