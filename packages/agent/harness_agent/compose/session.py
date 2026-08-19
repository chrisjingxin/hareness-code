"""ComposeSession：文档 + 确认驱动的薄流程；阶段不由模型或分类器决定。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from harness_agent.compose.document_paths import (
    DEFAULT_COMPOSE_DOCS_DIR,
    make_compose_slug,
)
from harness_agent.compose.models import ThreadMode
from harness_agent.threads.compose_progress_store import (
    ComposeProgressStore,
    ComposeProgressStoreError,
    ComposeSessionRecord,
)

COMPOSE_SYSTEM_PROMPT = """你在 Compose 工作模式。当前阶段由系统决定，不要自己跳阶段。
各阶段产出物（写到 docs/compose/<slug>/）：
- 需求：task.md
- 规格：spec.md
- 计划：plan.md 与 todo.md
需要向用户提问时必须调用 ask_user，不要只用正文提问后结束本轮。
提问指引：
- 快速通道：对于边界明确、无设计分叉的 Bug 修复或小型改动，不要强行发起多轮提问，首轮直接生成清晰简要的 task.md 并等待确认。
- 提问收敛：只问会改变实现的关键分叉；优先提供 2-4 个带推荐选项（标注 (Recommended)）的单选题及简明利弊，避免抛出宽泛抽象的大问题。
- 相关决策可以一次问几题，最多 5 题；不要一次问十几题。不相关的问题下次再问。
- 简单需求少问，复杂需求问到关键决策清楚；够写当前产出物就停止提问并落盘。
写完产出物后停止。系统会用 ask_user 问用户这份产出是否符合预期；用户确认后由系统进入下一阶段，不要自己宣布或跳阶段。
用户若说要改，按修改点改同一份产出物，不要进入下一阶段。
如果用户说确认、推进、进入下一阶段、下一步，立刻停止 ask_user，不要再问。
先读仓库里能查到的事实；外部 MCP 工具未出现在工具列表时，用 tool_search 按需搜索。
"""

_PROCEED_PHRASES = frozenset(
    {
        "推进",
        "推进到下个阶段",
        "推进到下一阶段",
        "进入下一阶段",
        "进入下个阶段",
        "下一阶段",
        "下个阶段",
        "下一步",
        "确认",
        "确认并进入下一阶段",
        "确认，进入下一阶段",
        "可以进入下一阶段",
        "可以推进",
        "开始下一阶段",
        "继续下一阶段",
        "proceed",
        "next stage",
        "go next",
    }
)


def build_implement_prompt(workspace: Path, slug: str) -> str:
    """拼实现阶段输入：只带已确认文档，不含 Grill 对话。"""
    docs = Path(workspace) / DEFAULT_COMPOSE_DOCS_DIR / slug
    sections = [
        "你在 Compose 实现阶段。这是一次独立执行：只根据下列已确认文档改代码。",
        "不要重开需求访谈，不要自己跳阶段。",
        "按 TDD：先写失败测试，再写最小实现，再重构。提问必须调用 ask_user。",
        f"套件目录：{DEFAULT_COMPOSE_DOCS_DIR}/{slug}/",
    ]
    for name in ("task.md", "spec.md", "plan.md", "todo.md", "review.md", "verify-failure.log"):
        path = docs / name
        if path.is_file() and path.stat().st_size > 0:
            body = path.read_text(encoding="utf-8")
            sections.append(f"## {name}\n\n{body}")
    return "\n\n".join(sections)


def build_review_prompt(workspace: Path, slug: str) -> str:
    """拼检视阶段输入：已确认文档 + 本套计划点名的改动，不含 Grill 对话。"""
    docs = Path(workspace) / DEFAULT_COMPOSE_DOCS_DIR / slug
    skill = _load_review_skill_text()
    sections = [
        "你在 Compose 检视阶段。这是一次独立执行。",
        "只检视本套 Compose 产生的代码，对照已确认文档。",
        f"把结论写入 {DEFAULT_COMPOSE_DOCS_DIR}/{slug}/review.md。",
        "不要修改产品代码，不要写 review.md 以外的文件，不要宣布进入其他阶段。",
        "提问必须调用 ask_user。写完 review.md 后停止。",
        f"套件目录：{DEFAULT_COMPOSE_DOCS_DIR}/{slug}/",
    ]
    if skill:
        sections.append(f"## 检视方法（内置 code-review-and-quality）\n\n{skill}")
    for name in ("task.md", "spec.md", "plan.md", "todo.md"):
        path = docs / name
        if path.is_file() and path.stat().st_size > 0:
            body = path.read_text(encoding="utf-8")
            sections.append(f"## {name}\n\n{body}")
    return "\n\n".join(sections)


def parse_compose_slash(message: str) -> tuple[str, str] | None:
    """解析 /abandon 与 /new-work；其余消息返回 None。"""
    text = message.strip()
    if text == "/abandon" or text.startswith("/abandon ") or text.startswith("/abandon\t"):
        return "abandon", text[len("/abandon") :].strip()
    if text == "/new-work" or text.startswith("/new-work ") or text.startswith("/new-work\t"):
        return "new-work", text[len("/new-work") :].strip()
    return None


def is_proceed_message(message: str) -> bool:
    """用户是否明确要求结束当前阶段、进入下一阶段。"""
    normalized = " ".join(message.strip().lower().split())
    if not normalized:
        return False
    if normalized in _PROCEED_PHRASES:
        return True
    compact = normalized.replace("，", "").replace(",", "").replace(" ", "")
    return any(
        phrase.replace("，", "").replace(",", "").replace(" ", "") in compact
        for phrase in (
            "推进到下个阶段",
            "推进到下一阶段",
            "进入下一阶段",
            "进入下个阶段",
            "确认进入下一阶段",
            "可以进入下一阶段",
        )
    )


class ComposeSessionError(RuntimeError):
    """Session 层稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ComposeTurnOutcome(str, Enum):
    """一次 Turn 的收敛。"""

    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ComposeTurnRequest:
    """一次用户 Turn。"""

    thread_id: str
    run_id: str
    message: str
    cancelled: bool = False
    explicit_command: str | None = None


