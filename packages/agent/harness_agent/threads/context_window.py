"""上下文预算中间件与统一压缩服务的运行期适配层。

真正的自动、手动和 overflow 压缩只在 :mod:`context_compaction` 中实现。本模块
只负责读取当前模型调用的 canonical projection、把 typed result 转成
``context.updated``，以及在模型第一次报告 overflow 后最多重试一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import BaseMessage
from langgraph.types import Command

from harness_agent.threads.context_compaction import (
    CompressionRequest,
    CompressionResult,
    ContextCompactor,
)
from harness_agent.threads.context_pressure import (
    ContextPressurePolicy,
    ContextPressureSnapshot,
    ModelCallType,
)
from harness_agent.threads.context_projection import (
    ContextProjector,
    ModelProjection,
    encode_projected_messages,
    validate_atomic_message_groups,
)
from harness_agent.threads.prompting import (
    canonical_json,
    estimate_tokens,
    input_cap_tokens,
    normalized_tool_schemas,
)
from harness_agent.runtime.run_context import RunContext, thread_id_for_runtime
from harness_agent.threads.thread_persistence import ContextState, ThreadPersistence


_SAFE_CONTEXT_WIRE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _safe_context_wire_token(value: object, *, fallback: str) -> str:
    """只把稳定短标识放到 context.updated，拒绝路径和原始诊断文本。"""
    if isinstance(value, str) and _SAFE_CONTEXT_WIRE_TOKEN.fullmatch(value):
        return value
    return fallback


def _safe_context_wire_reason(value: object) -> str | None:
    """将 miss_reason 限制为可诊断码，不回传异常、提示词或工具内容。"""
    if value is None:
        return None
    return _safe_context_wire_token(value, fallback="diagnostic_unavailable")

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from langchain_core.language_models import BaseChatModel


@dataclass(frozen=True, slots=True)
class ContextUpdate:
    """一次模型请求的上下文状态，供 server 转成 ``context.updated``。"""

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
            "action": _safe_context_wire_token(self.action, fallback="context_unknown"),
            "estimated_tokens": self.estimated_tokens,
            "input_cap_tokens": self.input_cap_tokens,
            "context_window_tokens": self.context_window_tokens,
            "dynamic_tokens": self.dynamic_tokens,
            "cache_status": _safe_context_wire_token(
                self.cache_status, fallback="unknown"
            ),
            "cached_tokens": self.cached_tokens,
            "miss_reason": _safe_context_wire_reason(self.miss_reason),
            "artifact_ids": [
                _safe_context_wire_token(artifact_id, fallback="artifact_redacted")
                for artifact_id in self.artifact_ids
            ],
        }


class ContextWindowMiddleware(AgentMiddleware):
    """在模型边界接入唯一的 ContextCompactor。"""

    def __init__(
        self,
        model: "BaseChatModel",
        *,
        context_window_tokens: int,
        thread_persistence: ThreadPersistence | None = None,
        updates: dict[str, list[ContextUpdate]] | None = None,
        pressure_policy: ContextPressurePolicy | None = None,
        runtime_state_provider: Callable[..., Any] | None = None,
    ) -> None:
        """绑定模型窗口、project-scoped persistence 和运行态读取器。"""
        super().__init__()
        self._model = model
        self._window = context_window_tokens
        self._input_cap = input_cap_tokens(context_window_tokens)
        self._thread_persistence = thread_persistence
        self._updates = updates if updates is not None else {}
        self._pressure_policy = pressure_policy or ContextPressurePolicy()
        self._compactor = (
            ContextCompactor(
                model,
                context_window_tokens=context_window_tokens,
                thread_persistence=thread_persistence,
                pressure_policy=self._pressure_policy,
                runtime_state_provider=runtime_state_provider,
            )
            if thread_persistence is not None
            else None
        )

    @property
    def compactor(self) -> ContextCompactor | None:
        """返回统一领域服务，供 server 的手动入口复用同一实现。"""
        return self._compactor

    def consume_updates(self, thread_id: str) -> tuple[ContextUpdate, ...]:
        """读取并清空指定 thread 的待发送状态。"""
        return tuple(self._updates.pop(thread_id, []))

    async def compact_now(
        self,
        request_or_thread: CompressionRequest | str,
        messages: list[BaseMessage] | None = None,
    ) -> CompressionResult | tuple[list[BaseMessage], ContextUpdate, bool]:
        """执行 typed 手动压缩；旧 positional 形状仅作为测试兼容网关。"""
        if isinstance(request_or_thread, CompressionRequest):
            result = await self._compress_typed(request_or_thread)
            self._publish_result(request_or_thread.thread_id, result)
            return result
        if messages is None:
            raise TypeError("COMPRESSION_TYPED_REQUEST_REQUIRED")
        # 旧调用方不能参与生产路径；它仍然经过同一个 service，避免保留第二套
        # SQL/摘要实现。正式 server 使用上面的 CompressionRequest 分支。
        request = await self._legacy_request(
            request_or_thread,
            messages,
            trigger="manual",
            estimated_tokens=_messages_tokens(messages),
        )
        if request is None or self._compactor is None:
            estimated = _messages_tokens(messages)
            update = self._publish(
                request_or_thread,
                "manual_compaction_unavailable",
                estimated,
                miss_reason="thread persistence is unavailable",
            )
            return messages, update, False
        result = await self._compactor.compress(request)
        legacy_action = _legacy_manual_action(result)
        update = self._publish_result(
            request.thread_id,
            result,
            action=legacy_action,
        )
        return list(result.projected_messages), update, result.compressed

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Awaitable[ModelResponse]]",
    ) -> ModelResponse | ExtendedModelResponse:
        """自动压缩后调用模型，并将 overflow 恢复限制为一次重试。"""
        thread_id = _thread_id(request)
        ordered_tools = _ordered_request_tools(list(request.tools or ()))
        estimated = _estimate_request_tokens(request, ordered_tools)
        call_type, idle_duration_ms = _next_model_call(request.runtime)

        prepared = list(request.messages)
        rewrite = False
        projection = await self._canonical_projection(
            thread_id,
            request.messages,
            allow_legacy=not isinstance(_run_context(request.runtime), RunContext),
        )
        if projection is not None and self._compactor is not None:
            pressure = self._measure_pressure(
                request.messages,
                estimated,
                idle_duration_ms=idle_duration_ms,
            )
            decision = self._pressure_policy.decide(pressure, call_type=call_type)
            if decision.action in {"micro", "full"}:
                typed_request = CompressionRequest(
                    thread_id=thread_id,
                    trigger="auto",
                    projection=projection,
                    run_context_snapshot=_run_context_snapshot(request.runtime),
                    run_context=_run_context(request.runtime),
                    pressure_before=pressure,
                    estimated_tokens=estimated,
                    call_type=call_type,
                )
                compression_result = await self._compactor.compress(typed_request)
                self._publish_result(thread_id, compression_result)
                if compression_result.compressed:
                    prepared = list(compression_result.projected_messages)
                    rewrite = True
            elif decision.action == "report":
                self._publish(thread_id, "report", estimated)

        try:
            result = await handler(
                request.override(messages=prepared, tools=ordered_tools)
            )
        except ContextOverflowError as first_overflow:
            # 不把 ContextCompactor 的摘要模型异常带进这里；只有真正的模型
            # handler overflow 才进入一次 recovery，避免递归压缩循环。
            overflow_estimated = _estimate_request_tokens(
                request.override(messages=prepared, tools=ordered_tools),
                ordered_tools,
            )
            recovery = await self._overflow_once(
                request,
                thread_id=thread_id,
                messages=prepared,
                estimated_tokens=overflow_estimated,
                call_type=call_type,
            )
            if recovery is None or not recovery.compressed:
                reason = recovery.reason if recovery is not None else "persistence_unavailable"
                safe_reason = _safe_context_wire_reason(reason) or "diagnostic_unavailable"
                self._publish(
                    thread_id,
                    "overflow_failed",
                    estimated,
                    miss_reason=safe_reason,
                )
                raise ContextOverflowError(
                    f"CONTEXT_OVERFLOW_RECOVERY_FAILED:{safe_reason}"
                ) from first_overflow
            prepared = list(recovery.projected_messages)
            rewrite = True
            try:
                result = await handler(
                    request.override(messages=prepared, tools=ordered_tools)
                )
            except ContextOverflowError as second_overflow:
                self._publish(
                    thread_id,
                    "overflow_failed",
                    recovery.estimated_tokens,
                    recovery.artifact_ids,
                    miss_reason="second_overflow_after_single_retry",
                )
                raise ContextOverflowError(
                    "CONTEXT_OVERFLOW_AFTER_RECOVERY"
                ) from second_overflow

        if rewrite:
            return ExtendedModelResponse(
                model_response=result,
                command=Command(
                    update={
                        "messages": [
                            *ContextProjector.cache_rewrite(prepared),
                            # 附加 Command 在模型结果之后应用；保留本轮回答或
                            # tool-call，不能只写入重写后的历史。
                            *result.result,
                        ]
                    }
                ),
            )
        return result

    async def _compress_typed(self, request: CompressionRequest) -> CompressionResult:
        """执行 typed service；不存在持久化时返回明确 skipped。"""
        if self._compactor is None:
            return CompressionResult(
                outcome="skipped",
                trigger=request.trigger,
                action=f"{request.trigger}_skipped",
                projected_messages=request.projection.messages,
                estimated_tokens=request.estimated_tokens or _messages_tokens(request.projection.messages),
                input_cap_tokens=self._input_cap,
                reason="thread persistence is unavailable",
            )
        return await self._compactor.compress(request)

    async def _overflow_once(
        self,
        request: ModelRequest,
        *,
        thread_id: str,
        messages: Sequence[BaseMessage],
        estimated_tokens: int,
        call_type: ModelCallType,
    ) -> CompressionResult | None:
        """只为第一次模型 overflow 创建一次 typed recovery 请求。"""
        if self._compactor is None:
            return None
        projection = await self._canonical_projection(
            thread_id,
            messages,
            allow_legacy=not isinstance(_run_context(request.runtime), RunContext),
        )
        if projection is None:
            return CompressionResult(
                outcome="failed",
                trigger="overflow",
                action="overflow_failed",
                projected_messages=tuple(messages),
                estimated_tokens=estimated_tokens,
                input_cap_tokens=self._input_cap,
                reason="canonical_projection_unavailable",
            )
        pressure = self._measure_pressure(messages, estimated_tokens)
        result = await self._compactor.compress(
            CompressionRequest(
                thread_id=thread_id,
                trigger="overflow",
                projection=projection,
                run_context_snapshot=_run_context_snapshot(request.runtime),
                run_context=_run_context(request.runtime),
                pressure_before=pressure,
                estimated_tokens=estimated_tokens,
                call_type=call_type,
            )
        )
        self._publish_result(thread_id, result)
        return result

    async def _canonical_projection(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        allow_legacy: bool = False,
    ) -> ModelProjection | None:
        """只接受 Transcript + latest-valid checkpoint 与模型输入相同的投影。"""
        if self._thread_persistence is None:
            return None
        try:
            projection = await ContextProjector(self._thread_persistence).project(thread_id)
            if encode_projected_messages(projection.messages) != encode_projected_messages(messages):
                # 生产路径 fail closed；当前调用若尚未 flush 到 Transcript，不能
                # 把内存缓存伪装成可审计 checkpoint。
                # 非 shared-engine 的嵌入式兼容图没有 RunContext，也没有生产
                # Transcript flush 边界；它只用于旧库级测试，不进入 Host 生产路径。
                if allow_legacy:
                    records = await self._thread_persistence.load_transcript(thread_id)
                    return ModelProjection(
                        messages=tuple(messages),
                        checkpoint=None,
                        tail_start_sequence=0,
                        source_record_sequence=records[-1].sequence if records else 0,
                    )
                return None
            return projection
        except Exception:
            return None

    async def _legacy_request(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        trigger: str,
        estimated_tokens: int,
    ) -> CompressionRequest | None:
        """为旧测试/嵌入式调用构造最小 projection，不被生产入口使用。"""
        if self._thread_persistence is None:
            return None
        validate_atomic_message_groups(messages)
        records = await self._thread_persistence.load_transcript(thread_id)
        source_sequence = records[-1].sequence if records else 0
        return CompressionRequest(
            thread_id=thread_id,
            trigger=trigger,  # type: ignore[arg-type]
            projection=ModelProjection(
                messages=tuple(messages),
                checkpoint=None,
                tail_start_sequence=0,
                source_record_sequence=source_sequence,
            ),
            estimated_tokens=estimated_tokens,
        )

    async def _prepare(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        estimated: int,
        *,
        call_type: ModelCallType = "unclassified",
        idle_duration_ms: int | None = None,
    ) -> tuple[list[BaseMessage], str, tuple[str, ...], bool]:
        """旧 middleware 单测网关；内部仍只调用 ContextCompactor。"""
        pressure = self._measure_pressure(
            messages, estimated, idle_duration_ms=idle_duration_ms
        )
        decision = self._pressure_policy.decide(pressure, call_type=call_type)
        if decision.action == "none":
            return messages, "within_budget", (), False
        if decision.action == "report":
            self._publish(thread_id, "report", estimated)
            return messages, "report", (), False
        if self._compactor is None:
            self._publish(
                thread_id,
                "micro_skipped",
                estimated,
                miss_reason="thread persistence is unavailable",
            )
            return messages, "micro_skipped", (), False
        request = await self._legacy_request(
            thread_id,
            messages,
            trigger="auto",
            estimated_tokens=estimated,
        )
        if request is None:
            return messages, "micro_skipped", (), False
        request = replace(request, pressure_before=pressure, call_type=call_type)
        result = await self._compactor.compress(request)
        if result.compressed:
            action = _legacy_auto_action(result, pressure)
            self._publish_result(thread_id, result, action=action)
            return list(result.projected_messages), action, result.artifact_ids, True
        if result.action == "auto_skipped_circuit_open":
            action = "circuit_open"
        elif decision.action == "micro":
            action = "micro_skipped"
        elif result.outcome == "failed":
            # 旧调用方把空/截断摘要归为 insufficient；typed result 仍在
            # context.updated 中保留失败原因。
            action = "summary_insufficient"
        else:
            action = "summary_insufficient"
        self._publish_result(thread_id, result, action=action)
        return messages, action, (), False

    async def _plan_dehydrate(
        self,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        keep_recent: int,
        estimated_tokens: int | None = None,
    ) -> Any:
        """旧测试网关，直接复用统一服务的确定性 micro planner。"""
        if self._compactor is None:
            return None
        return self._compactor._plan_micro(  # noqa: SLF001
            "legacy",
            messages,
            keep_turns=keep_turns,
            keep_recent=keep_recent,
            before_tokens=estimated_tokens or _messages_tokens(messages),
            trigger="auto",
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
        """旧测试网关；micro 仍通过 ContextCompactor 的事务入口提交。"""
        if self._compactor is None:
            return messages, (), False
        plan = self._compactor._plan_micro(  # noqa: SLF001
            thread_id,
            messages,
            keep_turns=keep_turns,
            keep_recent=keep_recent,
            before_tokens=estimated_tokens or _messages_tokens(messages),
            trigger="overflow" if state and state.last_action.startswith("overflow") else "auto",
        )
        if plan is None:
            return messages, (), False
        request = await self._legacy_request(
            thread_id,
            messages,
            trigger="overflow" if state and state.last_action.startswith("overflow") else "auto",
            estimated_tokens=plan.before_tokens,
        )
        if request is None:
            return messages, (), False
        before = self._measure_pressure(messages, plan.before_tokens)
        after = self._measure_pressure(list(plan.messages), plan.after_tokens)
        runtime_state = state or ContextState(last_action="auto_micro")
        result = await self._compactor._commit_projection(  # noqa: SLF001
            request,
            messages=plan.messages,
            artifacts=plan.artifacts,
            summary=None,
            state=runtime_state,
            trigger=request.trigger,
            action=runtime_state.last_action,
            before=before,
            after=after,
            estimated_tokens=plan.after_tokens,
        )
        return list(result.projected_messages), result.artifact_ids, True

    async def _summarize(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        state: ContextState | None = None,
        base_artifacts: Sequence[Any] = (),
        pressure_before: ContextPressureSnapshot | None = None,
        estimated_tokens: int | None = None,
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """旧测试网关；手动 trigger 明确要求 full。"""
        if self._compactor is None:
            return messages, (), False
        request = await self._legacy_request(
            thread_id,
            messages,
            trigger="manual",
            estimated_tokens=estimated_tokens or _messages_tokens(messages),
        )
        if request is None:
            return messages, (), False
        request = replace(
            request,
            pressure_before=pressure_before
            or self._measure_pressure(messages, request.estimated_tokens or 0),
            runtime_state=(state.runtime_state if state is not None else None),
        )
        result = await self._compactor.compress(request)
        if not result.compressed:
            return messages, (), False
        return list(result.projected_messages), result.artifact_ids, True

    async def _overflow_recovery(
        self,
        thread_id: str,
        messages: list[BaseMessage],
    ) -> tuple[list[BaseMessage], tuple[str, ...], bool]:
        """旧测试网关；真实模型调用使用 ``_overflow_once``。"""
        if self._compactor is None:
            return messages, (), False
        request = await self._legacy_request(
            thread_id,
            messages,
            trigger="overflow",
            estimated_tokens=_messages_tokens(messages),
        )
        if request is None:
            return messages, (), False
        result = await self._compactor.compress(request)
        if not result.compressed:
            return messages, (), False
        self._publish_result(thread_id, result)
        return list(result.projected_messages), result.artifact_ids, True

    def _measure_pressure(
        self,
        messages: Sequence[BaseMessage],
        estimated_tokens: int,
        *,
        idle_duration_ms: int | None = None,
    ) -> ContextPressureSnapshot:
        """按当前投影测量可回收工具压力。"""
        count, tokens = _reclaimable_tool_pressure(
            messages,
            keep_turns=2,
            keep_recent=self._pressure_policy.config.keep_recent,
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
        """兼容旧测试的投影 token 估算。"""
        return max(
            0,
            before_tokens - _messages_tokens(before) + _messages_tokens(after),
        )

    def _publish_result(
        self,
        thread_id: str,
        result: CompressionResult,
        *,
        action: str | None = None,
    ) -> ContextUpdate:
        """将 typed outcome 翻译为稳定事件，不改变 service 的持久化结果。"""
        return self._publish(
            thread_id,
            action or result.action,
            result.estimated_tokens,
            result.artifact_ids,
            miss_reason=result.reason,
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
        """缓冲状态给 server；网关未提供缓存 token 时显式标记 unknown。"""
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
    """优先从 RunContext 获取 thread ID，并拒绝跨 thread 配置。"""
    context_thread_id = thread_id_for_runtime(request.runtime)
    if context_thread_id is not None:
        return context_thread_id
    config = getattr(request.runtime, "config", {})
    configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    if not isinstance(configurable, Mapping):
        configurable = {}
    execution_info = getattr(request.runtime, "execution_info", None)
    execution_thread_id = getattr(execution_info, "thread_id", None)
    if isinstance(execution_thread_id, str) and execution_thread_id:
        return execution_thread_id
    return (
        str(configurable.get("thread_id") or "ephemeral")
        if isinstance(configurable, Mapping)
        else "ephemeral"
    )


def _run_context(runtime: object) -> Any:
    """返回当前 RunContext；无头兼容调用不猜测运行态。"""
    return getattr(runtime, "context", None)


def _run_context_snapshot(runtime: object) -> Any:
    """返回本次 run 的 snapshot，不读取旧摘要或旧 checkpoint。"""
    context = _run_context(runtime)
    return getattr(context, "context_snapshot", None)


def _ordered_request_tools(tools: list[object]) -> list[object]:
    """按规范化 schema 稳定排序工具对象，不改变工具内容。"""
    def key(tool: object) -> tuple[str, str, str]:
        schema = normalized_tool_schemas([tool])[0]
        return (
            str(schema["name"]),
            str(schema["description"]),
            canonical_json(schema["parameters"]),
        )

    return sorted(tools, key=key)


def _estimate_request_tokens(request: ModelRequest, tools: list[object]) -> int:
    """估算 system、messages 和工具 schema 的输入预算。"""
    system = _message_content(request.system_message) if request.system_message is not None else ""
    return (
        estimate_tokens(system)
        + _messages_tokens(request.messages)
        + estimate_tokens(canonical_json(normalized_tool_schemas(tools)))
        + (len(request.messages) + len(tools)) * 8
    )


def _messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """计算消息正文、工具调用元数据和固定结构开销。"""
    from harness_agent.threads.context_compaction import _render_message

    return sum(estimate_tokens(_render_message(message)) + 8 for message in messages)


def _message_content(message: object) -> str:
    """将模型消息的文本内容转换为稳定字符串。"""
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


def _reclaimable_tool_pressure(
    messages: Sequence[BaseMessage], *, keep_turns: int, keep_recent: int
) -> tuple[int, int]:
    """复用统一服务的候选判定，避免 middleware 另有工具归档规则。"""
    from harness_agent.threads.context_compaction import _reclaimable_tool_pressure as measure

    return measure(messages, keep_turns=keep_turns, keep_recent=keep_recent)


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


def _legacy_auto_action(
    result: CompressionResult, pressure: ContextPressureSnapshot
) -> str:
    """把旧测试需要的动作名映射到新的 typed action。"""
    if result.action == "auto_micro":
        return "idle_micro" if pressure.idle_duration_ms is not None else "pressure_micro"
    if result.action == "auto_full":
        after_ratio = pressure.occupancy_ratio
        if result.checkpoint is not None:
            value = result.checkpoint.pressure_before.get("occupancy_ratio")
            if isinstance(value, (int, float)):
                after_ratio = float(value)
        return (
            "forced_summary"
            if after_ratio >= 0.90
            else "summary"
        )
    return result.action


def _legacy_manual_action(result: CompressionResult) -> str:
    """兼容旧 context.compact 测试的动作名；正式 JSON-RPC 保持 typed 名。"""
    if result.compressed:
        return "manual_summary"
    if result.outcome == "failed":
        return "manual_compaction_failed"
    return "manual_compaction_skipped"
