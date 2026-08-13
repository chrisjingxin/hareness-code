"""Compose 工作模式的领域模型：状态、artifact 与有界校验。

Compose 的所有阶段事实都落在严格 artifact 上：Understanding、Plan、
TaskResult、VerificationEvidence、ReviewReport。模型只保留结构化、
有界 payload；源码、完整 Tool 输出、凭据和绝对用户目录不进入 artifact。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

MAX_ARTIFACT_PAYLOAD_BYTES = 256 * 1024
MAX_TASKS = 32
MAX_FIELD_CHARS = 2000
MAX_ACCEPTANCE_CHARS = 2000
MAX_COMMAND_CHARS = 2000
MAX_COMMANDS_PER_TASK = 20
MAX_FINDINGS = 50
MAX_CHANGED_PATHS = 200

_PLACEHOLDER_LINE_PATTERN = re.compile(
    r"^\s*(?:(?:#{1,6}|[-*+]|\d+[.)])\s+)?"
    r"(?:\[[ xX]\]\s+)?(?:TODO|TBD|待补充|待定)"
    r"\s*(?:(?:[:：—-])\s*.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
_TEMPLATE_PLACEHOLDER_PATTERN = re.compile(
    r"^\s*(?:(?:#{1,6}|[-*+]|\d+[.)])\s+)?"
    r"(?:[^{}\n]{1,40}[:：=]\s*)?"
    r"\{\{\s*[^{}\n]*\s*\}\}\s*$",
    re.MULTILINE,
)


class ComposeStage(str, Enum):
    """Compose 五阶段；顺序由状态机推进，模型不能自行跳阶段。"""

    UNDERSTAND = "understand"
    PLAN = "plan"
    BUILD = "build"
    VERIFY = "verify"
    REVIEW = "review"


class StageState(str, Enum):
    """单个阶段的确定性状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ComposeRunStatus(str, Enum):
    """Run 级状态；终态由 RunCoordinator 唯一收敛到 wire。"""

    RUNNING = "running"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ThreadMode(str, Enum):
    """Thread 首个有效 Run 冻结的工作模式。"""

    BUILD = "build"
    COMPOSE = "compose"


class ComposeWorkItemStatus(str, Enum):
    """跨 Run 持续的 Compose Work Item 生命周期状态。"""

    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABANDONED = "abandoned"

    @property
    def terminal(self) -> bool:
        """只有完成和放弃会释放同 Thread 的 active Work Item 槽位。"""
        return self in {
            ComposeWorkItemStatus.COMPLETED,
            ComposeWorkItemStatus.ABANDONED,
        }


class ComposeDocumentKind(str, Enum):
    """Compose Work Item 固定维护的 Workspace Markdown 文档类型。"""

    TASK = "task"
    SPEC = "spec"
    PLAN = "plan"
    TODO = "todo"
    REPORT = "report"



