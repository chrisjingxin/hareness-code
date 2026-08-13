"""ComposeWorkItemEngine 的生产驱动组装（WP15 cutover）。

所有模型/工具执行统一经过 ManagedStageAgentPort（ManagedAgentExecutor +
RoleBindingRegistry + 结构化输出解析）；Verification 复用 canonical
VerificationPort。本模块只把领域上下文渲染成有界任务文本并解析严格 schema，
不拥有 SQLite、graph 或 Host 细节。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from harness_agent.compose.activities.implement import (
    ImplementItemContext,
    ImplementItemOutcome,
    ImplementItemResult,
)
from harness_agent.compose.activities.plan import PlanDraft, PlanDraftContext
from harness_agent.compose.activities.review import (
    ReviewAxisResult,
    ReviewContext,
    ReviewFinding,
)
from harness_agent.compose.activities.spec import SpecDraftContext
from harness_agent.compose.activities.task import TaskInterviewContext
from harness_agent.compose.activities.verify import VerificationPort
from harness_agent.compose.guard import ReportContext
from harness_agent.compose.models import FindingSeverity
from harness_agent.compose.stage_agents import (
    StageAgentPort,
    StageRequest,
    parse_structured_output,
)
from harness_agent.compose.turn_intent import TurnIntentContext

_STAGE_TIMEOUT_SECONDS = 600.0


class EngineServicesError(RuntimeError):
    """生产驱动组装与解析的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(slots=True)
class EngineDriverServices:
    """Host 提供的 Work Item engine 依赖；adapter 只经此 seam 使用。"""

    stage_agent: StageAgentPort
    parent_ref: Any
    workspace_root: str = ""
    verification: VerificationPort | None = None
    profile_key: str = ""
    cancellation_token: Any = None
    stage_observer: Any | None = None
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))


_BODY_START = "---BEGIN-BODY---"
_BODY_END = "---END-BODY---"
_PLAN_START = "---BEGIN-PLAN---"
_PLAN_END = "---END-PLAN---"
_TODO_START = "---BEGIN-TODO---"
_TODO_END = "---END-TODO---"


def _extract_between(text: str, start_marker: str, end_marker: str) -> str | None:
    """提取标记对之间的正文；任一标记缺失返回 None（fail closed）。"""
    start = text.find(start_marker)
    if start == -1:
        return None
    body_start = start + len(start_marker)
    end = text.find(end_marker, body_start)
    if end == -1:
        return None
    body = text[body_start:end].strip()
    return body or None


def _parse_json_lenient(text: str) -> dict[str, Any]:
    """尝试解析 JSON 对象（含 prose/控制字符回退）；失败返回空 dict。"""
    try:
        return parse_structured_output(text)
    except ValueError:
        return {}


def _draft_body(raw_text: str, *, json_field: str) -> str | None:
    """标记分隔优先，JSON 对象字段兜底；两者都失败返回 None。"""
    body = _extract_between(raw_text, _BODY_START, _BODY_END)
    if body is not None:
        return body
    parsed = _parse_json_lenient(raw_text)
    body = parsed.get(json_field)
    return body if isinstance(body, str) and body.strip() else None


class _StageDriverBase:
    """共享 stage 请求构造与严格 schema 解析。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._services = services

    def _request(self, *, stage: str, task: str) -> StageRequest:
        return StageRequest(
            stage=stage,
            task=task[:32_000],
            parent_ref=self._services.parent_ref,
            profile_key=self._services.profile_key,
            cancellation_token=self._services.cancellation_token,
            timeout_seconds=_STAGE_TIMEOUT_SECONDS,
        )

    async def _run(
        self,
        *,
        stage: str,
        task: str,
        raw: bool = False,
    ) -> tuple[Mapping[str, Any], str]:
        result = await self._services.stage_agent.run(
            self._request(stage=stage, task=task),
            self._services.stage_observer,
        )
        if raw:
            return {"raw": result.raw_final}, result.execution_id
        output = parse_structured_output(json.dumps(result.output, ensure_ascii=False))
        return output, result.execution_id

    def _stage(self, name: str) -> str:
        return f"work-item-{name}"

class ManagedTurnIntentClassifier:
    """生产 TurnIntentResolver 分类器：小上下文结构化输出。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def classify(self, context: TurnIntentContext) -> Mapping[str, object]:
        task = (
            "你是 Compose 意图分类器。只输出一个 JSON 对象："
            '{"intent": "resume_current|amend_current|start_new_work|side_question|unclear", '
            '"detail": "<可选简述>"}。\n'
            f"目标摘要：{context.goal_summary or '无'}\n"
            f"当前是否有进行中的 Work Item：{context.has_active_work_item}\n"
            f"本条消息：{context.message}\n"
            "规则：不明确或需要用户选择时输出 unclear；分类无权放弃任务或创建冲突 Work Item。"
        )
        output, _ = await self._base._run(stage=self._base._stage("intent"), task=task)
        intent = output.get("intent")
        if intent not in {
            "resume_current",
            "amend_current",
            "start_new_work",
            "side_question",
            "unclear",
        }:
            raise EngineServicesError("CLASSIFIER_SCHEMA_INVALID")
        return {
            "intent": intent,
            "detail": str(output.get("detail", ""))[:400],
        }


