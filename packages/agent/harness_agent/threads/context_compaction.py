"""统一的自动、手动和 overflow 完整压缩领域服务。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from harness_agent.threads.context_pressure import (
    ContextPressurePolicy,
    ContextPressureSnapshot,
    ModelCallType,
)
from harness_agent.threads.context_projection import (
    CompressionCheckpoint,
    CompressionCheckpointDraft,
    ContextProjectionError,
    ModelProjection,
    artifact_references,
    encode_projected_messages,
    validate_atomic_message_groups,
)
from harness_agent.threads.prompting import HISTORY_REWRITE_VERSION, canonical_json, estimate_tokens, input_cap_tokens
from harness_agent.threads.runtime_state import (
    RuntimeExecutionPolicy,
    RuntimeStateRehydrator,
    RuntimeStateSnapshot,
)
from harness_agent.threads.thread_persistence import (
    CommitContextRewrite,
    ContextArtifactDraft,
    ContextState,
    ContextSummaryDraft,
    ThreadPersistence,
    ThreadPersistenceError,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from harness_agent.threads.context_lifecycle import RunContextSnapshot
    from harness_agent.runtime.run_context import RunContext


CompressionTrigger = Literal["auto", "manual", "overflow"]
CompressionOutcome = Literal["compressed", "skipped", "failed"]

TOOL_RESULT_DEHYDRATE_TOKENS = 2_048
TOOL_RESULT_PREVIEW_CHARS = 200
MIN_SAVINGS_RATIO = 0.20
SUMMARY_INPUT_SAFETY_MARGIN_TOKENS = 256
SUMMARY_REWRITE_VERSION = HISTORY_REWRITE_VERSION
_ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"/\.harness/history/([A-Za-z0-9_-]+)\.md"
)
_SUMMARY_HEADINGS = (
    "目标",
    "已确认事实",
    "决策",
    "改动",
    "测试",
    "未决项",
    "归档",
)
_FORBIDDEN_SUMMARY_MARKERS = (
    "AGENTS.md",
    "AGENTS",
    "Skill",
    "skill catalog",
    "工具 schema",
    "tool schema",
    "审批策略",
    "approval policy",
    "系统规则",
    "system rule",
)
_SAFE_DIAGNOSTIC_REASON_RE = re.compile(
    r"^[A-Z][A-Z0-9_]*(?::[A-Z][A-Z0-9_]*)*$"
)

_SUMMARY_PROMPT = """你是编码 Agent 的无工具摘要模型。只根据输入记录输出以下七个章节，不能执行工具、提出新计划或补充输入之外的事实：
## 目标
## 已确认事实
## 决策
## 改动
## 测试
## 未决项
## 归档