@dataclass(frozen=True, slots=True)
class ComposeTurnResult:
    """Turn 结果；progress 为协议 compose.progress 形状。"""

    progress: dict[str, object] | None
    status: ComposeTurnOutcome


@dataclass(frozen=True, slots=True)
class ComposeSessionPorts:
    """Session 依赖；classifier 若提供也不得被调用。"""

    store: ComposeProgressStore
    workspace: Path
    run_grill: Callable[[ComposeTurnRequest, str], Awaitable[None]]
    run_spec: Callable[[ComposeTurnRequest, str], Awaitable[None]] | None = None
    run_plan: Callable[[ComposeTurnRequest, str], Awaitable[None]] | None = None
    run_implement: Callable[[ComposeTurnRequest, str], Awaitable[None]] | None = None
    run_review: Callable[[ComposeTurnRequest, str], Awaitable[None]] | None = None
    classifier: object | None = None
    on_progress: Callable[[dict[str, object]], None] | None = None
    request_stage_confirm: Callable[[ComposeSessionRecord, str], Awaitable[bool]] | None = None


class ComposeSession:
    """跨 Run 的 Compose 进度 owner；不拥有 SQLite 或 graph。"""

    def __init__(self, ports: ComposeSessionPorts) -> None:
        self._ports = ports

    async def execute_turn(self, request: ComposeTurnRequest) -> ComposeTurnResult:
        """受理 Turn：无分类器；首条消息进入 Grill。"""
        try:
            mode = await self._ports.store.load_thread_mode(request.thread_id)
        except ComposeProgressStoreError as exc:
            raise ComposeSessionError(exc.code) from exc
        if mode is None:
            raise ComposeSessionError(
                "COMPOSE_THREAD_MODE_MISSING",
                "Thread 尚未受理有效 Run",
            )
        if mode is not ThreadMode.COMPOSE:
            raise ComposeSessionError(
                "THREAD_MODE_LOCKED",
                "Thread 已锁定为 Build 模式",
            )
        if request.cancelled:
            record = await self._ports.store.load(request.thread_id)
            return ComposeTurnResult(
                _progress_wire(record, self._ports.workspace) if record is not None else None,
                ComposeTurnOutcome.WAITING_USER,
            )
        record = await self._ports.store.load(request.thread_id)
        command_name = request.explicit_command
        command_arg = request.message.strip()
        parsed = parse_compose_slash(request.message)
        if parsed is not None:
            command_name, command_arg = parsed
        if command_name == "abandon":
            if command_arg:
                raise ComposeSessionError(
                    "COMPOSE_ABANDON_TAKES_NO_GOAL",
                    "换题请用 /new-work",
                )
            progress = await self.abandon(thread_id=request.thread_id)
            return ComposeTurnResult(progress, ComposeTurnOutcome.WAITING_USER)
        if command_name == "new-work":
            if not command_arg:
                raise ComposeSessionError(
                    "COMPOSE_NEW_WORK_GOAL_REQUIRED",
                    "开新需求请带目标；只要停用 /abandon",
                )
            if record is not None:
                await self.abandon(thread_id=request.thread_id)
            record = await self._create(
                ComposeTurnRequest(
                    thread_id=request.thread_id,
                    run_id=request.run_id,
                    message=command_arg,
                    cancelled=request.cancelled,
                )
            )
        if record is None:
            record = await self._create(request)
        if self._ports.classifier is not None:
            pass
        stage = _open_stage(record, self._ports.workspace)
        if is_proceed_message(request.message):
            if stage == "grill" or _is_ready_artifact(
                self._ports.workspace, record.slug, stage
            ):
                record = await self._confirm_open_stage(record, stage)
                stage = _open_stage(record, self._ports.workspace)
            if self._ports.on_progress is not None:
                self._ports.on_progress(_progress_wire(record, self._ports.workspace))
            latest = await self._produce_and_offer_confirm(stage, request, record)
            return _turn_result(latest, self._ports.workspace)
        if self._ports.on_progress is not None:
            self._ports.on_progress(_progress_wire(record, self._ports.workspace))
        latest = await self._produce_and_offer_confirm(stage, request, record)
        return _turn_result(latest, self._ports.workspace)

    async def inspect(self, *, thread_id: str) -> dict[str, object] | None:
        """只读投影。"""
        record = await self._ports.store.load(thread_id)
        return (
            _progress_wire(record, self._ports.workspace) if record is not None else None
        )

    async def abandon(self, *, thread_id: str, reason: str | None = None) -> dict[str, object]:
        """停点 1 只提供 interface；完整语义在停点 4。"""
        del reason
        record = await self._ports.store.load(thread_id)
        if record is None:
            raise ComposeSessionError("COMPOSE_NOTHING_TO_ABANDON")
        abandoned = ComposeSessionRecord(
            thread_id=record.thread_id,
            slug=record.slug,
            complexity=record.complexity,
            task_confirmed_digest=record.task_confirmed_digest,
            spec_confirmed_digest=record.spec_confirmed_digest,
            plan_confirmed_digest=record.plan_confirmed_digest,
            fix_rounds=record.fix_rounds,
            status="abandoned",
            revision=record.revision + 1,
        )
        await self._ports.store.upsert(abandoned)
        return _progress_wire(abandoned, self._ports.workspace)

    async def _create(self, request: ComposeTurnRequest) -> ComposeSessionRecord:
        """按目标文本分配 slug 并写入进度。"""
        slug = make_compose_slug(request.message)
        record = ComposeSessionRecord(
            thread_id=request.thread_id,
            slug=slug,
            complexity="simple",
            task_confirmed_digest=None,
            spec_confirmed_digest=None,
            plan_confirmed_digest=None,
            fix_rounds=0,
            status="active",
            revision=0,
        )
        saved = await self._ports.store.upsert(record)
        _write_task_document(
            workspace=self._ports.workspace,
            slug=slug,
            goal=request.message.strip() or slug,
            complexity=record.complexity,
        )
        return saved

    async def _produce_and_offer_confirm(
        self,
        stage: str,
        request: ComposeTurnRequest,
        record: ComposeSessionRecord,
        *,
        depth: int = 0,
    ) -> ComposeSessionRecord:
        """跑当前阶段；产出物就绪则请用户确认，确认后同一轮进入下一阶段。"""
        if depth > 4:
            return record
        if stage in {"implement", "review"}:
            return await self._run_implement_and_review(request, record)
        await self._run_stage(stage, request, record.slug)
        latest = await self._ports.store.load(request.thread_id) or record
        if self._ports.request_stage_confirm is None:
            return latest
        if not _is_ready_artifact(self._ports.workspace, latest.slug, stage):
            return latest
        if self._ports.on_progress is not None:
            self._ports.on_progress(_progress_wire(latest, self._ports.workspace))
        confirmed = await self._ports.request_stage_confirm(
            latest, _artifact_for_stage(stage)
        )
        if not confirmed:
            return latest
        advanced = await self._confirm_open_stage(latest, stage)
        next_stage = _open_stage(advanced, self._ports.workspace)
        if next_stage == stage:
            return advanced
        if self._ports.on_progress is not None:
            self._ports.on_progress(_progress_wire(advanced, self._ports.workspace))
        return await self._produce_and_offer_confirm(
            next_stage, request, advanced, depth=depth + 1
        )

    async def _run_stage(
        self,
        stage: str,
        request: ComposeTurnRequest,
        slug: str,
    ) -> None:
        """按未完成阶段调用对应 Agent。"""
        if stage == "grill":
            await self._ports.run_grill(request, slug)
            return
        if stage == "spec" and self._ports.run_spec is not None:
            await self._ports.run_spec(request, slug)
            return
        if stage == "plan" and self._ports.run_plan is not None:
            await self._ports.run_plan(request, slug)
            return
        if stage == "implement" and self._ports.run_implement is not None:
            await self._ports.run_implement(request, slug)

    async def _run_implement_and_review(
        self,
        request: ComposeTurnRequest,
        record: ComposeSessionRecord,
    ) -> ComposeSessionRecord:
        """实现结束后直接检视；不再由 Runtime 再跑一轮测试。"""
        workspace = self._ports.workspace
        latest = record
        if latest.status == "waiting_user":
            latest = await self._ports.store.upsert(_copy_record(latest, status="active"))
        if not _has_implement_ok(workspace, latest.slug):
            if self._ports.run_implement is not None:
                await self._ports.run_implement(request, latest.slug)
            _mark_implement_ok(workspace, latest.slug)
        if self._ports.run_review is not None:
            await self._ports.run_review(request, latest.slug)
        if self._ports.request_stage_confirm is None:
            return latest
        if not _is_ready_artifact(workspace, latest.slug, "review"):
            return latest
        latest = await self._ports.store.upsert(_copy_record(latest, status="waiting_user"))
        if self._ports.on_progress is not None:
            self._ports.on_progress(_progress_wire(latest, workspace))
        confirmed = await self._ports.request_stage_confirm(latest, "review")
        if not confirmed:
            _clear_implement_ok(workspace, latest.slug)
            return await self._ports.store.upsert(_copy_record(latest, status="active"))
        return await self._confirm_open_stage(latest, "review")

    async def _confirm_open_stage(
        self,
        record: ComposeSessionRecord,
        stage: str,
    ) -> ComposeSessionRecord:
        """确认当前阶段产出物 digest，推进门禁。"""
        workspace = self._ports.workspace
        task_digest = record.task_confirmed_digest
        spec_digest = record.spec_confirmed_digest
        plan_digest = record.plan_confirmed_digest
        if stage == "grill":
            task_digest = _write_task_document(
                workspace=workspace,
                slug=record.slug,
                goal=record.slug,
                complexity=record.complexity,
            )
        elif stage == "spec":
            spec_digest = _file_digest(_artifact_path(workspace, record.slug, "spec.md"))
        elif stage == "plan":
            plan_digest = _file_digest(_artifact_path(workspace, record.slug, "plan.md"))
        elif stage == "review":
            confirmed = ComposeSessionRecord(
                thread_id=record.thread_id,
                slug=record.slug,
                complexity=record.complexity,
                task_confirmed_digest=task_digest,
                spec_confirmed_digest=spec_digest,
                plan_confirmed_digest=plan_digest,
                fix_rounds=record.fix_rounds,
                status="completed",
                revision=record.revision + 1,
            )
            return await self._ports.store.upsert(confirmed)
        confirmed = ComposeSessionRecord(
            thread_id=record.thread_id,
            slug=record.slug,
            complexity=record.complexity,
            task_confirmed_digest=task_digest,
            spec_confirmed_digest=spec_digest,
            plan_confirmed_digest=plan_digest,
            fix_rounds=record.fix_rounds,
            status="active",
            revision=record.revision + 1,
        )
        return await self._ports.store.upsert(confirmed)