class ManagedGrillDriver:
    """生产 grill 驱动：一次一个决策问题，成稿输出正文。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def next_question(self, context: TaskInterviewContext) -> str | None:
        answered = "\n".join(f"- 问: {q}\n- 答: {a}" for q, a in context.answers)
        feedback = f"\n用户修改反馈：{context.feedback}" if context.feedback else ""
        task = (
            "你是需求确认访谈员。一次只问一个最重要的决策问题；没有需要确认的问题时输出 null。"
            '只输出 JSON：{"question": "<问题>"} 或 {"question": null}。\n'
            f"目标：{context.goal}\n"
            f"已确认：{answered or '无'}{feedback}"
        )
        output, _ = await self._base._run(stage=self._base._stage("grill"), task=task)
        question = output.get("question")
        if question is None:
            return None
        if not isinstance(question, str) or not question.strip():
            raise EngineServicesError("GRILL_SCHEMA_INVALID")
        return question.strip()[:2000]

    async def draft_task(self, context: TaskInterviewContext) -> str:
        answered = "\n".join(f"- 问: {q}\n- 答: {a}" for q, a in context.answers)
        task = (
            "根据目标与已确认决策生成研发任务文档正文（不含 front matter）。"
            "正文直接写在标记对之间，不要用 JSON 包裹、不要转义换行或引号：\n"
            f"{_BODY_START}\n<Markdown 正文>\n{_BODY_END}\n"
            f"目标：{context.goal}\n已确认决策：{answered or '无'}"
        )
        output, _ = await self._base._run(stage=self._base._stage("task"), task=task, raw=True)
        body = _draft_body(str(output.get("raw", "")), json_field="body")
        if body is None:
            raise EngineServicesError("TASK_DRAFT_SCHEMA_INVALID")
        return body


class ManagedSpecDriver:
    """生产 spec 驱动：只消费 confirmed Task 摘要与 feedback。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def draft_spec(self, context: SpecDraftContext) -> str:
        feedback = f"\n用户修改反馈：{context.feedback}" if context.feedback else ""
        task = (
            "根据已确认任务生成行为规格正文（不含 front matter）：公开 interface、状态、"
            "错误语义、关键 invariant、安全边界与可观察验收。禁止复制 Task 代替行为规格。"
            "正文直接写在标记对之间，不要用 JSON 包裹、不要转义换行或引号：\n"
            f"{_BODY_START}\n<Markdown 正文>\n{_BODY_END}\n"
            f"目标：{context.goal}\n任务摘要：{context.task_body[:8000]}{feedback}"
        )
        output, _ = await self._base._run(stage=self._base._stage("spec"), task=task, raw=True)
        body = _draft_body(str(output.get("raw", "")), json_field="body")
        if body is None:
            raise EngineServicesError("SPEC_DRAFT_SCHEMA_INVALID")
        return body


class ManagedPlanDriver:
    """生产 plan 驱动：成对生成 plan/todo 正文。"""


    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def draft_plan(self, context: PlanDraftContext) -> PlanDraft:
        feedback = f"\n用户修改反馈：{context.feedback}" if context.feedback else ""
        failure = (
            f"\n上次草稿被拒绝，必须修正：{context.previous_failure}"
            if context.previous_failure
            else ""
        )
        task = (
            "根据已确认规格生成实施计划与执行清单两份正文（均不含 front matter）。"
            "todo 每项必须是以「- [ ] 」开头的 markdown 复选框行，后面跟该项标题，"
            "标题下一行缩进写动作与验收；每项还必须包含恰好一行以「验证=」开头的"
            "focused 验证命令（例如：验证=cd jsondiff && python -m pytest tests/test_diff.py -q）；"
            "不得包含『决定/评估/选择方案』类实施期设计项；两份正文都不得留下"
            "未填写项或模板变量，也不要解释、复述这些格式限制。正文直接写在"
            "标记对之间，不要用 JSON 包裹、"
            "不要转义换行或引号：\n"
            f"{_PLAN_START}\n<plan.md 正文>\n{_PLAN_END}\n"
            f"{_TODO_START}\n<todo.md 正文>\n{_TODO_END}\n"
            f"目标：{context.goal}\n规格摘要：{context.spec_body[:8000]}{feedback}{failure}"
        )
        output, _ = await self._base._run(stage=self._base._stage("plan"), task=task, raw=True)
        raw_text = str(output.get("raw", ""))
        plan_body = _extract_between(raw_text, _PLAN_START, _PLAN_END)
        todo_body = _extract_between(raw_text, _TODO_START, _TODO_END)
        if plan_body is None or todo_body is None:
            parsed = _parse_json_lenient(raw_text)
            plan_body = parsed.get("plan_body") if isinstance(parsed.get("plan_body"), str) else None
            todo_body = parsed.get("todo_body") if isinstance(parsed.get("todo_body"), str) else None
        if plan_body is None or todo_body is None:
            raise EngineServicesError("PLAN_DRAFT_SCHEMA_INVALID")
        return PlanDraft(plan_body=plan_body, todo_body=todo_body)


