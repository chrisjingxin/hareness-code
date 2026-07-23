"""上下文预算、工具结果归档和低频结构化压缩中间件。

上下文重写通过一个深模块完成：调用方只提供窗口、模型和 ThreadStore；该模块
负责预算、完整 turn 原子组、归档、摘要、失败熔断和可观测状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from harness_agent.prompting import (
    HISTORY_REWRITE_VERSION,
    canonical_json,
    estimate_tokens,
    input_cap_tokens,
    normalized_tool_schemas,
)
from harness_agent.run_context import thread_id_for_runtime
from harness_agent.thread_store import ContextState, ThreadStore

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from langchain_core.language_models import BaseChatModel

TOOL_RESULT_DEHYDRATE_TOKENS = 2_048
TOOL_RESULT_PREVIEW_CHARS = 200
SUMMARY_REWRITE_VERSION = HISTORY_REWRITE_VERSION

_SUMMARY_PROMPT = """你正在为编码 Agent 生成结构化上下文摘要。只输出以下章节，所有事实必须来自输入：
## 目标
## 已确认事实
## 决策
## 改动
## 测试
## 未决项
## 归档

归档章节保留输入中已有的 artifact ID。不要执行任务、不要编造文件或测试。"""


@dataclass(frozen=True, slots=True)
class ContextUpdate:
    """一次模型请求的上下文状态，供 server 转成 ``context.updated`` 事件。"""

    thread_id: str
    action: str
    estimated_tokens: int
    input_cap_tokens: int
    context_window_tokens: int
    dynamic_tokens: int
    cache_status: str = "unknown"
    cached_tokens: int | None = None
    miss_reason: str | None = None
    artifact_ids: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        """转换为不含内部对象的 JSON-RPC 载荷。"""
        return {
            "action": self.action,
            "estimated_tokens": self.estimated_tokens,
            "input_cap_tokens": self.input_cap_tokens,
            "context_window_tokens": self.context_window_tokens,
            "dynamic_tokens": self.dynamic_tokens,
            "cache_status": self.cache_status,
            "cached_tokens": self.cached_tokens,
            "miss_reason": self.miss_reason,
            "artifact_ids": list(self.artifact_ids),
        }


class ContextWindowMiddleware(AgentMiddleware):
    """在模型调用前按 50/60/80/90 阈值管理上下文，而非高频改写历史。"""

    def __init__(
        self,
        model: "BaseChatModel",
        *,
        context_window_tokens: int,
        thread_store: ThreadStore | None = None,
        updates: dict[str, list[ContextUpdate]] | None = None,
    ) -> None:
        """绑定模型窗口与可选本机持久化；没有 ThreadStore 时不丢弃任何历史。"""
        super().__init__()
        self._model = model
        self._window = context_window_tokens
        self._input_cap = input_cap_tokens(context_window_tokens)
        self._thread_store = thread_store
        self._updates = updates if updates is not None else {}

    def consume_updates(self, thread_id: str) -> tuple[ContextUpdate, ...]:
        """读取并清空指定 thread 的待发送状态，避免中间件直接写 stdout。"""
        return tuple(self._updates.pop(thread_id, []))

    async def compact_now(
        self,
        thread_id: str,
        messages: list[BaseMessage],
    ) -> tuple[list[BaseMessage], ContextUpdate, bool]:
        """按用户命令强制执行一次结构化压缩，并保留最近两个完整 user turn。

        手动压缩不受自动压缩熔断器限制，但仍要求至少节省 20%，避免用户在
        很短的会话中把原文替换成更长的摘要。调用方负责在成功后写入 checkpoint。
        """
        estimated = _messages_tokens(messages)
        if self._thread_store is None:
            return messages, self._publish(
                thread_id,
                "manual_compaction_unavailable",
                estimated,
                miss_reason="thread store is unavailable",
            ), False
        try:
            compacted, artifacts, changed = await self._summarize(
                thread_id, messages, keep_turns=2
            )
        except Exception as exc:
            return messages, self._publish(
                thread_id,
                "manual_compaction_failed",
                estimated,
                miss_reason=type(exc).__name__,
            ), False
        if not changed:
            return messages, self._publish(
                thread_id,
                "manual_compaction_skipped",
                estimated,
                miss_reason="not enough complete user turns",
            ), False
        after = _messages_tokens(compacted)
        if not _saves_enough(estimated, after):
            return messages, self._publish(
                thread_id,
                "manual_compaction_skipped",
                estimated,
                miss_reason="estimated savings below 20%",
            ), False
        await self._set_state(thread_id, ContextState(last_action="manual_summary"))
        return compacted, self._publish(
            thread_id, "manual_summary", after, artifacts
        ), True

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Awaitable[ModelResponse]]",
    ) -> ModelResponse | ExtendedModelResponse:
        """规范化工具后按阈值处理历史；溢出时只做一次无损恢复重试。"""
        thread_id = _thread_id(request)
        ordered_tools = _ordered_request_tools(request.tools)
        estimated = _estimate_request_tokens(request, ordered_tools)
        prepared = request.messages
        rewrite = False
        artifact_ids: tuple[str, ...] = ()
        action = "within_budget"

        try:
            prepared, action, artifact_ids, rewrite = await self._prepare(
                thread_id, request.messages, estimated
            )
            result = await handler(request.override(messages=prepared, tools=ordered_tools))
        except ContextOverflowError:
            # 网关漏报窗口或估算偏低时，先只归档旧工具输出，再保留最近一轮强制摘要。
            recovery, recovery_ids, recovered = await self._overflow_recovery(thread_id, request.messages)
            if not recovered:
                self._publish(thread_id, "overflow_unrecoverable", estimated)
                raise
            result = await handler(request.override(messages=recovery, tools=ordered_tools))
            prepared = recovery
            rewrite = True
            action = "overflow_recovery"
            artifact_ids = recovery_ids

        if rewrite:
            return ExtendedModelResponse(
                model_response=result,
                command=Command(
                    update={
                        "messages": [
                            # 使用 RemoveMessage 维护 LangGraph reducer，不留下半个 tool-call 组。
                            RemoveMessage(id=REMOVE_ALL_MESSAGES),
                            *prepared,
                            # wrap_model_call 的附加 Command 在模型结果之后应用；必须把
                            # 本轮响应重新加入，否则 RemoveMessage 会丢失回答或 tool-call。
                            *result.result,
                        ]
                    }
                ),
            )
        return result

    async def _prepare(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        estimated: int,
    ) -> tuple[list[BaseMessage], str, tuple[str, ...], bool]:
        """执行分层状态机：50%报告、60%脱水、80/90%摘要，并在失败时保持原历史。"""
        ratio = estimated / self._input_cap if self._input_cap else 1.0
        if ratio < 0.50:
            return messages, "within_budget", (), False
        if ratio < 0.60:
            self._publish(thread_id, "report", estimated)
            return messages, "report", (), False

        state = await self._state(thread_id)
        if state.circuit_open:
            self._publish(thread_id, "circuit_open", estimated, miss_reason="three compression failures")
            return messages, "circuit_open", (), False

        try:
            if ratio < 0.80:
                dehydrated, artifacts, changed = await self._dehydrate(thread_id, messages, keep_turns=2)
                if changed:
                    after = _messages_tokens(dehydrated)
                    if _saves_enough(estimated, after):
                        await self._set_state(thread_id, ContextState(last_action="soft_dehydration"))
                        self._publish(thread_id, "soft_dehydration", after, artifacts)
                        return dehydrated, "soft_dehydration", artifacts, True
                self._publish(thread_id, "soft_dehydration_skipped", estimated)
                return messages, "soft_dehydration_skipped", (), False

            keep_turns = 1 if ratio >= 0.90 else 2
            summarized, artifacts, changed = await self._summarize(thread_id, messages, keep_turns=keep_turns)
            if changed:
                after = _messages_tokens(summarized)
                if _saves_enough(estimated, after):
                    await self._set_state(thread_id, ContextState(last_action="forced_summary" if keep_turns == 1 else "summary"))
                    action = "forced_summary" if keep_turns == 1 else "summary"
                    self._publish(thread_id, action, after, artifacts)
                    return summarized, action, artifacts, True
            await self._record_failure(thread_id, "summary_insufficient")
            return messages, "summary_insufficient", (), False
        except Exception as exc:
            await self._record_failure(thread_id, "compression_failed")
            self._publish(thread_id, "compression_failed", estimated, miss_reason=type(exc).__name__)
            return messages, "compression_failed", (), False

    async def _dehydrate(
        self, thread_id: str, messages: list[BaseMessage], *, keep_turns: int
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """将旧的大工具结果归档并替换为首尾预览，完整 user turn 保持原子边界。"""
        if self._thread_store is None:
            return messages, (), False
        cutoff = _cutoff_for_recent_turns(messages, keep_turns)
        if cutoff <= 0:
            return messages, (), False
        replacements = list(messages)
        artifact_ids: list[str] = []
        changed = False
        for index, message in enumerate(messages[:cutoff]):
            if not isinstance(message, ToolMessage):
                continue
            content = _message_content(message)
            if estimate_tokens(content) <= TOOL_RESULT_DEHYDRATE_TOKENS:
                continue
            preview = _tool_preview(content, "pending")
            if not _saves_enough(estimate_tokens(content), estimate_tokens(preview)):
                continue
            artifact = await self._thread_store.archive_context(
                thread_id,
                kind="tool",
                content=_render_message(message),
                source_start=index,
                source_end=index,
            )
            replacements[index] = message.model_copy(
                update={"content": _tool_preview(content, artifact.artifact_id)}
            )
            artifact_ids.append(artifact.artifact_id)
            changed = True
        return replacements, tuple(artifact_ids), changed

    async def _summarize(
        self, thread_id: str, messages: list[BaseMessage], *, keep_turns: int
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """把完整旧 turn 组生成最多 6% 窗口的结构化摘要，并在成功后归档原文。"""
        if self._thread_store is None:
            return messages, (), False
        cutoff = _cutoff_for_recent_turns(messages, keep_turns)
        if cutoff <= 0:
            return messages, (), False
        old, recent = messages[:cutoff], messages[cutoff:]
        summary_input_cap = min(12_000, self._summary_cap())
        summary_input = _clip_to_tokens(_render_messages(old), summary_input_cap)
        response = await self._model.ainvoke(
            [SystemMessage(content=_SUMMARY_PROMPT), HumanMessage(content=summary_input)]
        )
        summary = _message_content(response).strip()
        if not summary or estimate_tokens(summary) > self._summary_cap():
            return messages, (), False
        # 归档必须在摘要和节省率都通过校验后发生。先用等长 ID 占位评估，避免
        # 节省不足时留下无引用的归档或摘要记录。
        pending_artifact_id = "history-" + "0" * 32
        prospective = [
            HumanMessage(
                content=(
                    "<harness_context_summary>\n"
                    f"{summary}\n\n"
                    f"Archived original: /.harness/history/{pending_artifact_id}.md\n"
                    "</harness_context_summary>"
                )
            ),
            *recent,
        ]
        if not _saves_enough(_messages_tokens(messages), _messages_tokens(prospective)):
            return messages, (), False
        # 归档必须在摘要、长度和节省率校验后发生，失败时历史完全不变。
        artifact = await self._thread_store.archive_context(
            thread_id,
            kind="history",
            content=_render_messages(old),
            source_start=0,
            source_end=cutoff - 1,
        )
        await self._thread_store.save_context_summary(
            thread_id,
            rewrite_version=SUMMARY_REWRITE_VERSION,
            content=summary,
            source_start=0,
            source_end=cutoff - 1,
            artifact_ids=(artifact.artifact_id,),
        )
        summary_message = HumanMessage(
            content=(
                "<harness_context_summary>\n"
                f"{summary}\n\n"
                f"Archived original: /.harness/history/{artifact.artifact_id}.md\n"
                "</harness_context_summary>"
            )
        )
        return [summary_message, *recent], (artifact.artifact_id,), True

    async def _overflow_recovery(
        self, thread_id: str, messages: list[BaseMessage]
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """处理一次网关溢出：先工具脱水，仍不足时才强制保留最近一轮摘要。"""
        dehydrated, artifact_ids, changed = await self._dehydrate(thread_id, messages, keep_turns=1)
        if changed:
            self._publish(thread_id, "overflow_tool_dehydration", _messages_tokens(dehydrated), artifact_ids)
            return dehydrated, artifact_ids, True
        summarized, artifact_ids, changed = await self._summarize(thread_id, messages, keep_turns=1)
        if changed:
            self._publish(thread_id, "overflow_summary", _messages_tokens(summarized), artifact_ids)
        return summarized, artifact_ids, changed

    def _summary_cap(self) -> int:
        """把摘要长度限定在 2K 到 12K token，并与窗口大小线性相关。"""
        return max(2_048, min(12_000, int((self._window * 0.06) + 0.999)))

    async def _state(self, thread_id: str) -> ContextState:
        """读取可选持久化状态；无 store 的库调用保持无副作用。"""
        return await self._thread_store.context_state(thread_id) if self._thread_store else ContextState()

    async def _set_state(self, thread_id: str, state: ContextState) -> None:
        """写入成功后的状态并自动清空此前失败次数。"""
        if self._thread_store:
            await self._thread_store.set_context_state(thread_id, state)

    async def _record_failure(self, thread_id: str, action: str) -> None:
        """累计摘要失败，第三次打开熔断器且不再自动重写历史。"""
        if self._thread_store is None:
            return
        previous = await self._thread_store.context_state(thread_id)
        failures = previous.failures + 1
        await self._thread_store.set_context_state(
            thread_id,
            ContextState(failures=failures, circuit_open=failures >= 3, last_action=action),
        )

    def _publish(
        self,
        thread_id: str,
        action: str,
        estimated: int,
        artifact_ids: tuple[str, ...] = (),
        *,
        miss_reason: str | None = None,
    ) -> ContextUpdate:
        """缓冲状态给 server，网关不报告缓存 token 时显式标记为 unknown。"""
        update = ContextUpdate(
            thread_id=thread_id,
            action=action,
            estimated_tokens=estimated,
            input_cap_tokens=self._input_cap,
            context_window_tokens=self._window,
            dynamic_tokens=estimated,
            artifact_ids=artifact_ids,
            miss_reason=miss_reason,
        )
        self._updates.setdefault(thread_id, []).append(update)
        return update


def _thread_id(request: ModelRequest) -> str:
    """优先从 RunContext 获取 thread ID，并拒绝与图配置不一致的调用。"""
    context_thread_id = thread_id_for_runtime(request.runtime)
    if context_thread_id is not None:
        return context_thread_id
    config = getattr(request.runtime, "config", {})
    configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    return str(configurable.get("thread_id") or "ephemeral") if isinstance(configurable, Mapping) else "ephemeral"


def _ordered_request_tools(tools: list[object]) -> list[object]:
    """根据规范化 schema 查找原对象并按稳定键排序，避免改变工具对象本身。"""
    def key(tool: object) -> tuple[str, str, str]:
        schema = normalized_tool_schemas([tool])[0]
        return str(schema["name"]), str(schema["description"]), canonical_json(schema["parameters"])

    return sorted(tools, key=key)


def _estimate_request_tokens(request: ModelRequest, tools: list[object]) -> int:
    """估算 system、消息和 schema 固定开销；供应商 usage 仅作为后续诊断补充。"""
    system = _message_content(request.system_message) if request.system_message is not None else ""
    return estimate_tokens(system) + _messages_tokens(request.messages) + estimate_tokens(canonical_json(normalized_tool_schemas(tools))) + (len(request.messages) + len(tools)) * 8


def _messages_tokens(messages: list[BaseMessage]) -> int:
    """计算消息正文、工具调用元数据和每条消息固定结构的近似预算。"""
    return sum(estimate_tokens(_render_message(message)) + 8 for message in messages)


def _message_content(message: object) -> str:
    """将 LangChain 文本或内容块降为稳定文本，避免对象 repr 进入摘要。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return str(content) if content is not None else ""