class ComposeActivityStatus(str, Enum):
    """Work Item 内一次有界 Activity 的可恢复执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    RETRYABLE_FAILED = "retryable_failed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ComposeEffectStatus(str, Enum):
    """一次外部副作用的 ledger 状态；receipt 只能来自真实对账。"""

    INTENT = "intent"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ComposeActivity:
    """Activity 的执行事实投影；attempt 表示同一 identity 的恢复轮次。"""

    activity_id: str
    work_item_id: str
    run_id: str | None
    kind: str
    status: ComposeActivityStatus
    attempt: int
    started_at_ms: int
    finished_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ComposeEffect:
    """EffectIntent/Receipt 的 ledger 事实；intent 与 receipt 均为有界 JSON。"""

    effect_key: str
    work_item_id: str
    activity_id: str | None
    intent: Mapping[str, object]
    receipt: Mapping[str, object] | None
    status: ComposeEffectStatus
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class ComposeEvidence:
    """Verification/Review 证据事实；content_digest 绑定产出摘要。"""

    evidence_id: str
    work_item_id: str
    evidence_kind: str
    content_digest: str
    payload: Mapping[str, object]
    created_at_ms: int


class TaskStatus(str, Enum):
    """Plan task 的执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvidenceStatus(str, Enum):
    """Verify 命令的展示状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChangeKind(str, Enum):
    """任务变更类型；决定 Runtime 使用 TDD 还是 direct 执行。"""

    BEHAVIOR = "behavior"
    BUG = "bug"
    REFACTOR = "refactor"
    DOCS = "docs"
    CONFIG = "config"
    STYLE = "style"


class ArtifactKind(str, Enum):
    """Compose artifact 种类；每类只有唯一 payload schema。"""

    UNDERSTANDING = "understanding"
    PLAN = "plan"
    TASK_RESULT = "task_result"
    VERIFICATION = "verification"
    REVIEW = "review"


class ReviewAxis(str, Enum):
    """Review 双轴：spec 正确性与代码结构/安全。"""

    REQUIREMENT = "requirement"
    CODE = "code"


class FindingSeverity(str, Enum):
    """finding 严重度；Critical/Required 阻断完成，Optional/Nit 只入报告。"""

    CRITICAL = "critical"
    REQUIRED = "required"
    OPTIONAL = "optional"
    NIT = "nit"


class ComposeStoreError(RuntimeError):
    """Compose 存储/校验错误；上层收敛为稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ComposeWorkItem:
    """Compose 长期研发目标的持久化投影；正文由后续 Markdown store 管理。"""

    work_item_id: str
    thread_id: str
    slug: str
    goal: str
    status: ComposeWorkItemStatus
    revision: int
    created_at_ms: int
    updated_at_ms: int
    terminal_at_ms: int | None = None
    amends_work_item_id: str | None = None

    @property
    def terminal(self) -> bool:
        """返回 Work Item 是否不可再由普通 Turn 恢复执行。"""
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class ComposeTask:
    """Plan 中的一项执行任务；acceptance 与 verification 必须非空。"""

    id: str
    title: str
    kind: ChangeKind
    acceptance: str
    depends_on: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING

    def to_projection(self) -> dict[str, object]:
        """只暴露 wire 允许的 id/title/status。"""
        return {"id": self.id, "title": self.title, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Verify 命令的 projection 条目；label 是命令的稳定身份。"""

    label: str
    status: EvidenceStatus


@dataclass(slots=True)
class ComposeRunState:
    """Compose Run 的唯一状态事实；由 ComposeStateMachine 推进。

    状态机每次 transition 都先拷贝再修改，实例自身不可复用；非 frozen
    是为了允许 handler 在一次拷贝上完成多字段更新。
    """

    thread_id: str
    run_id: str
    revision: int = 0
    stage: ComposeStage = ComposeStage.UNDERSTAND
    status: ComposeRunStatus = ComposeRunStatus.RUNNING
    stages: dict[ComposeStage, StageState] = field(default_factory=dict)
    stage_attempts: dict[ComposeStage, int] = field(default_factory=dict)
    schema_retry_used: dict[ComposeStage, bool] = field(default_factory=dict)
    understanding_artifact_id: str | None = None
    plan_artifact_id: str | None = None
    tasks: tuple[ComposeTask, ...] = ()
    verification_evidence_id: str | None = None
    review_report_id: str | None = None
    evidence: tuple[EvidenceItem, ...] = ()
    verify_fix_round: int = 0
    review_fix_round: int = 0
    blocked_reason: str | None = None

    @property
    def terminal(self) -> bool:
        """Run 是否已进入唯一终态。"""
        return self.status in {
            ComposeRunStatus.COMPLETED,
            ComposeRunStatus.FAILED,
            ComposeRunStatus.BLOCKED,
            ComposeRunStatus.CANCELLED,
        }

    def projection(self) -> dict[str, object]:
        """生成有界完整 projection；不含 artifact 正文、Prompt 或内部配置。"""
        return {
            "revision": self.revision,
            "stage": self.stage.value,
            "status": self.status.value,
            "stages": [
                {
                    "id": stage.value,
                    "status": self.stages.get(stage, StageState.PENDING).value,
                    "attempts": self.stage_attempts.get(stage, 0),
                }
                for stage in ComposeStage
            ],
            "tasks": [task.to_projection() for task in self.tasks],
            "evidence": [
                {"label": item.label, "status": item.status.value}
                for item in self.evidence
            ],
            "blocked_reason": self.blocked_reason,
        }


# ---------- 有界字符串辅助 ----------


def _bounded_text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if not allow_empty and not text.strip():
        raise ValueError(f"{field_name} must be non-empty")
    if len(text) > MAX_FIELD_CHARS:
        raise ValueError(f"{field_name} exceeds {MAX_FIELD_CHARS} chars")
    return text


def _bounded_strings(value: object, field_name: str, *, max_items: int = 32) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds {max_items} items")
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, field_name, allow_empty=False)
        result.append(text)
    return tuple(result)


def has_placeholder(text: str) -> bool:
    """只检测结构化占位行或完整模板表达式，避免误伤正文与代码。"""
    # 占位符是文档结构而非任意子串：`todo.md`、说明文字，以及 Python/JSON
    # 嵌套字典结尾的 `}}` 都是合法内容；独立占位行与完整模板变量仍 fail closed。
    return bool(
        _PLACEHOLDER_LINE_PATTERN.search(text)
        or _TEMPLATE_PLACEHOLDER_PATTERN.search(text)
    )


# ---------- Understanding ----------


@dataclass(frozen=True, slots=True)
class UnderstandingArtifact:
    """Understand 阶段输出：goal、约束、验收与非范围。"""

    goal: str
    constraints: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    open_decisions: tuple[str, ...] = ()
    change_kind: str = "unknown"


def validate_understanding_artifact(value: Mapping[str, object]) -> UnderstandingArtifact:
    """校验 Understanding payload；open_decisions 非空表示仍需用户决策。"""
    goal = _bounded_text(value.get("goal"), "goal")
    change_kind = _bounded_text(value.get("change_kind", "unknown"), "change_kind", allow_empty=True)
    if change_kind not in {"feature", "bugfix", "refactor", "docs", "config", "unknown"}:
        raise ValueError("change_kind must be a known change kind")
    return UnderstandingArtifact(
        goal=goal,
        constraints=_bounded_strings(value.get("constraints"), "constraints"),
        acceptance=_bounded_strings(value.get("acceptance"), "acceptance"),
        out_of_scope=_bounded_strings(value.get("out_of_scope"), "out_of_scope"),
        open_decisions=_bounded_strings(value.get("open_decisions"), "open_decisions"),
        change_kind=change_kind,
    )


# ---------- Plan ----------


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    """Plan 阶段输出：方案、有序任务与相关源码指针。"""

    solution: str
    tasks: tuple[ComposeTask, ...]
    relevant_pointers: tuple[str, ...] = ()


def find_dag_cycle(tasks: tuple[ComposeTask, ...]) -> tuple[str, ...] | None:
    """返回任务依赖图中的一个环；无环返回 None。"""
    by_id = {task.id: task for task in tasks}
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def visit(task_id: str) -> tuple[str, ...] | None:
        if task_id in done:
            return None
        if task_id in visiting:
            cycle_start = stack.index(task_id)
            return tuple(stack[cycle_start:] + [task_id])
        visiting.add(task_id)
        stack.append(task_id)
        task = by_id.get(task_id)
        if task is not None:
            for dependency in task.depends_on:
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle
        stack.pop()
        visiting.discard(task_id)
        done.add(task_id)
        return None

    for task in tasks:
        cycle = visit(task.id)
        if cycle is not None:
            return cycle
    return None


def validate_plan_artifact(value: Mapping[str, object]) -> PlanArtifact:
    """校验 Plan payload：无 placeholder、DAG 无环、acceptance/verification 有界。"""
    solution = _bounded_text(value.get("solution"), "solution")
    if has_placeholder(solution):
        raise ValueError("plan contains placeholder")

    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan must contain tasks")
    if len(raw_tasks) > MAX_TASKS:
        raise ValueError(f"plan exceeds {MAX_TASKS} tasks")
    tasks: list[ComposeTask] = []
    task_ids: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise ValueError("task must be an object")
        task_id = _bounded_text(raw.get("id"), "task.id")
        if task_id in task_ids:
            raise ValueError("task id must be unique")
        task_ids.add(task_id)
        title = _bounded_text(raw.get("title"), "task.title")
        if has_placeholder(title):
            raise ValueError("task.title contains placeholder")
        kind = _bounded_text(raw.get("kind", "behavior"), "task.kind", allow_empty=True)
        if kind not in ChangeKind._value2member_map_:
            raise ValueError("task.kind must be a known change kind")
        acceptance = _bounded_text(raw.get("acceptance"), "task.acceptance")
        if len(acceptance) > MAX_ACCEPTANCE_CHARS:
            raise ValueError("task.acceptance exceeds limit")
        if has_placeholder(acceptance):
            raise ValueError("task.acceptance contains placeholder")
        depends_raw = raw.get("depends_on", [])
        if not isinstance(depends_raw, list) or any(not isinstance(d, str) or not d for d in depends_raw):
            raise ValueError("task.depends_on must be a list of ids")
        commands_raw = raw.get("verification_commands", [])
        if not isinstance(commands_raw, list):
            raise ValueError("task.verification_commands must be a list")
        if len(commands_raw) > MAX_COMMANDS_PER_TASK:
            raise ValueError("task.verification_commands exceeds limit")
        commands: list[str] = []
        for command in commands_raw:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("verification command must be non-empty")
            if len(command) > MAX_COMMAND_CHARS:
                raise ValueError("verification command exceeds limit")
            commands.append(command)
        tasks.append(
            ComposeTask(
                id=task_id,
                title=title,
                kind=ChangeKind(kind),
                acceptance=acceptance,
                depends_on=tuple(depends_raw),
                verification_commands=tuple(commands),
            )
        )
    for task in tasks:
        unknown = [dep for dep in task.depends_on if dep not in task_ids]
        if unknown:
            raise ValueError("task.depends_on references unknown task")
    cycle = find_dag_cycle(tuple(tasks))
    if cycle is not None:
        raise ValueError(f"plan task dependency cycle: {' -> '.join(cycle)}")
    return PlanArtifact(
        solution=solution,
        tasks=tuple(tasks),
        relevant_pointers=_bounded_strings(
            value.get("relevant_pointers"), "relevant_pointers"
        ),
    )


# ---------- TaskResult ----------


@dataclass(frozen=True, slots=True)
class TaskResultArtifact:
    """Build 单个任务的执行结果。"""

    task_id: str
    changed_paths: tuple[str, ...]
    focused_test_evidence: str
    red_evidence: str = ""
    remaining_issue: str = ""


def validate_task_result_artifact(value: Mapping[str, object]) -> TaskResultArtifact:
    """校验 TaskResult payload；focused evidence 是完成任务的必要条件。"""
    task_id = _bounded_text(value.get("task_id"), "task_id")
    evidence = _bounded_text(value.get("focused_test_evidence"), "focused_test_evidence")
    paths = value.get("changed_paths")
    if paths is None:
        paths = []
    if not isinstance(paths, list) or len(paths) > MAX_CHANGED_PATHS:
        raise ValueError("changed_paths must be a bounded list")
    return TaskResultArtifact(
        task_id=task_id,
        changed_paths=tuple(_bounded_text(p, "changed_paths") for p in paths),
        focused_test_evidence=evidence,
        red_evidence=_bounded_text(
            value.get("red_evidence", ""), "red_evidence", allow_empty=True
        ),
        remaining_issue=_bounded_text(
            value.get("remaining_issue", ""), "remaining_issue", allow_empty=True
        ),
    )


# ---------- VerificationEvidence ----------


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Verify 命令的真实执行事实；exit code 是唯一通过证据。"""

    command: str
    working_dir: str
    started_at_ms: int
    finished_at_ms: int
    exit_code: int
    output_digest: str
    output_summary: str
    truncated: bool = False


def validate_verification_evidence(value: Mapping[str, object]) -> VerificationEvidence:
    """校验 Verification payload；只有本轮 fresh exit code 0 才算 pass。"""
    command = _bounded_text(value.get("command"), "command")
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError("command exceeds limit")
    working_dir = _bounded_text(value.get("working_dir"), "working_dir")
    started = value.get("started_at_ms")
    finished = value.get("finished_at_ms")
    if not isinstance(started, int) or not isinstance(finished, int) or started < 0 or finished < started:
        raise ValueError("timestamps must satisfy 0 <= started <= finished")
    exit_code = value.get("exit_code")
    if not isinstance(exit_code, int) or exit_code < 0 or exit_code > 255:
        raise ValueError("exit_code must be an integer in 0..255")
    digest = _bounded_text(value.get("output_digest"), "output_digest")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError("output_digest must be a sha256 hex digest")
    summary = _bounded_text(value.get("output_summary", ""), "output_summary", allow_empty=True)
    return VerificationEvidence(
        command=command,
        working_dir=working_dir,
        started_at_ms=started,
        finished_at_ms=finished,
        exit_code=exit_code,
        output_digest=digest,
        output_summary=summary,
        truncated=bool(value.get("truncated", False)),
    )


# ---------- ReviewReport ----------


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """Review 双轴 finding；severity 决定是否阻断完成。"""

    axis: ReviewAxis
    severity: FindingSeverity
    message: str
    location: str = ""


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """两轴 Reviewer 合并后的唯一 Review 事实。"""

    requirement_verdict: str
    code_verdict: str
    findings: tuple[ReviewFinding, ...] = ()


def validate_review_report(value: Mapping[str, object]) -> ReviewReport:
    """校验 Review payload；verdict 与 severity 使用白名单枚举。"""
    requirement = _bounded_text(value.get("requirement_verdict"), "requirement_verdict")
    code = _bounded_text(value.get("code_verdict"), "code_verdict")
    if requirement not in {"pass", "fail"} or code not in {"pass", "fail"}:
        raise ValueError("verdict must be pass or fail")
    raw_findings = value.get("findings", [])
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
        raise ValueError("findings must be a bounded list")
    findings: list[ReviewFinding] = []
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            raise ValueError("finding must be an object")
        axis = _bounded_text(raw.get("axis"), "finding.axis")
        severity = _bounded_text(raw.get("severity"), "finding.severity")
        if axis not in ReviewAxis._value2member_map_ or severity not in FindingSeverity._value2member_map_:
            raise ValueError("finding axis/severity must be known")
        findings.append(
            ReviewFinding(
                axis=ReviewAxis(axis),
                severity=FindingSeverity(severity),
                message=_bounded_text(raw.get("message"), "finding.message"),
                location=_bounded_text(raw.get("location", ""), "finding.location", allow_empty=True),
            )
        )
    return ReviewReport(
        requirement_verdict=requirement,
        code_verdict=code,
        findings=tuple(findings),
    )


# ---------- ComposeArtifact 信封 ----------


@dataclass(frozen=True, slots=True)
class ComposeArtifact:
    """Compose artifact 信封：身份、来源 execution 与内容摘要。"""

    artifact_id: str
    kind: ArtifactKind
    version: int
    run_id: str
    source_execution_id: str
    created_at_ms: int
    payload: Mapping[str, object]
    content_digest: str


def make_artifact(
    kind: ArtifactKind,
    *,
    run_id: str,
    source_execution_id: str,
    created_at_ms: int,
    payload: Mapping[str, object],
) -> ComposeArtifact:
    """构造带内容摘要的 ComposeArtifact；payload 超出有界上限被拒绝。"""
    if not run_id or not source_execution_id:
        raise ComposeStoreError("COMPOSE_ARTIFACT_IDENTITY_INVALID")
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_PAYLOAD_BYTES:
        raise ComposeStoreError(
            "COMPOSE_ARTIFACT_PAYLOAD_TOO_LARGE",
            f"payload exceeds {MAX_ARTIFACT_PAYLOAD_BYTES} bytes",
        )
    artifact_id = hashlib.sha256(
        f"{run_id}:{kind.value}:{created_at_ms}:{hashlib.sha256(encoded).hexdigest()}".encode("utf-8")
    ).hexdigest()[:16]
    return ComposeArtifact(
        artifact_id=artifact_id,
        kind=kind,
        version=1,
        run_id=run_id,
        source_execution_id=source_execution_id,
        created_at_ms=created_at_ms,
        payload=dict(payload),
        content_digest=hashlib.sha256(encoded).hexdigest(),
    )
