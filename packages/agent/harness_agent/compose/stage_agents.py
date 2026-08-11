"""Compose StageAgentPort：fresh Managed stage execution seam。

StageAgentPort 由 Harness 内置 stage Agent adapter 实现，底层复用
AgentDelegator / AgentEnginePool：stage Agent 使用与主 Agent 相同的
可信 spec/Policy（Compose 不提权），但每个 stage 都拿到 fresh
RunContext 与独立 checkpoint namespace，不继承前一个 stage 的对话。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from langchain_core.messages import HumanMessage

from harness_agent.runtime.agent_delegation import (
    AgentDelegator,
    DelegationTarget,
    child_execution_ref,
    managed_engine_runner,
)
from harness_agent.runtime.execution_binding import ExecutionMode, ExecutionRef
from harness_agent.runtime.run_context import RunCancellationToken, RunContext

if TYPE_CHECKING:
    from harness_agent.runtime.agent_engine import AgentEnginePool
    from harness_agent.runtime.agent_execution import AgentExecutionRegistry
    from harness_agent.runtime.agent_spec import ResolvedAgentSpec

STAGE_AGENT_TIMEOUT_SECONDS = 600.0

# Reviewer 使用只读能力视图；作者 execution 不得兼任 Reviewer。
REVIEWER_STAGES = frozenset({"requirement-reviewer", "code-reviewer"})


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
    ) -> None:
        """保存 stage 身份、有界任务文本与父 execution 引用。"""
        if not stage or not task.strip():
            raise ValueError("STAGE_REQUEST_INVALID")
        self.stage = stage
        self.task = task
        self.parent_ref = parent_ref
        self.profile_key = profile_key
        self.cancellation_token = cancellation_token
        self.timeout_seconds = timeout_seconds


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


class StageAgentPort(Protocol):
    """执行一个 fresh Managed stage execution 的 seam。"""

    async def run(self, request: StageRequest) -> StageResult: ...


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


class ManagedStageAgentPort:
    """通过 AgentDelegator 运行内置 stage spec 的 Host-backed 实现。

    stage Agent 复用主 Agent 的 resolved spec（模型、Policy、Skill、MCP），
    由 Delegator 登记为 root Run 的 child execution；author 执行与
    Reviewer 永远不在同一个 execution identity 下。
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

    async def run(self, request: StageRequest) -> StageResult:
        """解析可信 spec（Reviewer 用只读视图，全部 stage 关闭 ask_user）。"""
        spec = self._resolve_spec(
            request.profile_key,
            headless=True,
            readonly=request.stage in REVIEWER_STAGES,
        )
        if spec is None:
            raise RuntimeError("COMPOSE_STAGE_SPEC_MISSING")
        profile = spec.runtime_profile

        async def invoke(engine: Any, command: Any) -> Mapping[str, Any]:
            """在独立 checkpoint namespace 中运行 stage Agent 并返回正文。"""
            from harness_agent.threads.context_lifecycle import ContextLifecycle

            child_ref = child_execution_ref(command)
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
            result = await engine.graph.ainvoke(
                {"messages": [HumanMessage(content=command.task)]},
                config={
                    "configurable": {
                        "thread_id": child_ref.thread_id,
                        "checkpoint_ns": child_ref.checkpoint_namespace(
                            spec.project_fingerprint
                        ),
                    }
                },
                context=context,
            )
            messages = result.get("messages", ()) if isinstance(result, Mapping) else ()
            final = getattr(messages[-1], "content", "") if messages else ""
            return {"final": str(final)}

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
            f"{request.stage}:{request.parent_ref.execution_id}:{hashlib.sha256(request.task.encode('utf-8')).hexdigest()[:16]}".encode("utf-8")
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