摘要只记录工作目标、已确认事实、决定、改动、测试、未决事项和输入中已有的 Artifact 引用。不要写 AGENTS、Skill、工具 schema、审批策略、系统规则、凭据或提示词。"""


@dataclass(frozen=True, slots=True)
class CompressionRequest:
    """压缩服务的唯一输入；调用方不接触表级事务或消息重写细节。"""

    thread_id: str
    trigger: CompressionTrigger
    projection: ModelProjection
    run_context_snapshot: "RunContextSnapshot | None" = None
    run_context: "RunContext | None" = None
    runtime_state: RuntimeStateSnapshot | None = None
    current_execution_policy: RuntimeExecutionPolicy | None = None
    pressure_before: ContextPressureSnapshot | None = None
    estimated_tokens: int | None = None
    call_type: ModelCallType = "unclassified"


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """统一结果；``skipped`` 和 ``failed`` 都不会返回可用的新投影。"""

    outcome: CompressionOutcome
    trigger: CompressionTrigger
    action: str
    projected_messages: tuple[BaseMessage, ...]
    estimated_tokens: int
    input_cap_tokens: int
    artifact_ids: tuple[str, ...] = ()
    checkpoint: CompressionCheckpoint | None = None
    state: ContextState | None = None
    reason: str | None = None

    @property
    def compressed(self) -> bool:
        """返回是否产生了可供调用方刷新的新投影。"""
        return self.outcome == "compressed"


@dataclass(frozen=True, slots=True)
class _MicroPlan:
    """只存在于当前压缩调用内的微压缩草稿。"""

    messages: tuple[BaseMessage, ...]
    artifacts: tuple[ContextArtifactDraft, ...]
    before_tokens: int
    after_tokens: int


class ContextCompactor:
    """执行确定性 micro、结构化 full、校验和单事务提交。"""

    def __init__(
        self,
        model: "BaseChatModel",
        *,
        context_window_tokens: int,
        thread_persistence: ThreadPersistence,
        pressure_policy: ContextPressurePolicy | None = None,
        runtime_state_provider: Callable[
            [str, "RunContext | None", Sequence[BaseMessage]], Awaitable[RuntimeStateSnapshot]
        ]
        | None = None,
    ) -> None:
        """绑定当前 Profile 的摘要模型与共享 ThreadPersistence。"""
        self._model = model
        self._window = context_window_tokens
        self._input_cap = input_cap_tokens(context_window_tokens)
        self._persistence = thread_persistence
        self._pressure_policy = pressure_policy or ContextPressurePolicy()
        self._runtime_state_provider = runtime_state_provider

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        """按 trigger 统一执行完整压缩闭环，并在失败时保持旧投影。"""
        try:
            self._validate_request(request)
        except Exception as exc:
            return CompressionResult(
                outcome="failed",
                trigger=request.trigger,
                action=f"{request.trigger}_failed",
                projected_messages=request.projection.messages,
                estimated_tokens=request.estimated_tokens or 0,
                input_cap_tokens=self._input_cap,
                reason=_safe_diagnostic_reason(exc),
            )
        messages = list(request.projection.messages)
        before_tokens = request.estimated_tokens or _messages_tokens(messages)
        previous_state = await self._persistence.load_context_state(request.thread_id)

        # 输出预留可能耗尽整个极小窗口；这种配置不能进入压力测量（其
        # ``ContextPressureSnapshot`` 有效 cap 必须为正），也不能被当成
        # 自动压缩失败或打开熔断。
        if self._input_cap <= 0:
            return self._skipped(
                request,
                before_tokens,
                f"{request.trigger}_skipped",
                "input_cap_exhausted",
                previous_state,
            )

        pressure_before = request.pressure_before or self._pressure(
            messages, before_tokens
        )

        if request.trigger == "auto" and previous_state.circuit_open:
            return self._skipped(
                request,
                before_tokens,
                "auto_skipped_circuit_open",
                "three automatic compression failures",
                previous_state,
            )

        decision = self._pressure_policy.decide(
            pressure_before,
            call_type=request.call_type,
            manual=request.trigger == "manual",
            overflow=request.trigger == "overflow",
        )
        if request.trigger == "auto" and decision.action not in {"micro", "full"}:
            return self._skipped(
                request,
                before_tokens,
                "auto_skipped",
                decision.reason,
                previous_state,
            )

        try:
            micro_plan = (
                None
                if request.trigger == "manual"
                else self._plan_micro(
                    request.thread_id,
                    messages,
                    keep_turns=self._keep_turns(request.trigger, pressure_before),
                    keep_recent=decision.keep_recent,
                    before_tokens=before_tokens,
                    trigger=request.trigger,
                )
            )
            full_messages = messages
            full_before = pressure_before

            # 手动命令的语义是明确的 full；它不能因为当前有可回收工具结果
            # 就提前返回 micro。自动/overflow 才允许在同一轮先规划 micro。
            if micro_plan is not None:
                pressure_after = self._pressure(
                    list(micro_plan.messages), micro_plan.after_tokens
                )
                full_messages = list(micro_plan.messages)
                full_before = pressure_after
                after_decision = self._pressure_policy.decide(
                    pressure_after,
                    call_type=request.call_type,
                )
                should_stop_after_micro = (
                    request.trigger == "auto"
                    and after_decision.action != "full"
                ) or (
                    request.trigger == "overflow"
                    and pressure_after.occupancy_ratio < self._pressure_policy.config.full_ratio
                )
                if should_stop_after_micro:
                    state = await self._success_state(
                        request,
                        "auto_micro"
                        if request.trigger == "auto"
                        else "overflow_micro",
                        micro_plan.artifacts,
                        artifact_ids=artifact_references(micro_plan.messages),
                        messages=micro_plan.messages,
                    )
                    return await self._commit_projection(
                        request,
                        messages=tuple(micro_plan.messages),
                        artifacts=micro_plan.artifacts,
                        summary=None,
                        state=state,
                        trigger=(
                            "idle"
                            if decision.reason == "idle"
                            else request.trigger
                        ),
                        action=state.last_action,
                        before=pressure_before,
                        after=pressure_after,
                        estimated_tokens=micro_plan.after_tokens,
                    )
            elif request.trigger == "auto" and decision.action == "micro":
                return self._skipped(
                    request,
                    before_tokens,
                    "auto_skipped",
                    "micro_noop",
                    previous_state,
                )

            keep_turns = self._full_keep_turns(request, full_before)
            return await self._full_compress(
                request,
                messages=full_messages,
                before=full_before,
                keep_turns=keep_turns,
                micro_artifacts=(micro_plan.artifacts if micro_plan else ()),
                previous_state=previous_state,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                return await self._failed(
                    request,
                    before_tokens,
                    previous_state,
                    _safe_diagnostic_reason(exc),
                )
            except Exception as state_exc:
                # 存储故障不能覆盖原始投影，也不能把部分事务误报成成功。
                return CompressionResult(
                    outcome="failed",
                    trigger=request.trigger,
                    action=f"{request.trigger}_failed",
                    projected_messages=request.projection.messages,
                    estimated_tokens=before_tokens,
                    input_cap_tokens=self._input_cap,
                    state=previous_state,
                    reason=(
                        f"{_safe_diagnostic_reason(exc)};"
                        f"failure_state:{_safe_diagnostic_reason(state_exc)}"
                    ),
                )

    async def _full_compress(
        self,
        request: CompressionRequest,
        *,
        messages: list[BaseMessage],
        before: ContextPressureSnapshot,
        keep_turns: int,
        micro_artifacts: Sequence[ContextArtifactDraft],
        previous_state: ContextState,
    ) -> CompressionResult:
        cutoff = _cutoff_for_recent_turns(messages, keep_turns)
        if cutoff <= 0:
            return self._skipped(
                request,
                before.projected_input_tokens,
                f"{request.trigger}_skipped",
                "short_history",
                previous_state,
            )
        old = messages[:cutoff]
        recent = messages[cutoff:]
        summary_input_cap = self._summary_input_cap()
        if summary_input_cap <= 0:
            return self._skipped(
                request,
                before.projected_input_tokens,
                f"{request.trigger}_skipped",
                "summary_input_cap_exhausted",
                previous_state,
            )
        summary_input = _select_complete_summary_input(
            old,
            summary_input_cap,
            measure=self._summary_payload_tokens,
        )
        if summary_input is None:
            return self._skipped(
                request,
                before.projected_input_tokens,
                f"{request.trigger}_skipped",
                "summary_input_no_complete_group",
                previous_state,
            )

        if request.trigger == "manual":
            _, minimum_projection = _build_full_projection(
                request,
                cutoff=cutoff,
                old=old,
                recent=recent,
                summary="",
            )
            minimum_after_tokens = _estimate_rewritten_tokens(
                before.projected_input_tokens,
                messages,
                minimum_projection,
            )
            if not _reduces_context(
                before.projected_input_tokens, minimum_after_tokens
            ):
                return self._skipped(
                    request,
                    before.projected_input_tokens,
                    "manual_skipped",
                    "manual_history_too_small",
                    previous_state,
                )

        response = await self._model.ainvoke(
            [
                SystemMessage(content=_SUMMARY_PROMPT),
                HumanMessage(content=_render_messages(summary_input)),
            ]
        )
        summary = _summary_text(response)
        validation_reason = _validate_summary_response(response, summary, self._summary_cap())
        if validation_reason is not None:
            return await self._failed(
                request,
                before.projected_input_tokens,
                previous_state,
                validation_reason,
            )

        known_artifacts = set(artifact_references(messages))
        summary_refs = set(_ARTIFACT_REFERENCE_PATTERN.findall(summary))
        if not summary_refs.issubset(known_artifacts):
            return await self._failed(
                request,
                before.projected_input_tokens,
                previous_state,
                "summary_source_artifact_boundary_invalid",
            )

        history_id, prospective = _build_full_projection(
            request,
            cutoff=cutoff,
            old=old,
            recent=recent,
            summary=summary,
        )
        after_tokens = _estimate_rewritten_tokens(
            before.projected_input_tokens,
            messages,
            prospective,
        )
        savings_reason = None
        if request.trigger == "manual":
            if not _reduces_context(before.projected_input_tokens, after_tokens):
                savings_reason = "manual_no_savings"
        elif not _saves_enough(before.projected_input_tokens, after_tokens):
            savings_reason = "savings_below_20_percent"
        if savings_reason is not None:
            return self._skipped(
                request,
                before.projected_input_tokens,
                f"{request.trigger}_skipped",
                savings_reason,
                previous_state,
            )

        history_artifact = ContextArtifactDraft(
            kind="history",
            content=_render_messages(old),
            source_start=0,
            source_end=max(0, cutoff - 1),
            artifact_id=history_id,
        )
        all_artifacts = tuple(micro_artifacts) + (history_artifact,)
        artifact_ids = tuple(
            dict.fromkeys(
                (*artifact_references(prospective), *(artifact.artifact_id or "" for artifact in all_artifacts))
            )
        )
        if "" in artifact_ids:
            return await self._failed(
                request,
                before.projected_input_tokens,
                previous_state,
                "compression_artifact_id_invalid",
            )
        state = await self._success_state(
            request,
            f"{request.trigger}_full",
            all_artifacts,
            artifact_ids=artifact_ids,
            messages=prospective,
        )
        summary_draft = ContextSummaryDraft(
            rewrite_version=SUMMARY_REWRITE_VERSION,
            content=summary,
            source_start=0,
            source_end=max(0, cutoff - 1),
            artifact_indexes=tuple(range(len(all_artifacts))),
        )
        return await self._commit_projection(
            request,
            messages=prospective,
            artifacts=all_artifacts,
            summary=summary_draft,
            state=state,
            trigger=request.trigger,
            action=state.last_action,
            before=before,
            after=self._pressure(list(prospective), after_tokens),
            estimated_tokens=after_tokens,
        )

    async def _commit_projection(
        self,
        request: CompressionRequest,
        *,
        messages: tuple[BaseMessage, ...],
        artifacts: Sequence[ContextArtifactDraft],
        summary: ContextSummaryDraft | None,
        state: ContextState,
        trigger: str,
        action: str,
        before: ContextPressureSnapshot,
        after: ContextPressureSnapshot,
        estimated_tokens: int,
    ) -> CompressionResult:
        """只通过 ThreadPersistence 提交完整领域对象，禁止调用方拼事务。"""
        artifact_ids = tuple(
            dict.fromkeys(
                (*artifact_references(messages), *(artifact.artifact_id or "" for artifact in artifacts))
            )
        )
        checkpoint_id = _stable_checkpoint_id(
            request,
            messages,
            trigger,
            summary.content if summary is not None else "",
        )
        draft = CompressionCheckpointDraft(
            checkpoint_id=checkpoint_id,
            mode="micro" if summary is None else "full",
            rewrite_version=SUMMARY_REWRITE_VERSION,
            projected_messages=messages,
            artifact_ids=artifact_ids,
            trigger=trigger,
            pressure_before=before.record(),
            pressure_after=after.record(),
            source_record_sequence=request.projection.source_record_sequence,
        )
        committed = await self._persistence.commit_context(
            CommitContextRewrite(
                thread_id=request.thread_id,
                artifacts=tuple(artifacts),
                summary=summary,
                state=state,
                checkpoint=draft,
            )
        )
        checkpoint = committed.checkpoint
        if checkpoint is None:
            raise ThreadPersistenceError("COMPRESSION_CHECKPOINT_MISSING")
        new_artifact_ids = tuple(
            artifact.artifact_id for artifact in committed.artifacts
        )
        return CompressionResult(
            outcome="compressed",
            trigger=request.trigger,
            action=action,
            projected_messages=messages,
            estimated_tokens=estimated_tokens,
            input_cap_tokens=self._input_cap,
            artifact_ids=new_artifact_ids,
            checkpoint=checkpoint,
            state=committed.state,
        )

    async def _success_state(
        self,
        request: CompressionRequest,
        action: str,
        artifacts: Sequence[ContextArtifactDraft],
        *,
        artifact_ids: Sequence[str] = (),
        messages: Sequence[BaseMessage] | None = None,
    ) -> ContextState:
        """成功后重置当前 Thread 的自动熔断，并保存真实结构化运行态。"""
        current = request.runtime_state
        if current is None:
            if self._runtime_state_provider is not None:
                current = await self._runtime_state_provider(
                    request.thread_id,
                    request.run_context,
                    messages or request.projection.messages,
                )
            else:
                current = RuntimeStateRehydrator.capture(
                    None,
                    request.run_context,
                    messages or request.projection.messages,
                    artifact_ids=artifact_ids,
                    context_snapshot=request.run_context_snapshot,
                    current_execution_policy=request.current_execution_policy,
                )
        if request.current_execution_policy is not None:
            current = current.with_execution_policy(request.current_execution_policy)
        snapshot = request.run_context_snapshot
        if snapshot is not None:
            current = RuntimeStateSnapshot(
                todos=current.todos,
                execution_mode=current.execution_mode,
                approval_mode=current.approval_mode,
                context_snapshot_id=snapshot.snapshot_id,
                capability_fingerprint=(
                    snapshot.system_fingerprint or current.capability_fingerprint
                ),
                artifact_ids=current.artifact_ids,
                recent_tool_group=current.recent_tool_group,
            )
        created_ids = tuple(
            artifact.artifact_id
            for artifact in artifacts
            if artifact.artifact_id is not None
        )
        return ContextState(
            failures=0,
            circuit_open=False,
            last_action=action,
            runtime_state=current.with_artifacts((*created_ids, *artifact_ids)),
        )

    async def _failed(
        self,
        request: CompressionRequest,
        estimated_tokens: int,
        previous_state: ContextState,
        reason: str,
    ) -> CompressionResult:
        """失败只记录自动熔断计数，不创建 Artifact/Summary/Checkpoint。"""
        state = previous_state
        if request.trigger in {"auto", "overflow"}:
            failures = previous_state.failures + 1
            state = ContextState(
                failures=failures,
                circuit_open=failures >= 3,
                last_action=f"{request.trigger}_failed",
                runtime_state=previous_state.runtime_state,
            )
            await self._persistence.commit_context(
                CommitContextRewrite(thread_id=request.thread_id, state=state)
            )
        return CompressionResult(
            outcome="failed",
            trigger=request.trigger,
            action=f"{request.trigger}_failed",
            projected_messages=request.projection.messages,
            estimated_tokens=estimated_tokens,
            input_cap_tokens=self._input_cap,
            state=state,
            reason=reason,
        )

    def _skipped(
        self,
        request: CompressionRequest,
        estimated_tokens: int,
        action: str,
        reason: str,
        state: ContextState,
    ) -> CompressionResult:
        """返回可诊断 skipped，调用方不得把它当作成功重写。"""
        return CompressionResult(
            outcome="skipped",
            trigger=request.trigger,
            action=action,
            projected_messages=request.projection.messages,
            estimated_tokens=estimated_tokens,
            input_cap_tokens=self._input_cap,
            state=state,
            reason=reason,
        )

    def _pressure(
        self, messages: Sequence[BaseMessage], estimated_tokens: int
    ) -> ContextPressureSnapshot:
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
        )

    def _plan_micro(
        self,
        thread_id: str,
        messages: list[BaseMessage],
        *,
        keep_turns: int,
        keep_recent: int,
        before_tokens: int,
        trigger: CompressionTrigger,
    ) -> _MicroPlan | None:
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
            artifact_id = _stable_artifact_id(
                "tool", thread_id, index, trigger, _render_message(message), content
            )
            preview = _tool_preview(content, artifact_id, tool_name=tool_name)
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
        after_tokens = _estimate_rewritten_tokens(
            before_tokens, messages, replacements
        )
        if not drafts or not _saves_enough(before_tokens, after_tokens):
            return None
        return _MicroPlan(
            messages=tuple(replacements),
            artifacts=tuple(drafts),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    def _validate_request(self, request: CompressionRequest) -> None:
        """验证来源边界和消息原子性，防止压缩服务接收旧缓存。"""
        if not request.thread_id:
            raise ContextProjectionError("COMPRESSION_THREAD_ID_INVALID")
        validate_atomic_message_groups(request.projection.messages)
        snapshot = request.run_context_snapshot
        if request.run_context is not None:
            context_snapshot = getattr(request.run_context, "context_snapshot", None)
            if context_snapshot is not None:
                if snapshot is not None and snapshot.snapshot_id != context_snapshot.snapshot_id:
                    raise ContextProjectionError("COMPRESSION_CONTEXT_SNAPSHOT_MISMATCH")
                snapshot = context_snapshot
        if snapshot is not None and snapshot.thread_id != request.thread_id:
            raise ContextProjectionError("COMPRESSION_CONTEXT_SNAPSHOT_THREAD_MISMATCH")
        if request.current_execution_policy is not None and not isinstance(
            request.current_execution_policy, RuntimeExecutionPolicy
        ):
            raise ContextProjectionError("COMPRESSION_EXECUTION_POLICY_INVALID")

    def _keep_turns(
        self, trigger: CompressionTrigger, pressure: ContextPressureSnapshot
    ) -> int:
        if trigger == "manual":
            return 2
        return 1 if pressure.occupancy_ratio >= self._pressure_policy.config.hard_ratio else 2

    def _full_keep_turns(
        self,
        request: CompressionRequest,
        pressure: ContextPressureSnapshot,
    ) -> int:
        if request.trigger == "manual":
            return 2
        return 1 if pressure.occupancy_ratio >= self._pressure_policy.config.hard_ratio else 2

    def _summary_cap(self) -> int:
        """限制摘要输出，并为模型输出保留稳定上限。"""
        return max(2_048, min(12_000, int(self._window * 0.06 + 0.999)))

    def _summary_input_cap(self) -> int:
        """从真实模型输入 cap 扣除 prompt、消息 framing 和安全余量。"""
        # ``self._input_cap`` 已经扣除了 output reserve；这里不能再次扣窗口
        # 或输出预算。只为当前无工具摘要 side query 扣除确定的输入开销。
        prompt_tokens = estimate_tokens(
            _render_message(SystemMessage(content=_SUMMARY_PROMPT))
        )
        human_framing_tokens = estimate_tokens(
            _render_message(HumanMessage(content=""))
        )
        return max(
            0,
            self._input_cap
            - prompt_tokens
            - human_framing_tokens
            - SUMMARY_INPUT_SAFETY_MARGIN_TOKENS,
        )

    @staticmethod
    def _summary_payload_tokens(messages: Sequence[BaseMessage]) -> int:
        """估算嵌入 outer HumanMessage 后的完整摘要正文负载。"""
        body = _render_messages(messages)
        with_body = estimate_tokens(_render_message(HumanMessage(content=body)))
        empty = estimate_tokens(_render_message(HumanMessage(content="")))
        return max(0, with_body - empty)


def _validate_summary_response(
    response: object, summary: str, output_cap: int
) -> str | None:
    """统一校验空结果、工具调用、结构、截断和不可信来源。"""
    if getattr(response, "tool_calls", None):
        return "summary_model_tools_forbidden"
    metadata = getattr(response, "response_metadata", {})
    if isinstance(metadata, Mapping):
        finish_reason = metadata.get("finish_reason") or metadata.get("stop_reason")
        if finish_reason in {"length", "max_tokens", "MAX_TOKENS"}:
            return "summary_output_truncated"
    if not summary:
        return "summary_empty"
    if estimate_tokens(summary) >= output_cap:
        return "summary_output_limit"
    headings = tuple(
        line[3:].strip()
        for line in summary.splitlines()
        if line.startswith("## ")
    )
    if headings != _SUMMARY_HEADINGS:
        return "summary_structure_invalid"
    sections = _summary_sections(summary)
    if any(not sections.get(heading, "").strip() for heading in _SUMMARY_HEADINGS):
        return "summary_structure_empty"
    lowered = summary.lower()
    if any(marker.lower() in lowered for marker in _FORBIDDEN_SUMMARY_MARKERS):
        return "summary_forbidden_runtime_content"
    return None


def _summary_sections(summary: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {heading: [] for heading in _SUMMARY_HEADINGS}
    current: str | None = None
    for line in summary.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            continue
        if current in sections:
            sections[current].append(line)
    return {heading: "\n".join(lines) for heading, lines in sections.items()}


def _summary_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        ).strip()
    return ""


def _select_complete_summary_input(
    messages: Sequence[BaseMessage],
    token_cap: int,
    *,
    measure: Callable[[Sequence[BaseMessage]], int] | None = None,
) -> tuple[BaseMessage, ...] | None:
    """从完整 user/tool 原子组取最近的连续后缀，不截断并行工具组。"""
    groups = _atomic_groups(messages)
    if not groups:
        return None
    selected: list[tuple[BaseMessage, ...]] = []
    total = 0
    for group in reversed(groups):
        if measure is not None:
            candidate_groups = [group, *selected]
            candidate = tuple(
                message for candidate_group in candidate_groups for message in candidate_group
            )
            if measure(candidate) > token_cap:
                if not selected:
                    return None
                break
        else:
            group_tokens = _messages_tokens(group)
            if group_tokens > token_cap:
                if not selected:
                    return None
                break
            if total + group_tokens > token_cap:
                break
        selected.insert(0, group)
        if measure is None:
            total += group_tokens
    if not selected:
        return None
    return tuple(message for group in selected for message in group)


def _atomic_groups(messages: Sequence[BaseMessage]) -> list[tuple[BaseMessage, ...]]:
    groups: list[tuple[BaseMessage, ...]] = []
    current: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and current:
            groups.append(tuple(current))
            current = []
        current.append(message)
    if current:
        groups.append(tuple(current))
    return groups


def _dehydratable_tool_candidates(
    messages: Sequence[BaseMessage], cutoff: int
) -> list[tuple[int, ToolMessage, str]]:
    """选取旧、完成、可恢复且未被 Artifact 占位的结果。"""
    active_calls: dict[str, str] = {}
    candidates: list[tuple[int, ToolMessage, str]] = []
    for index, message in enumerate(messages[:cutoff]):
        if isinstance(message, AIMessage):
            if active_calls:
                active_calls.clear()
            for call in message.tool_calls or ():
                if isinstance(call, Mapping):
                    call_id = call.get("id")
                    name = call.get("name")
                    if isinstance(call_id, str) and call_id:
                        active_calls[call_id] = (
                            name if isinstance(name, str) and name else "tool"
                        )
            continue
        if isinstance(message, HumanMessage) or not isinstance(message, ToolMessage):
            active_calls.clear()
            continue
        name = active_calls.pop(message.tool_call_id, None)
        if name is None:
            continue
        content = _message_content(message)
        if artifact_references((message,)) or estimate_tokens(content) <= TOOL_RESULT_DEHYDRATE_TOKENS:
            continue
        preview = _tool_preview(content, "tool-preview", tool_name=message.name or name)
        if _saves_enough(estimate_tokens(content), estimate_tokens(preview)):
            candidates.append((index, message, message.name or name))
    return candidates


def _reclaimable_tool_pressure(
    messages: Sequence[BaseMessage], *, keep_turns: int, keep_recent: int
) -> tuple[int, int]:
    cutoff = _cutoff_for_recent_turns(list(messages), keep_turns)
    candidates = _dehydratable_tool_candidates(messages, cutoff)
    reclaimable = candidates[:-max(1, keep_recent)]
    return len(reclaimable), sum(
        estimate_tokens(_message_content(message))
        for _index, message, _name in reclaimable
    )


def _cutoff_for_recent_turns(messages: Sequence[BaseMessage], keep_turns: int) -> int:
    starts = [
        index for index, message in enumerate(messages) if isinstance(message, HumanMessage)
    ]
    return starts[-keep_turns] if len(starts) > keep_turns else 0


def _safe_diagnostic_reason(error: BaseException) -> str:
    """只把稳定错误码带到 context.updated，绝不回传异常原文。"""
    detail = str(error)
    if _SAFE_DIAGNOSTIC_REASON_RE.fullmatch(detail):
        return f"{type(error).__name__}:{detail}"
    return type(error).__name__


def _message_content(message: object) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return ""


def _render_message(message: BaseMessage) -> str:
    payload: dict[str, object] = {
        "type": message.type,
        "content": _message_content(message),
    }
    if isinstance(message, AIMessage) and message.tool_calls:
        payload["tool_calls"] = message.tool_calls
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
        payload["name"] = message.name
        payload["status"] = getattr(message, "status", "success")
    return canonical_json(payload)


def _render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n".join(_render_message(message) for message in messages)


def _messages_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(estimate_tokens(_render_message(message)) + 8 for message in messages)


def _estimate_rewritten_tokens(
    before_tokens: int,
    before: Sequence[BaseMessage],
    after: Sequence[BaseMessage],
) -> int:
    return max(0, before_tokens - _messages_tokens(before) + _messages_tokens(after))


def _tool_preview(content: str, artifact_id: str, *, tool_name: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (
        f"[tool={tool_name}]\n"
        f"Summary preview:\n{content[:TOOL_RESULT_PREVIEW_CHARS]}\n"
        f"...\n{content[-TOOL_RESULT_PREVIEW_CHARS:]}\n"
        f"sha256={digest}\n"
        f"Artifact: /.harness/history/{artifact_id}.md"
    )


def _stable_artifact_id(
    kind: str, thread_id: str, *parts: object
) -> str:
    material = canonical_json(
        {"kind": kind, "thread_id": thread_id, "parts": list(parts)}
    )
    return f"{kind}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _build_full_projection(
    request: CompressionRequest,
    *,
    cutoff: int,
    old: Sequence[BaseMessage],
    recent: Sequence[BaseMessage],
    summary: str,
) -> tuple[str, tuple[BaseMessage, ...]]:
    """构造 full 候选投影；手动预检与最终提交必须使用同一包装开销。"""
    history_id = _stable_artifact_id(
        "history",
        request.thread_id,
        request.projection.source_record_sequence,
        cutoff,
        _render_messages(old),
        summary,
    )
    return history_id, (
        HumanMessage(
            content=(
                "<harness_context_summary>\n"
                f"{summary}\n\n"
                f"Archived original: /.harness/history/{history_id}.md\n"
                "</harness_context_summary>"
            )
        ),
        *recent,
    )


def _stable_checkpoint_id(
    request: CompressionRequest,
    messages: Sequence[BaseMessage],
    trigger: str,
    summary: str,
) -> str:
    encoded = encode_projected_messages(messages)
    return "projection-" + hashlib.sha256(
        canonical_json(
            {
                "thread_id": request.thread_id,
                "source_record_sequence": request.projection.source_record_sequence,
                "trigger": trigger,
                "summary": summary,
                "messages": encoded,
            }
        ).encode("utf-8")
    ).hexdigest()[:32]


def _saves_enough(before_tokens: int, after_tokens: int) -> bool:
    return (
        _reduces_context(before_tokens, after_tokens)
        and (before_tokens - after_tokens) / before_tokens >= MIN_SAVINGS_RATIO
    )


def _reduces_context(before_tokens: int, after_tokens: int) -> bool:
    """手动压缩只要求投影严格变小，不复用自动路径的收益率门槛。"""
    return before_tokens > 0 and after_tokens < before_tokens