class ManagedImplementDriver:
    """生产 implement 驱动：单 Todo 项 TDD 执行。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def implement_item(self, context: ImplementItemContext) -> ImplementItemResult:
        previous = (
            f"\n上次失败与诊断：{context.previous_failure}"
            if context.previous_failure
            else ""
        )
        task = (
            "按 TDD 执行下面这一项 Todo：先写失败测试再实现再重构；行为变更必须给出"
            "fail-before 与 pass-after 证据。确实不适合测试的文档/配置项在 reason 里"
            "按原版 Skill 规则记录理由，不能伪造 RED。需要用户决策时 outcome 用 blocked。"
            '只输出 JSON：{"outcome": "completed|blocked|failed", '
            '"fail_before": "<命令与失败摘要>", "pass_after": "<命令与通过摘要>", '
            '"changed_paths": ["<相对路径>"], "reason": "", '
            '"blocked_message": "", "execution_id": ""}。\n'
            f"目标：{context.goal}\n当前项：{context.item_title[:4000]}{previous}"
        )
        output, _ = await self._base._run(
            stage=self._base._stage("implement"), task=task
        )
        outcome_raw = output.get("outcome")
        try:
            outcome = ImplementItemOutcome(str(outcome_raw))
        except ValueError as exc:
            raise EngineServicesError("IMPLEMENT_SCHEMA_INVALID") from exc
        changed = output.get("changed_paths")
        paths = tuple(str(path) for path in changed) if isinstance(changed, list) else ()
        return ImplementItemResult(
            outcome=outcome,
            fail_before=str(output.get("fail_before", "")),
            pass_after=str(output.get("pass_after", "")),
            changed_paths=paths,
            reason=str(output.get("reason", "")),
            blocked_message=str(output.get("blocked_message", "")),
            execution_id=str(output.get("execution_id", "")),
        )


class ManagedReviewDriver:
    """生产 review 驱动：单轴只读审查。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def review(self, context: ReviewContext) -> ReviewAxisResult:
        task = (
            "你是只读 Code Reviewer，按指定轴审查当前 Work Item 的实现。"
            "只输出 JSON："
            '{"axis": "<requirement|code>", "findings": [{"severity": '
            '"critical|required|optional|nit", "message": "<说明>", "location": ""}]}。\n'
            f"目标：{context.goal}\n审查轴：{context.axis}\n"
            f"验证摘要引用：{context.verification_digest}"
        )
        output, execution_id = await self._base._run(
            stage=self._base._stage("review"), task=task
        )
        if output.get("axis") != context.axis:
            raise EngineServicesError("REVIEW_AXIS_MISMATCH")
        raw_findings = output.get("findings")
        findings: list[ReviewFinding] = []
        if isinstance(raw_findings, list):
            for item in raw_findings:
                if not isinstance(item, Mapping):
                    continue
                try:
                    severity = FindingSeverity(str(item.get("severity")))
                except ValueError as exc:
                    raise EngineServicesError("REVIEW_SCHEMA_INVALID") from exc
                findings.append(
                    ReviewFinding(
                        severity=severity,
                        message=str(item.get("message", ""))[:2000],
                        location=str(item.get("location", ""))[:400],
                    )
                )
        return ReviewAxisResult(
            axis=context.axis,
            execution_id=execution_id,
            findings=tuple(findings),
        )


class ManagedReportDriver:
    """生产 report 驱动：只消费摘要引用。"""

    def __init__(self, services: EngineDriverServices) -> None:
        self._base = _StageDriverBase(services)

    async def draft_report(self, context: ReportContext) -> str:
        task = (
            "根据验证与双轴评审摘要生成完成报告正文（不含 front matter）。"
            "必须引用当前文档、验证与评审 digest，不遮蔽任何失败。"
            "正文直接写在标记对之间，不要用 JSON 包裹、不要转义换行或引号：\n"
            f"{_BODY_START}\n<Markdown 正文>\n{_BODY_END}\n"
            f"目标：{context.goal}\n验证 digest：{context.verification_digest}\n"
            f"Requirement Review：{context.requirement_review_digest}\n"
            f"Code Review：{context.code_review_digest}"
        )
        output, _ = await self._base._run(stage=self._base._stage("report"), task=task, raw=True)
        body = _extract_between(str(output.get("raw", "")), _BODY_START, _BODY_END)
        if body is None:
            raise EngineServicesError("REPORT_SCHEMA_INVALID")
        return body