def _render_message(message: BaseMessage) -> str:
    """把消息以稳定角色标签序列化到归档或摘要输入。"""
    payload: dict[str, object] = {"type": message.type, "content": _message_content(message)}
    if isinstance(message, AIMessage) and message.tool_calls:
        payload["tool_calls"] = message.tool_calls
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
        payload["name"] = message.name
    return canonical_json(payload)


def _render_messages(messages: list[BaseMessage]) -> str:
    """用逐行 JSON 保留消息原子边界，方便摘要模型引用事实而不执行内容。"""
    return "\n".join(_render_message(message) for message in messages)


def _clip_to_tokens(content: str, token_cap: int) -> str:
    """按 UTF-8 保守字节界裁剪摘要输入，永远不超过 12K token。"""
    cap = token_cap * 4
    data = content.encode("utf-8")
    if len(data) <= cap:
        return content
    return data[:cap].decode("utf-8", errors="ignore") + "\n[older context clipped for summary input]"


def _tool_preview(content: str, artifact_id: str) -> str:
    """构造首尾各 200 字符与 artifact 指针，供模型决定是否按需恢复原文。"""
    return (
        f"{content[:TOOL_RESULT_PREVIEW_CHARS]}\n"
        f"[tool result dehydrated: /.harness/history/{artifact_id}.md]\n"
        f"{content[-TOOL_RESULT_PREVIEW_CHARS:]}"
    )


def _cutoff_for_recent_turns(messages: list[BaseMessage], keep_turns: int) -> int:
    """在完整 user turn 前切分，assistant tool-call 与对应结果绝不会被拆开。"""
    starts = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if len(starts) <= keep_turns:
        return 0
    return starts[-keep_turns]


def _saves_enough(before_tokens: int, after_tokens: int) -> bool:
    """只在预计节省至少 20% 时改写历史，避免压缩反而增加上下文。"""
    return before_tokens > 0 and after_tokens < before_tokens and (before_tokens - after_tokens) / before_tokens >= 0.20
