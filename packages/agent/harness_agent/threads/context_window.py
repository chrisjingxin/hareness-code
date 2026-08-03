"""上下文预算、工具结果归档和低频结构化压缩中间件。

上下文重写通过一个深模块完成：调用方只提供窗口、模型和 ThreadPersistence；该模块
负责预算、完整 turn 原子组、归档、摘要、失败熔断和可观测状态。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from harness_agent.threads.prompting import (
    HISTORY_REWRITE_VERSION,
    canonical_json,
    estimate_tokens,
    input_cap_tokens,
    normalized_tool_schemas,
)
from harness_agent.context_pressure import (
    ContextPressurePolicy,
    ContextPressureSnapshot,
    ModelCallType,
)
from harness_agent.run_context import thread_id_for_runtime
from harness_agent.context_projection import (
    CompressionCheckpointDraft,
    ContextProjector,
    artifact_references,
)
from harness_agent.thread_persistence import (
    CommitContextRewrite,
    ContextArtifactDraft,
    ContextState,
    ContextSummaryDraft,
    ThreadPersistence,
)

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


@dataclass(frozen=True, slots=True)
class _MicroCompressionPlan:
    """尚未提交的确定性工具归档及其新投影。"""

    messages: tuple[BaseMessage, ...]
    artifacts: tuple[ContextArtifactDraft, ...]
    before_tokens: int
    after_tokens: int


class ContextWindowMiddleware(AgentMiddleware):
    """在模型调用前按 50/60/80/90 阈值管理上下文，而非高频改写历史。"""

    def __init__(
        self,
        model: "BaseChatModel",
        *,
        context_window_tokens: int,
        thread_persistence: ThreadPersistence | None = None,
        updates: dict[str, list[ContextUpdate]] | None = None,
        pressure_policy: ContextPressurePolicy | None = None,
    ) -> None:
        """绑定模型窗口与可选本机持久化；没有 ThreadPersistence 时不丢弃任何历史。"""
        super().__init__()
        self._model = model
        self._window = context_window_tokens
        self._input_cap = input_cap_tokens(context_window_tokens)
        self._thread_persistence = thread_persistence
        self._updates = updates if updates is not None else {}
        self._pressure_policy = pressure_policy or ContextPressurePolicy()

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
        if self._thread_persistence is None:
            return messages, self._publish(
                thread_id,
                "manual_compaction_unavailable",
                estimated,
                miss_reason="thread persistence is unavailable",
            ), False
        try:
            compacted, artifacts, changed = await self._summarize(
                thread_id,
                messages,
                keep_turns=2,
                state=ContextState(last_action="manual_summary"),
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
        call_type, idle_duration_ms = _next_model_call(request.runtime)

        try:
            prepared, action, artifact_ids, rewrite = await self._prepare(
                thread_id,
                request.messages,
                estimated,
                call_type=call_type,
                idle_duration_ms=idle_duration_ms,
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
                            *ContextProjector.cache_rewrite(prepared),
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
        *,
        call_type: ModelCallType = "unclassified",
        idle_duration_ms: int | None = None,
    ) -> tuple[list[BaseMessage], str, tuple[str, ...], bool]:
        """执行 pressure policy：报告、微压缩、重新计量，再按需完整摘要。"""
        pressure_before = self._measure_pressure(
            messages,
            estimated,
            idle_duration_ms=idle_duration_ms,
        )
        decision = self._pressure_policy.decide(
            pressure_before,
            call_type=call_type,
        )
        if decision.action == "none":
            return messages, "within_budget", (), False
        if decision.action == "report":
            self._publish(thread_id, "report", estimated)
            return messages, "report", (), False
        if self._thread_persistence is None:
            # A preview without its durable Artifact would leave an
            # unrecoverable virtual path in the model cache.  Fail closed for
            # every automatic compression action, including overflow-adjacent
            # callers that enter through this preparation path.
            self._publish(
                thread_id,
                "micro_skipped",
                estimated,
                miss_reason="thread persistence is unavailable",
            )
            return messages, "micro_skipped", (), False

        state = await self._state(thread_id)
        if state.circuit_open:
            self._publish(
                thread_id,
                "circuit_open",
                estimated,
                miss_reason="three compression failures",
            )
            return messages, "circuit_open", (), False

        try:
            keep_turns = (
                1
                if pressure_before.occupancy_ratio >= self._pressure_policy.config.hard_ratio
                else 2
            )
            micro_plan = await self._plan_dehydrate(
                messages,
                keep_turns=keep_turns,
                keep_recent=decision.keep_recent,
                estimated_tokens=estimated,
            )
            if micro_plan is not None:
                pressure_after = self._measure_pressure(
                    list(micro_plan.messages),
                    micro_plan.after_tokens,
                )
                # This is the second, new policy decision.  The old estimate is
                # never used to decide whether the full model path is still needed.
                after_decision = self._pressure_policy.decide(
                    pressure_after,
                    call_type=call_type,
                )
                if after_decision.action != "full":
                    committed = await self._commit_micro_plan(
                        thread_id,
                        micro_plan,
                        trigger=decision.reason,
                        pressure_before=pressure_before,
                        pressure_after=pressure_after,
                        state=ContextState(
                            last_action=(
                                "idle_micro"
                                if decision.reason == "idle"
                                else "pressure_micro"
                            )
                        ),
                    )
                    action = (
                        "idle_micro" if decision.reason == "idle" else "pressure_micro"
                    )
                    self._publish(
                        thread_id,
                        action,
                        micro_plan.after_tokens,
                        committed,
                    )
                    return list(micro_plan.messages), action, committed, True

                # The micro plan is deliberately kept in memory only while the
                # same request continues into full summarization.  Its Artifact
                # drafts are committed together with the final full checkpoint.
                messages_for_full = list(micro_plan.messages)
                micro_artifacts = micro_plan.artifacts
                full_before = pressure_after
                full_estimated = micro_plan.after_tokens
                keep_turns = (
                    1
                    if pressure_after.occupancy_ratio
                    >= self._pressure_policy.config.hard_ratio
                    else 2
                )
            else:
                if decision.action != "full":
                    self._publish(
                        thread_id,
                        "micro_skipped",
                        estimated,
                        miss_reason=decision.reason,
                    )
                    return messages, "micro_skipped", (), False
                messages_for_full = messages
                micro_artifacts = ()
                full_before = pressure_before
                full_estimated = estimated

            summarized, artifacts, changed = await self._summarize(
                thread_id,
                messages_for_full,
                keep_turns=keep_turns,
                state=ContextState(
                    last_action=(
                        "forced_summary"
                        if keep_turns == 1
                        else "summary"
                    )
                ),
                base_artifacts=micro_artifacts,
                pressure_before=full_before,
                estimated_tokens=full_estimated,
            )
            if changed:
                after = self._estimate_rewritten_tokens(
                    full_estimated,
                    messages_for_full,
                    summarized,
                )
                action = "forced_summary" if keep_turns == 1 else "summary"
                self._publish(thread_id, action, after, artifacts)
                return summarized, action, artifacts, True
            await self._record_failure(thread_id, "summary_insufficient")
            return messages, "summary_insufficient", (), False
        except Exception as exc:
            await self._record_failure(thread_id, "compression_failed")
            self._publish(
                thread_id,
                "compression_failed",
                estimated,
                miss_reason=type(exc).__name__,
            )
            return messages, "compression_failed", (), False

    async def _plan_dehydrate(
        self,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        keep_recent: int,
        estimated_tokens: int | None = None,
    ) -> _MicroCompressionPlan | None:
        """只在内存中规划工具归档，不提前写 Artifact 或 checkpoint。"""
        cutoff = _cutoff_for_recent_turns(messages, keep_turns)
        if cutoff <= 0:
            return None
        candidates = _dehydratable_tool_candidates(messages, cutoff)
        reclaimable = candidates[:-max(1, keep_recent)]
        if not reclaimable:
            return None

        replacements = list(messages)
        drafts: list[ContextArtifactDraft] = []
        for index, message, tool_name in reclaimable:
            content = _message_content(message)
            artifact_id = f"tool-{uuid.uuid4().hex}"
            preview = _tool_preview(
                content,
                artifact_id,
                tool_name=tool_name,
            )
            if not _saves_enough(estimate_tokens(content), estimate_tokens(preview)):
                continue
            replacements[index] = message.model_copy(update={"content": preview})
            drafts.append(
                ContextArtifactDraft(
                    kind="tool",
                    content=_render_message(message),
                    source_start=index,
                    source_end=index,
                    artifact_id=artifact_id,
                )
            )
        before_tokens = (
            estimated_tokens
            if estimated_tokens is not None
            else _messages_tokens(messages)
        )
        after_tokens = self._estimate_rewritten_tokens(
            before_tokens,
            messages,
            replacements,
        )
        if not drafts or not _saves_enough(before_tokens, after_tokens):
            return None
        return _MicroCompressionPlan(
            messages=tuple(replacements),
            artifacts=tuple(drafts),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    async def _dehydrate(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        keep_recent: int = 1,
        state: ContextState | None = None,
        estimated_tokens: int | None = None,
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """提交一份独立 micro checkpoint；自动升级路径使用未提交的 plan。"""
        if self._thread_persistence is None:
            return messages, (), False
        plan = await self._plan_dehydrate(
            messages,
            keep_turns=keep_turns,
            keep_recent=keep_recent,
            estimated_tokens=estimated_tokens,
        )
        if plan is None:
            return messages, (), False
        before = self._measure_pressure(
            messages,
            plan.before_tokens,
        )
        after = self._measure_pressure(list(plan.messages), plan.after_tokens)
        artifact_ids = await self._commit_micro_plan(
            thread_id,
            plan,
            trigger=state.last_action if state is not None else "automatic",
            pressure_before=before,
            pressure_after=after,
            state=state,
        )
        return list(plan.messages), artifact_ids, True

    async def _commit_micro_plan(
        self,
        thread_id: str,
        plan: _MicroCompressionPlan,
        *,
        trigger: str,
        pressure_before: ContextPressureSnapshot,
        pressure_after: ContextPressureSnapshot,
        state: ContextState | None,
    ) -> tuple[str, ...]:
        """把确定性微压缩和其 Artifact 在同一事务中提交。"""
        if self._thread_persistence is None:
            raise RuntimeError("CONTEXT_PERSISTENCE_REQUIRED")
        committed = await self._thread_persistence.commit_context(
            CommitContextRewrite(
                thread_id=thread_id,
                artifacts=plan.artifacts,
                state=state,
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id=f"projection-{uuid.uuid4().hex}",
                    mode="micro",
                    rewrite_version=SUMMARY_REWRITE_VERSION,
                    projected_messages=plan.messages,
                    artifact_ids=artifact_references(plan.messages),
                    trigger=trigger,
                    pressure_before=pressure_before.record(),
                    pressure_after=pressure_after.record(),
                ),
            )
        )
        return tuple(artifact.artifact_id for artifact in committed.artifacts)

    def _measure_pressure(
        self,
        messages: list[BaseMessage],
        estimated_tokens: int,
        *,
        idle_duration_ms: int | None = None,
    ) -> ContextPressureSnapshot:
        """用当前投影重新统计可回收工具压力，不依赖旧快照。"""
        keep_recent = self._pressure_policy.config.keep_recent
        count, tokens = _reclaimable_tool_pressure(
            messages,
            keep_turns=2,
            keep_recent=keep_recent,
        )
        return self._pressure_policy.measure(
            estimated_tokens,
            self._input_cap,
            reclaimable_tool_tokens=tokens,
            reclaimable_tool_count=count,
            idle_duration_ms=idle_duration_ms,
        )

    @staticmethod
    def _estimate_rewritten_tokens(
        before_tokens: int,
        before: Sequence[BaseMessage],
        after: Sequence[BaseMessage],
    ) -> int:
        """保留 system/schema 固定开销，只替换投影消息的估算量。"""
        return max(
            0,
            before_tokens - _messages_tokens(list(before)) + _messages_tokens(list(after)),
        )

    async def _summarize(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        state: ContextState | None = None,
        base_artifacts: Sequence[ContextArtifactDraft] = (),
        pressure_before: ContextPressureSnapshot | None = None,
        estimated_tokens: int | None = None,
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """把完整旧 turn 组生成最多 6% 窗口的结构化摘要，并在成功后归档原文。"""
        if self._thread_persistence is None:
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
        artifact_id = f"history-{uuid.uuid4().hex}"
        prospective = [
            HumanMessage(
                content=(
                    "<harness_context_summary>\n"
                    f"{summary}\n\n"
                    f"Archived original: /.harness/history/{artifact_id}.md\n"
                    "</harness_context_summary>"
                )
            ),
            *recent,
        ]
        before_tokens = (
            estimated_tokens
            if estimated_tokens is not None
            else _messages_tokens(messages)
        )
        after_tokens = self._estimate_rewritten_tokens(
            before_tokens,
            messages,
            prospective,
        )
        if not _saves_enough(before_tokens, after_tokens):
            return messages, (), False
        # 归档必须在摘要、长度和节省率校验后发生，失败时历史完全不变。
        summary_artifact = ContextArtifactDraft(
            kind="history",
            content=_render_messages(old),
            source_start=0,
            source_end=cutoff - 1,
            artifact_id=artifact_id,
        )
        all_artifacts = tuple(base_artifacts) + (summary_artifact,)
        all_artifact_ids = tuple(
            artifact.artifact_id
            for artifact in all_artifacts
            if artifact.artifact_id is not None
        )
        summary_artifact_indexes = tuple(range(len(all_artifacts)))
        checkpoint_artifact_ids = tuple(
            dict.fromkeys((*artifact_references(prospective), *all_artifact_ids))
        )
        before_record = (
            pressure_before.record()
            if pressure_before is not None
            else {"estimated_tokens": before_tokens}
        )
        after_record = self._measure_pressure(
            list(prospective),
            after_tokens,
        ).record()
        committed = await self._thread_persistence.commit_context(
            CommitContextRewrite(
                thread_id=thread_id,
                artifacts=all_artifacts,
                summary=ContextSummaryDraft(
                    rewrite_version=SUMMARY_REWRITE_VERSION,
                    content=summary,
                    source_start=0,
                    source_end=cutoff - 1,
                    artifact_indexes=summary_artifact_indexes,
                ),
                state=state,
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id=f"projection-{uuid.uuid4().hex}",
                    mode="full",
                    rewrite_version=SUMMARY_REWRITE_VERSION,
                    projected_messages=tuple(prospective),
                    artifact_ids=checkpoint_artifact_ids,
                    trigger=state.last_action if state is not None else "automatic",
                    pressure_before=before_record,
                    pressure_after=after_record,
                ),
            )
        )
        return prospective, tuple(
            artifact.artifact_id for artifact in committed.artifacts
        ), True

    async def _overflow_recovery(
        self, thread_id: str, messages: list[BaseMessage]
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """处理一次网关溢出：先工具脱水，仍不足时才强制保留最近一轮摘要。"""
        dehydrated, artifact_ids, changed = await self._dehydrate(
            thread_id,
            messages,
            keep_turns=1,
            state=ContextState(last_action="overflow_tool_dehydration"),
        )
        if changed:
            self._publish(thread_id, "overflow_tool_dehydration", _messages_tokens(dehydrated), artifact_ids)
            return dehydrated, artifact_ids, True
        summarized, artifact_ids, changed = await self._summarize(
            thread_id,
            messages,
            keep_turns=1,
            state=ContextState(last_action="overflow_summary"),
        )
        if changed:
            self._publish(thread_id, "overflow_summary", _messages_tokens(summarized), artifact_ids)
        return summarized, artifact_ids, changed

    def _summary_cap(self) -> int:
        """把摘要长度限定在 2K 到 12K token，并与窗口大小线性相关。"""
        return max(2_048, min(12_000, int((self._window * 0.06) + 0.999)))

    async def _state(self, thread_id: str) -> ContextState:
        """读取可选持久化状态；无 persistence 的库调用保持无副作用。"""
        if self._thread_persistence is None:
            return ContextState()
        return await self._thread_persistence.load_context_state(thread_id)

    async def _record_failure(self, thread_id: str, action: str) -> None:
        """累计摘要失败，第三次打开熔断器且不再自动重写历史。"""
        if self._thread_persistence is None:
            return
        previous = await self._thread_persistence.load_context_state(thread_id)
        failures = previous.failures + 1
        await self._thread_persistence.commit_context(
            CommitContextRewrite(
                thread_id=thread_id,
                state=ContextState(
                    failures=failures,
                    circuit_open=failures >= 3,
                    last_action=action,
                ),
            )
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


def _tool_preview(
    content: str,
    artifact_id: str,
    *,
    tool_name: str = "tool",
) -> str:
    """构造带工具名、摘要预览、哈希和 Artifact 指针的确定性占位。"""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (
        f"[tool={tool_name}]\n"
        f"Summary preview:\n{content[:TOOL_RESULT_PREVIEW_CHARS]}\n"
        f"...\n{content[-TOOL_RESULT_PREVIEW_CHARS:]}\n"
        f"sha256={digest}\n"
        f"Artifact: /.harness/history/{artifact_id}.md"
    )


def _cutoff_for_recent_turns(messages: list[BaseMessage], keep_turns: int) -> int:
    """在完整 user turn 前切分，assistant tool-call 与对应结果绝不会被拆开。"""
    starts = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if len(starts) <= keep_turns:
        return 0
    return starts[-keep_turns]


def _dehydratable_tool_candidates(
    messages: Sequence[BaseMessage], cutoff: int
) -> list[tuple[int, ToolMessage, str]]:
    """只返回已完成、配对且尚未指向 Artifact 的旧工具结果。"""
    active_calls: dict[str, tuple[int, str]] = {}
    candidates: list[tuple[int, ToolMessage, str]] = []
    for index, message in enumerate(messages[:cutoff]):
        if isinstance(message, AIMessage):
            if active_calls:
                # 不能跨越另一个 assistant 边界猜测工具归属。
                active_calls.clear()
            for call in message.tool_calls or ():
                if isinstance(call, Mapping):
                    call_id = call.get("id")
                    if isinstance(call_id, str) and call_id:
                        call_name = call.get("name")
                        active_calls[call_id] = (
                            index,
                            call_name if isinstance(call_name, str) and call_name else "tool",
                        )
            continue
        if isinstance(message, HumanMessage):
            active_calls.clear()
            continue
        if not isinstance(message, ToolMessage):
            active_calls.clear()
            continue
        call_id = message.tool_call_id
        call_info = active_calls.pop(call_id, None)
        if call_info is None:
            continue
        _call_index, call_name = call_info
        content = _message_content(message)
        if artifact_references((message,)):
            continue
        if estimate_tokens(content) <= TOOL_RESULT_DEHYDRATE_TOKENS:
            continue
        if not _saves_enough(
            estimate_tokens(content),
            estimate_tokens(
                _tool_preview(
                    content,
                    "tool-preview",
                    tool_name=message.name or call_name,
                )
            ),
        ):
            continue
        candidates.append((index, message, message.name or call_name))
    return candidates


def _reclaimable_tool_pressure(
    messages: Sequence[BaseMessage], *, keep_turns: int, keep_recent: int
) -> tuple[int, int]:
    """统计实际可被本次微压缩释放的工具结果，而非所有历史工具结果。"""
    cutoff = _cutoff_for_recent_turns(list(messages), keep_turns)
    candidates = _dehydratable_tool_candidates(messages, cutoff)
    reclaimable = candidates[:-max(1, keep_recent)]
    return len(reclaimable), sum(
        estimate_tokens(_message_content(message))
        for _index, message, _tool_name in reclaimable
    )


def _next_model_call(runtime: object) -> tuple[ModelCallType, int | None]:
    """从显式 Run 生命周期消费调用类型；缺少上下文时关闭 idle 触发。"""
    context = getattr(runtime, "context", None)
    lifecycle = getattr(context, "model_call_lifecycle", None)
    begin = getattr(lifecycle, "begin", None)
    if not callable(begin):
        return "unclassified", None
    value = begin()
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], (int, type(None)))
    ):
        return value[0], value[1]  # type: ignore[return-value]
    return "unclassified", None


def _saves_enough(before_tokens: int, after_tokens: int) -> bool:
    """只在预计节省至少 20% 时改写历史，避免压缩反而增加上下文。"""
    return before_tokens > 0 and after_tokens < before_tokens and (before_tokens - after_tokens) / before_tokens >= 0.20