def _write_task_document(
    *,
    workspace: Path,
    slug: str,
    goal: str,
    complexity: str,
) -> str:
    """落盘 task.md 并返回正文 digest。"""
    import hashlib

    docs = Path(workspace) / DEFAULT_COMPOSE_DOCS_DIR / slug
    docs.mkdir(parents=True, exist_ok=True)
    existing = docs / "task.md"
    if existing.is_file() and existing.stat().st_size > 0:
        body = existing.read_text(encoding="utf-8")
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    text = (
        f"---\nslug: {slug}\ncomplexity: {complexity}\n---\n\n"
        f"# {goal}\n\n"
        f"{goal}\n"
    )
    (docs / "task.md").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_IMPLEMENT_OK_NAME = "implement.ok"


def _artifact_path(workspace: Path, slug: str, name: str) -> Path:
    return Path(workspace) / DEFAULT_COMPOSE_DOCS_DIR / slug / name


def _implement_ok_path(workspace: Path, slug: str) -> Path:
    return _artifact_path(workspace, slug, _IMPLEMENT_OK_NAME)


def _has_implement_ok(workspace: Path, slug: str) -> bool:
    return _implement_ok_path(workspace, slug).is_file()


def _mark_implement_ok(workspace: Path, slug: str) -> None:
    path = _implement_ok_path(workspace, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


def _clear_implement_ok(workspace: Path, slug: str) -> None:
    path = _implement_ok_path(workspace, slug)
    if path.is_file():
        path.unlink()


def _load_review_skill_text() -> str:
    """读取内置 code-review-and-quality 正文；缺失时检视提示仍可用。"""
    path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "builtin"
        / "agent-skills"
        / "skills"
        / "code-review-and-quality"
        / "SKILL.md"
    )
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _file_digest(path: Path) -> str | None:
    import hashlib

    if not path.is_file() or path.stat().st_size <= 0:
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_for_stage(stage: str) -> str:
    if stage == "grill":
        return "task"
    if stage == "spec":
        return "spec"
    if stage == "plan":
        return "plan"
    if stage == "review":
        return "review"
    return "task"


