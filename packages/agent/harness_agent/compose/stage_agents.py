"""Compose StageAgentPort：fresh Managed stage execution seam。

StageAgentPort 由 Harness 内置 stage Agent adapter 实现，底层复用
AgentDelegator / AgentEnginePool：stage Agent 使用与主 Agent 相同的
可信 spec/Policy（Compose 不提权），但每个 stage 都拿到 fresh
RunContext 与独立 checkpoint namespace，不继承前一个 stage 的对话。

执行流统一走 runtime.execution_stream 的 capture_only：安全事件经
StageObserver 投影，artifact 正文只进入 StageResult 供 Runtime 校验。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import HumanMessage

from harness_agent.runtime.agent_delegation import (
    AgentDelegator,
    DelegationTarget,
    child_execution_ref,
    managed_engine_runner,
)
from harness_agent.runtime.execution_binding import ExecutionMode, ExecutionRef
from harness_agent.runtime.execution_stream import (
    CONTENT_DELTA,
    ExecutionStreamError,
    ExecutionStreamRequest,
    ExecutionStreamResult,
    RUN_PROGRESS,
    StreamInteractionRequest,
    StreamSession,
    TOOL_COMPLETED,
    TOOL_STARTED,
    execute as execute_stream,
)
from harness_agent.runtime.run_context import RunCancellationToken, RunContext

if TYPE_CHECKING:
    from harness_agent.runtime.agent_engine import AgentEnginePool
    from harness_agent.runtime.agent_execution import AgentExecutionRegistry

STAGE_AGENT_TIMEOUT_SECONDS = 600.0

# Reviewer 使用只读能力视图；作者 execution 不得兼任 Reviewer。
REVIEWER_STAGES = frozenset({"requirement-reviewer", "code-reviewer"})
PLANNING_STAGES = frozenset({"understand", "plan"})


class StageRequest:
    """一次 stage Agent 调用的领域输入。"""

    def __init__(
        self,
        *,
        stage: str,
        task: str,
        parent_ref: ExecutionRef,
        profile_key: str,
        cancellation_token: RunCancellationToken,
        timeout_seconds: float = STAGE_AGENT_TIMEOUT_SECONDS,
        compose_scope: Mapping[str, object] | None = None,
    ) -> None:
        """保存 stage 身份、有界任务文本、父 execution 与可选 activity scope。"""
        if not stage or not task.strip():
            raise ValueError("STAGE_REQUEST_INVALID")
        self.stage = stage
        self.task = task
        self.parent_ref = parent_ref
        self.profile_key = profile_key
        self.cancellation_token = cancellation_token
        self.timeout_seconds = timeout_seconds
        self.invocation_id = uuid.uuid4().hex
        self.compose_scope = dict(compose_scope) if compose_scope is not None else None


class StageResult:
    """stage Agent 的结构化结果；output 是待校验的 artifact dict。"""

    def __init__(
        self,
        *,
        execution_id: str,
        agent_id: str,
        status: str,
        output: Mapping[str, Any],
    ) -> None:
        """保存 execution 身份与 artifact payload。"""
        self.execution_id = execution_id
        self.agent_id = agent_id
        self.status = status
        self.output = dict(output)


class StageObserver(Protocol):
    """Compose-owned stage 观察者：不导入 Host 类型。

    Workflow 实现此 interface，把 signal / Interaction 映射到 RunLifecyclePort，
    并附带 child provenance 与 compose_scope。
    """

    def bind_execution(
        self,
        *,
        execution_id: str,
        parent_execution_id: str | None,
        agent_id: str,
    ) -> None:
        """在 child execution 创建后绑定真实 provenance。"""

    def emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        """发送带 scope 的非终态领域事件。"""

    def record_tool_terminal(self, *, tool_name: str, status: str) -> None:
        """持久化一个有界 Tool 终态（名与状态，无参数/结果正文）。"""

    async def interact(self, request: StreamInteractionRequest) -> object:
        """处理 Tool Approval / Question；返回 resume 值或 answers。"""


class StageAgentPort(Protocol):
    """执行一个 fresh Managed stage execution 的 seam。"""

    async def run(
        self,
        request: StageRequest,
        observer: StageObserver | None = None,
    ) -> StageResult: ...


def parse_structured_output(text: str) -> dict[str, Any]:
    """从 stage Agent 的最后消息中解析严格 JSON；容忍 markdown 围栏。

    失败时抛出可读 ValueError（空输出 / 长度与期望形状），绝不把
    json.loads 的裸解析器文本传播到 wire。
    """
    content = str(text or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    if not content:
        raise ValueError("stage 输出为空：模型没有产出 JSON 对象")
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise ValueError(
            f"stage 输出不是有效 JSON（输出长度 {len(content)} 字符，"
            "期望单个 JSON 对象，不要附加解释文字）"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("STAGE_OUTPUT_NOT_OBJECT")
    return dict(parsed)


def compose_scope_stage(stage_agent_id: str) -> str:
    """把 stage Agent id 映射为协议 compose_scope.stage 枚举。"""
    if stage_agent_id in REVIEWER_STAGES:
        return "review"
    if stage_agent_id in {"understand", "plan", "build", "verify"}:
        return stage_agent_id
    return "build"


def make_activity_scope(
    *,
    stage_agent_id: str,
    attempt: int,
    invocation_id: str,
    task_id: str | None = None,
    task_title: str | None = None,
) -> dict[str, object]:
    """构造严格有界的 ComposeActivityScope 字典。"""
    scope: dict[str, object] = {
        "activity_id": f"{compose_scope_stage(stage_agent_id)}-{max(1, attempt)}-{invocation_id[:12]}",
        "stage": compose_scope_stage(stage_agent_id),
        "attempt": max(1, int(attempt)),
    }
    if task_id:
        scope["task_id"] = task_id
    if task_title:
        scope["task_title"] = str(task_title)[:200]
    return scope


def summarize_understanding(artifact: Any) -> str:
    """基于已校验 Understanding 生成有界阶段摘要。"""
    goal = str(getattr(artifact, "goal", "") or "")[:200]
    constraints = getattr(artifact, "constraints", ()) or ()
    acceptance = getattr(artifact, "acceptance", ()) or ()
    open_decisions = getattr(artifact, "open_decisions", ()) or ()
    text = "\n".join(
        [
            f"目标：{goal or '（空）'}",
            f"约束 {len(constraints)} 项；验收 {len(acceptance)} 项",
            f"仍需用户决策：{'是' if open_decisions else '否'}",
        ]
    )
    return text[:1000]


def summarize_plan(artifact: Any) -> str:
    """基于已校验 Plan 生成有界阶段摘要。"""
    solution = str(getattr(artifact, "solution", "") or "")[:200]
    tasks = getattr(artifact, "tasks", ()) or ()
    text = "\n".join(
        [
            f"方案：{solution or '（空）'}",
            f"任务数：{len(tasks)}",
            "下一步：等待整体方案批准",
        ]
    )
    return text[:1000]


def summarize_stage_failure(message: str) -> str:
    """schema invalid 等失败时的稳定摘要；不携带原始模型输出。"""
    return f"阶段输出校验失败：{str(message)[:200]}"[:1000]


def summarize_build(*, task_title: str, task_result: Any) -> str:
    """基于已校验 TaskResult 生成有界 Build 摘要；不展示 Builder 自报原文。"""
    paths = getattr(task_result, "changed_paths", ()) or ()
    evidence = str(getattr(task_result, "focused_test_evidence", "") or "").strip()
    text = "\n".join(
        [
            f"任务：{str(task_title or '')[:120] or '（未命名）'}",
            f"变更路径：{len(paths)} 个",
            f"focused evidence：{'已记录' if evidence else '未记录'}",
        ]
    )
    return text[:1000]


def summarize_verify(
    *,
    commands: int,
    passed: int,
    failed: int,
    blocked: int,
    failed_label: str = "",
) -> str:
    """基于已校验 VerificationEvidence 计数生成有界 Verify 摘要。"""
    lines = [
        f"命令：{commands} 条；通过 {passed}；失败 {failed}；阻塞 {blocked}",
    ]
    if failed_label:
        lines.append(f"失败命令：{failed_label[:120]}")
    return "\n".join(lines)[:1000]


def summarize_review(*, requirement_verdict: str, code_verdict: str, findings: Any) -> str:
    """基于已校验 Review report 生成有界 Review 摘要。"""
    items = findings or ()
    required = 0
    optional = 0
    for finding in items:
        if isinstance(finding, Mapping):
            value = str(finding.get("severity") or "")
        else:
            severity = getattr(finding, "severity", None)
            value = severity.value if hasattr(severity, "value") else str(severity or "")
        if value in {"critical", "required"}:
            required += 1
        elif value:
            optional += 1
    fix = "是" if required > 0 else "否"
    text = "\n".join(
        [
            f"Requirement：{requirement_verdict}；Code：{code_verdict}",
            f"Required finding：{required}；Optional/Nit：{optional}",
            f"进入 fix loop：{fix}",
        ]
    )
    return text[:1000]


class ManagedStageAgentPort:
    """通过 AgentDelegator 运行内置 stage spec 的 Host-backed 实现。

    stage Agent 复用主 Agent 的 resolved spec（模型、Policy、Skill、MCP），
    由 Delegator 登记为 root Run 的 child execution；author 执行与
    Reviewer 永远不在同一个 execution identity 下。

    图执行使用共享 execution stream 的 capture_only：公开 Reasoning / Tool
    经 observer 实时发出，artifact 正文只返回 parser。
    """

    def __init__(
        self,
        *,
        registry: AgentExecutionRegistry,
        pool: AgentEnginePool,
        resolve_spec: Any,
        config_home: Path,
        workspace: Path,
    ) -> None:
        """注入 registry/pool 与按 profile key 解析 spec 的回调。"""
        self._registry = registry
        self._pool = pool
        self._resolve_spec = resolve_spec
        self._config_home = config_home
        self._workspace = workspace

    async def run(
        self,
        request: StageRequest,
        observer: StageObserver | None = None,
    ) -> StageResult:
        """解析可信 spec（Reviewer 用只读视图，全部 stage 关闭 ask_user）。"""
        spec = self._resolve_spec(
            request.profile_key,
            headless=True,
            readonly=request.stage in REVIEWER_STAGES,
            planning=request.stage in PLANNING_STAGES,
        )
        if spec is None:
            raise RuntimeError("COMPOSE_STAGE_SPEC_MISSING")
        profile = spec.runtime_profile
        started_at = time.monotonic()

        async def invoke(engine: Any, command: Any) -> Mapping[str, Any]:
            """在独立 checkpoint namespace 中流式运行 stage Agent。"""
            from harness_agent.threads.context_lifecycle import ContextLifecycle

            child_ref = child_execution_ref(command)
            if observer is not None:
                observer.bind_execution(
                    execution_id=child_ref.execution_id,
                    parent_execution_id=child_ref.parent_execution_id,
                    agent_id=request.stage,
                )
                observer.emit(
                    RUN_PROGRESS,
                    {
                        "phase": "model",
                        "elapsed_ms": max(
                            0, round((time.monotonic() - started_at) * 1000)
                        ),
                    },
                )
            context_snapshot = ContextLifecycle(
                spec.workspace,
                home=self._config_home,
            ).prepare(
                thread_id=child_ref.thread_id,
                spec=spec,
            )
            context = RunContext(
                thread_id=child_ref.thread_id,
                run_id=child_ref.run_id,
                context_snapshot=context_snapshot,
                skill_registry=spec.skill_registry,
                approval_mode=(
                    spec.effective_policy.approval_mode
                    or spec.execution.approval_mode
                ),
                profile_key=profile.profile_key,
                execution_id=child_ref.execution_id,
                parent_execution_id=child_ref.parent_execution_id,
                agent_id=spec.agent_id,
                execution_mode=ExecutionMode.MANAGED,
                cancellation_token=command.cancellation_token,
                delegation_policy=spec.effective_policy.delegation,
            )
            graph = engine.graph
            session = StreamSession(
                run_id=child_ref.run_id,
                started_at=started_at,
            )
            ports = _StageStreamPorts(observer)
            graph_config = {
                "configurable": {
                    "thread_id": child_ref.thread_id,
                    "checkpoint_ns": child_ref.checkpoint_namespace(
                        spec.project_fingerprint
                    ),
                }
            }
            resume: object | None = None
            final_content = ""
            from langgraph.types import Command

            while True:
                if command.cancellation_token.cancelled:
                    raise asyncio_cancelled()
                stream_input: object = (
                    Command(resume=resume)
                    if resume is not None
                    else {"messages": [HumanMessage(content=command.task)]}
                )
                try:
                    result = await execute_stream(
                        ExecutionStreamRequest(
                            agent=graph,
                            stream_input=stream_input,
                            graph_config=graph_config,
                            context=context,
                            content_visibility="capture_only",
                            session=session,
                            is_cancelled=lambda: command.cancellation_token.cancelled,
                        ),
                        ports,
                    )
                except ExecutionStreamError as exc:
                    if exc.code == "RUN_CANCELLED":
                        raise asyncio_cancelled() from exc
                    raise
                final_content = result.final_content or final_content
                if result.resume is None:
                    break
                resume = result.resume
            return {"final": str(final_content)}

        target = DelegationTarget(
            agent_id=request.stage,
            mode=ExecutionMode.MANAGED,
            runner=managed_engine_runner(self._pool, profile, invoke),
            description=f"Compose {request.stage} stage",
            model=spec.model_view,
            policy_fingerprint=spec.effective_policy.fingerprint,
            engine_profile_key=profile.profile_key,
            definition_fingerprint=spec.definition_fingerprint,
        )
        delegator = AgentDelegator(self._registry, targets=(target,))
        from harness_agent.runtime.agent_catalog import DelegationPolicy
        from harness_agent.runtime.agent_delegation import DelegateAgent

        idempotency_key = hashlib.sha256(
            f"{request.stage}:{request.parent_ref.execution_id}:{request.invocation_id}:{hashlib.sha256(request.task.encode('utf-8')).hexdigest()[:16]}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        # stage 不能复用主 Agent 的 delegation policy：其 allowed_agents 只含
        # general-purpose/Plugin id，内置 stage id 会被 DELEGATION_TARGET_FORBIDDEN
        # 拒绝。这里收紧为只允许当前 stage 一个 id，且 depth 封顶 1（stage
        # Agent 不能再委派），不扩大任何委派权限。
        stage_policy = DelegationPolicy(
            enabled=True,
            allowed_agents=(request.stage,),
            max_depth=1,
            max_parallelism=1,
        )
        command = DelegateAgent(
            parent_ref=request.parent_ref,
            target_agent_id=request.stage,
            task=request.task,
            idempotency_key=idempotency_key,
            delegation_policy=stage_policy,
            cancellation_token=request.cancellation_token,
            timeout_seconds=request.timeout_seconds,
        )
        result = await delegator.execute(command)
        raw_final = str(result.output.get("final", ""))
        return StageResult(
            execution_id=result.ref.execution_id,
            agent_id=result.agent_id,
            status=result.status.value,
            output=parse_structured_output(raw_final),
        )


def asyncio_cancelled() -> BaseException:
    """构造 CancelledError，避免在模块顶层依赖 asyncio 导入顺序。"""
    import asyncio

    return asyncio.CancelledError()


class _StageStreamPorts:
    """把 stream signal 转给 StageObserver；不捕获根 Transcript。"""

    def __init__(self, observer: StageObserver | None) -> None:
        self._observer = observer
        # tool_call_id → 工具名；TOOL_COMPLETED 本身不带 name，用 started 事件关联。
        self._tool_names: dict[str, str] = {}

    def emit(self, signal: Any) -> None:
        # capture_only 本就不会发 content.delta；双保险丢弃正文事件。
        if signal.type == CONTENT_DELTA:
            return
        if self._observer is None:
            return
        self._observer.emit(signal.type, dict(signal.payload))
        payload = dict(signal.payload)
        if signal.type == TOOL_STARTED:
            tool_id = str(payload.get("tool_call_id") or "")
            name = str(payload.get("name") or "")
            if tool_id and name:
                self._tool_names[tool_id] = name
        elif signal.type == TOOL_COMPLETED:
            tool_id = str(payload.get("tool_call_id") or "")
            result = payload.get("result")
            is_error = (
                bool(result.get("is_error"))
                if isinstance(result, Mapping)
                else False
            )
            self._observer.record_tool_terminal(
                tool_name=self._tool_names.get(tool_id) or "tool",
                status="failed" if is_error else "completed",
            )

    async def interact(self, request: StreamInteractionRequest) -> object:
        if self._observer is None:
            raise ExecutionStreamError(
                "STAGE_INTERACTION_UNBOUND",
                "Stage Interaction requires a StageObserver",
            )
        return await self._observer.interact(request)

    async def observe_message(self, chunk: object, session: StreamSession) -> bool:
        return False

    async def after_tool_boundary(self) -> None:
        return None

    def on_stream_event(self) -> None:
        return None