def _artifact_filename(stage: str) -> str | None:
    if stage == "grill":
        return "task.md"
    if stage == "spec":
        return "spec.md"
    if stage == "plan":
        return "plan.md"
    if stage == "review":
        return "review.md"
    return None


def _is_ready_artifact(workspace: Path, slug: str, stage: str) -> bool:
    """阶段 Agent 本轮结束后，产出物文件在就可以请用户确认。"""
    name = _artifact_filename(stage)
    if name is None:
        return False
    path = _artifact_path(workspace, slug, name)
    return path.is_file() and path.stat().st_size > 0


def _open_stage(record: ComposeSessionRecord, workspace: Path) -> str:
    """未确认的第一个阶段；有产出物也仍停在该阶段直到确认。"""
    if record.status == "completed":
        return "review"
    if record.task_confirmed_digest is None:
        return "grill"
    if record.spec_confirmed_digest is None:
        return "spec"
    if record.plan_confirmed_digest is None:
        return "plan"
    if not _has_implement_ok(workspace, record.slug):
        return "implement"
    return "review"


def _waiting_for(record: ComposeSessionRecord, workspace: Path, stage: str) -> str:
    """产出物就绪则等确认；否则仍在访谈/撰写。"""
    if record.status == "completed":
        return "none"
    if stage == "implement":
        return "ask_user" if record.status == "waiting_user" else "none"
    if stage == "review":
        if _is_ready_artifact(workspace, record.slug, "review"):
            return "review_confirm"
        return "none"
    if _is_ready_artifact(workspace, record.slug, stage):
        return {
            "grill": "task_confirm",
            "spec": "spec_confirm",
            "plan": "plan_confirm",
        }.get(stage, "ask_user")
    return "ask_user"


def _progress_wire(record: ComposeSessionRecord, workspace: Path) -> dict[str, object]:
    """把记录投影为 compose.progress。"""
    stage = _open_stage(record, workspace)
    current = {
        "grill": "grill",
        "spec": "spec",
        "plan": "plan",
        "implement": "implement",
        "review": "review",
    }[stage]
    waiting = _waiting_for(record, workspace, stage)
    implement_ok = _has_implement_ok(workspace, record.slug)
    implement_failed = (
        stage == "implement"
        and record.status == "waiting_user"
        and not implement_ok
    )
    return {
        "thread_id": record.thread_id,
        "slug": record.slug,
        "complexity": record.complexity,
        "status": record.status,
        "current_stage": current,
        "waiting": waiting,
        "stages": [
            {
                "id": "requirement",
                "state": "confirmed" if record.task_confirmed_digest else "current",
            },
            {
                "id": "spec",
                "state": (
                    "confirmed"
                    if record.spec_confirmed_digest
                    else "current"
                    if stage == "spec"
                    else "pending"
                ),
            },
            {
                "id": "plan",
                "state": (
                    "confirmed"
                    if record.plan_confirmed_digest
                    else "current"
                    if stage == "plan"
                    else "pending"
                ),
            },
            {
                "id": "implement",
                "state": (
                    "confirmed"
                    if record.status == "completed" or implement_ok
                    else "failed"
                    if implement_failed
                    else "current"
                    if stage == "implement"
                    else "pending"
                ),
            },
            {
                "id": "review",
                "state": (
                    "confirmed"
                    if record.status == "completed"
                    else "current"
                    if stage == "review"
                    else "pending"
                ),
            },
        ],
        "documents": [
            {
                "kind": "task",
                "path": f"{DEFAULT_COMPOSE_DOCS_DIR}/{record.slug}/task.md",
                "confirmed": record.task_confirmed_digest is not None,
            },
            {
                "kind": "spec",
                "path": f"{DEFAULT_COMPOSE_DOCS_DIR}/{record.slug}/spec.md",
                "confirmed": record.spec_confirmed_digest is not None,
            },
            {
                "kind": "plan",
                "path": f"{DEFAULT_COMPOSE_DOCS_DIR}/{record.slug}/plan.md",
                "confirmed": record.plan_confirmed_digest is not None,
            },
            {
                "kind": "todo",
                "path": f"{DEFAULT_COMPOSE_DOCS_DIR}/{record.slug}/todo.md",
                "confirmed": record.plan_confirmed_digest is not None,
            },
            {
                "kind": "review",
                "path": f"{DEFAULT_COMPOSE_DOCS_DIR}/{record.slug}/review.md",
                "confirmed": record.status == "completed",
            },
        ],
        "fix_rounds": record.fix_rounds,
        "revision": record.revision,
    }


def _turn_result(record: ComposeSessionRecord, workspace: Path) -> ComposeTurnResult:
    """按 session 状态选择 Turn 收敛。"""
    outcome = (
        ComposeTurnOutcome.COMPLETED
        if record.status == "completed"
        else ComposeTurnOutcome.WAITING_USER
    )
    return ComposeTurnResult(_progress_wire(record, workspace), outcome)


def _copy_record(
    record: ComposeSessionRecord,
    *,
    status: str | None = None,
    fix_rounds: int | None = None,
) -> ComposeSessionRecord:
    """只改状态或自修次数，revision 递增。"""
    return ComposeSessionRecord(
        thread_id=record.thread_id,
        slug=record.slug,
        complexity=record.complexity,
        task_confirmed_digest=record.task_confirmed_digest,
        spec_confirmed_digest=record.spec_confirmed_digest,
        plan_confirmed_digest=record.plan_confirmed_digest,
        fix_rounds=record.fix_rounds if fix_rounds is None else fix_rounds,
        status=record.status if status is None else status,
        revision=record.revision + 1,
    )
