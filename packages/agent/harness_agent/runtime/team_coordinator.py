"""受控 Agent Team：固定 DAG、并行上限、取消传播与可恢复终态。

TeamCoordinator 只拥有任务编排状态，不拥有 AgentEngine、Prompt、消息或
checkpoint。每个成员任务都通过 AgentDelegator 进入统一的子 execution 链路。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

import aiosqlite

from harness_agent.runtime.agent_catalog import AgentDefinition, DelegationPolicy
from harness_agent.runtime.agent_delegation import (
    AgentDelegationError,
    AgentDelegator,
    DelegateAgent,
)
from harness_agent.runtime.execution_binding import ExecutionRef
from harness_agent.runtime.run_context import RunCancellationToken


class TeamError(RuntimeError):
    """Team 定义、状态或执行无法安全继续时抛出的稳定错误。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: object | None = None,
    ) -> None:
        """保留稳定错误码和可选脱敏详情，避免携带成员 Prompt 或路径。"""
        self.code = code
        self.details = details
        super().__init__(message or code)


class TeamTaskAccess(StrEnum):
    """任务对共享工作区的预期访问方式。"""

    READ = "read"
    WRITE = "write"


class TeamFailurePolicy(StrEnum):
    """成员失败后的固定调度策略。"""

    FAIL_FAST = "fail-fast"
    CONTINUE = "continue"
    CONTINUE_TO_SYNTHESIS = "continue-to-synthesis"


class TeamTaskStatus(StrEnum):
    """一个 TeamTask 的可恢复生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"

    @property
    def terminal(self) -> bool:
        """返回任务是否已经不可继续执行。"""
        return self in {
            TeamTaskStatus.COMPLETED,
            TeamTaskStatus.FAILED,
            TeamTaskStatus.CANCELLED,
            TeamTaskStatus.BLOCKED,
        }


class TeamRunStatus(StrEnum):
    """TeamRun 的唯一终态集合。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """返回 Team 是否已经产生最终结果。"""
        return self is not TeamRunStatus.RUNNING


@dataclass(frozen=True, slots=True)
class TeamTaskDefinition:
    """固定 DAG 中的一项任务，只引用 Agent ID 和结构化输入模板。"""

    task_id: str
    agent_id: str
    input_template: str
    depends_on: tuple[str, ...] = ()
    access: TeamTaskAccess = TeamTaskAccess.READ
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        """拒绝空 ID、空输入、自依赖和无效超时。"""
        if (
            not self.task_id
            or not self.agent_id
            or not self.input_template.strip()
            or self.task_id in self.depends_on
            or self.timeout_seconds <= 0
        ):
            raise TeamError("TEAM_TASK_INVALID")
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True, slots=True)
class TeamDefinition:
    """一个可安装、可预览的固定任务 DAG。"""

    team_id: str
    tasks: tuple[TeamTaskDefinition, ...]
    max_parallelism: int = 4
    failure_policy: TeamFailurePolicy = TeamFailurePolicy.FAIL_FAST
    description: str | None = None

    def __post_init__(self) -> None:
        """在受理前验证引用与有向无环性。"""
        if not self.team_id or not self.tasks or self.max_parallelism <= 0:
            raise TeamError("TEAM_DEFINITION_INVALID")
        object.__setattr__(self, "tasks", tuple(self.tasks))
        task_ids = {task.task_id for task in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise TeamError("TEAM_TASK_DUPLICATE")
        for task in self.tasks:
            if any(dependency not in task_ids for dependency in task.depends_on):
                raise TeamError("TEAM_DEPENDENCY_NOT_FOUND")
        _validate_acyclic(self.tasks)


@dataclass(frozen=True, slots=True)
class TeamTaskState:
    """可持久化的成员任务事实，不复制消息、Prompt 或 checkpoint。"""

    task_id: str
    status: TeamTaskStatus = TeamTaskStatus.PENDING
    execution_id: str | None = None
    result: Mapping[str, object] = field(default_factory=dict)
    error_code: str | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        """冻结结果映射，保证保存后不会被 runner 原地修改。"""
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))


@dataclass(frozen=True, slots=True)
class TeamRun:
    """一次 Team 执行的唯一可恢复状态。"""

    run_id: str
    team_id: str
    parent_ref: ExecutionRef
    status: TeamRunStatus
    tasks: tuple[TeamTaskState, ...]
    terminal_count: int = 0

    def __post_init__(self) -> None:
        """确保状态中每个任务只出现一次。"""
        if not self.run_id or not self.team_id:
            raise TeamError("TEAM_RUN_INVALID")
        object.__setattr__(self, "tasks", tuple(self.tasks))
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise TeamError("TEAM_RUN_TASK_DUPLICATE")
        if self.status.terminal and self.terminal_count != 1:
            raise TeamError("TEAM_TERMINAL_COUNT_INVALID")
        if not self.status.terminal and self.terminal_count != 0:
            raise TeamError("TEAM_TERMINAL_COUNT_INVALID")

    def task(self, task_id: str) -> TeamTaskState:
        """读取一个任务状态，未知 ID fail closed。"""
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise TeamError("TEAM_TASK_STATE_NOT_FOUND")


class TeamStateStore(Protocol):
    """Team 状态持久化 seam；实现可以使用 SQLite 或测试内存。"""

    async def load(self, run_id: str) -> TeamRun | None:
        """读取一次 TeamRun。"""

    async def save(self, run: TeamRun) -> None:
        """原子保存一次 TeamRun 快照。"""


class InMemoryTeamStateStore:
    """测试和嵌入式调用使用的进程内 Team 状态存储。"""

    def __init__(self) -> None:
        """初始化空快照表。"""
        self._runs: dict[str, TeamRun] = {}
        self._lock = asyncio.Lock()

    async def load(self, run_id: str) -> TeamRun | None:
        """读取已保存快照。"""
        async with self._lock:
            return self._runs.get(run_id)

    async def save(self, run: TeamRun) -> None:
        """覆盖同一 run 的最新快照。"""
        async with self._lock:
            self._runs[run.run_id] = run


class SqliteTeamStateStore:
    """使用项目指纹隔离的 SQLite TeamRun 存储，可跨进程恢复。"""

    def __init__(
        self,
        connection: aiosqlite.Connection,
        *,
        project_fingerprint: str,
        lock: asyncio.Lock | None = None,
    ) -> None:
        """借用调用方连接；store 不拥有也不关闭 ThreadPersistence 资源。"""
        if not project_fingerprint:
            raise TeamError("TEAM_PROJECT_FINGERPRINT_INVALID")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock or asyncio.Lock()
        self._ready = False

    async def setup(self) -> None:
        """幂等创建 Team 表；不修改现有 Thread schema 版本。"""
        async with self._lock:
            if self._ready:
                return
            await self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_team_runs (
                    project_fingerprint TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL,
                    parent_execution_id TEXT NOT NULL,
                    parent_parent_execution_id TEXT,
                    status TEXT NOT NULL,
                    tasks_json TEXT NOT NULL,
                    terminal_count INTEGER NOT NULL,
                    PRIMARY KEY (project_fingerprint, run_id)
                )
                """
            )
            await self._connection.commit()
            self._ready = True

    async def load(self, run_id: str) -> TeamRun | None:
        """从 SQLite 恢复一个 TeamRun，不读取成员消息或 checkpoint。"""
        await self.setup()
        async with self._lock, self._connection.execute(
            """
            SELECT team_id, thread_id, parent_run_id, parent_execution_id,
                   parent_parent_execution_id, status, tasks_json, terminal_count
            FROM harness_team_runs
            WHERE project_fingerprint = ? AND run_id = ?
            """,
            (self._project_fingerprint, run_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            raw_tasks = json.loads(str(row[6]))
            if not isinstance(raw_tasks, list):
                raise ValueError
            tasks = tuple(_task_state_from_record(item) for item in raw_tasks)
            return TeamRun(
                run_id=run_id,
                team_id=str(row[0]),
                parent_ref=ExecutionRef(
                    thread_id=str(row[1]),
                    run_id=str(row[2]),
                    execution_id=str(row[3]),
                    parent_execution_id=(
                        str(row[4]) if row[4] is not None else None
                    ),
                ),
                status=TeamRunStatus(str(row[5])),
                tasks=tasks,
                terminal_count=int(row[7]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise TeamError("TEAM_PERSISTED_STATE_INVALID") from exc

    async def save(self, run: TeamRun) -> None:
        """原子 upsert TeamRun；终态之后只允许写入完全相同的终态。"""
        await self.setup()
        tasks_json = json.dumps(
            [_task_state_record(task) for task in run.tasks],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self._lock:
            async with self._connection.execute(
                """
                SELECT status, terminal_count, tasks_json
                FROM harness_team_runs
                WHERE project_fingerprint = ? AND run_id = ?
                """,
                (self._project_fingerprint, run.run_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None and TeamRunStatus(str(existing[0])).terminal:
                if (
                    str(existing[0]) != run.status
                    or int(existing[1]) != run.terminal_count
                    or str(existing[2]) != tasks_json
                ):
                    raise TeamError("TEAM_TERMINAL_ALREADY_PUBLISHED")
                return
            await self._connection.execute(
                """
                INSERT INTO harness_team_runs (
                    project_fingerprint, run_id, team_id, thread_id,
                    parent_run_id, parent_execution_id,
                    parent_parent_execution_id, status, tasks_json, terminal_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_fingerprint, run_id) DO UPDATE SET
                    team_id = excluded.team_id,
                    thread_id = excluded.thread_id,
                    parent_run_id = excluded.parent_run_id,
                    parent_execution_id = excluded.parent_execution_id,
                    parent_parent_execution_id = excluded.parent_parent_execution_id,
                    status = excluded.status,
                    tasks_json = excluded.tasks_json,
                    terminal_count = excluded.terminal_count
                """,
                (
                    self._project_fingerprint,
                    run.run_id,
                    run.team_id,
                    run.parent_ref.thread_id,
                    run.parent_ref.run_id,
                    run.parent_ref.execution_id,
                    run.parent_ref.parent_execution_id,
                    str(run.status),
                    tasks_json,
                    run.terminal_count,
                ),
            )
            await self._connection.commit()


class TeamCoordinator:
    """通过 AgentDelegator 执行固定 DAG，并且只拥有 Team 状态。"""

    def __init__(
        self,
        delegator: AgentDelegator,
        *,
        store: TeamStateStore,
    ) -> None:
        """绑定统一委派入口和状态存储，不接收 Engine 或图。"""
        self._delegator = delegator
        self._store = store
        self._state_lock = asyncio.Lock()
        # 写任务在 Team 层互斥；成员的真实工具调用仍必须经过 Host 共享读写锁。
        self._write_task_lock = asyncio.Lock()

    async def run(
        self,
        definition: TeamDefinition,
        *,
        run_id: str,
        parent_ref: ExecutionRef,
        request: str,
        delegation_policy: DelegationPolicy,
        cancellation_token: RunCancellationToken,
    ) -> TeamRun:
        """恢复或创建 TeamRun，按 DAG 调度，最终只写入一个 Team 终态。"""
        if not run_id or not request.strip():
            raise TeamError("TEAM_RUN_INPUT_INVALID")
        state = await self._load_or_create(definition, run_id, parent_ref)
        if state.status.terminal:
            return state
        states = {task.task_id: task for task in state.tasks}
        definitions = {task.task_id: task for task in definition.tasks}
        running: dict[str, asyncio.Task[TeamTaskState]] = {}
        semaphore = asyncio.Semaphore(
            min(definition.max_parallelism, delegation_policy.max_parallelism or definition.max_parallelism)
        )
        cancelled = False

        try:
            while True:
                if cancellation_token.cancelled:
                    cancelled = True
                    break
                self._block_unreachable(definition, states)
                ready = [
                    task
                    for task in definition.tasks
                    if states[task.task_id].status is TeamTaskStatus.PENDING
                    and task.task_id not in running
                    and self._dependencies_ready(task, states, definition.failure_policy)
                ]
                for task in ready:
                    running[task.task_id] = asyncio.create_task(
                        self._execute_task(
                            definition,
                            task,
                            states,
                            parent_ref=parent_ref,
                            request=request,
                            delegation_policy=delegation_policy,
                            cancellation_token=cancellation_token,
                            semaphore=semaphore,
                            team_run_id=run_id,
                        ),
                        name=f"harness-team-{definition.team_id}-{task.task_id}",
                    )
                if not running:
                    break
                done, _ = await asyncio.wait(
                    set(running.values()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    task_id = next(key for key, value in running.items() if value is completed)
                    running.pop(task_id)
                    try:
                        states[task_id] = await completed
                    except asyncio.CancelledError:
                        states[task_id] = replace(
                            states[task_id],
                            status=TeamTaskStatus.CANCELLED,
                            error_code="TEAM_CANCELLED",
                        )
                    await self._save_running(state, states)
                    if (
                        states[task_id].status is TeamTaskStatus.FAILED
                        and definition.failure_policy is TeamFailurePolicy.FAIL_FAST
                    ):
                        cancelled = True
                        break
                if cancelled:
                    break
        finally:
            if running:
                for task in running.values():
                    task.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                for task_id in running:
                    if not states[task_id].status.terminal:
                        states[task_id] = replace(
                            states[task_id],
                            status=TeamTaskStatus.CANCELLED,
                            error_code="TEAM_CANCELLED",
                        )

        if cancelled or cancellation_token.cancelled:
            for task_id, task_state in tuple(states.items()):
                if not task_state.status.terminal:
                    states[task_id] = replace(
                        task_state,
                        status=TeamTaskStatus.CANCELLED,
                        error_code="TEAM_CANCELLED",
                    )
            status = (
                TeamRunStatus.CANCELLED
                if cancellation_token.cancelled
                else TeamRunStatus.FAILED
            )
        elif all(task.status is TeamTaskStatus.COMPLETED for task in states.values()):
            status = TeamRunStatus.COMPLETED
        else:
            status = TeamRunStatus.FAILED
        return await self._finalize(state, states, status)

    async def _load_or_create(
        self,
        definition: TeamDefinition,
        run_id: str,
        parent_ref: ExecutionRef,
    ) -> TeamRun:
        """恢复任务事实；未确认的只读任务可重试，写任务 fail closed。"""
        current = await self._store.load(run_id)
        if current is None:
            current = TeamRun(
                run_id=run_id,
                team_id=definition.team_id,
                parent_ref=parent_ref,
                status=TeamRunStatus.RUNNING,
                tasks=tuple(TeamTaskState(task.task_id) for task in definition.tasks),
            )
            await self._store.save(current)
            return current
        if current.team_id != definition.team_id or current.parent_ref != parent_ref:
            raise TeamError("TEAM_RUN_IDENTITY_CONFLICT")
        known = {task.task_id for task in definition.tasks}
        if {task.task_id for task in current.tasks} != known:
            raise TeamError("TEAM_DEFINITION_CHANGED")
        definitions = {task.task_id: task for task in definition.tasks}
        recovered: list[TeamTaskState] = []
        changed = False
        for task in current.tasks:
            if task.status is not TeamTaskStatus.RUNNING:
                recovered.append(task)
            elif definitions[task.task_id].access is TeamTaskAccess.READ:
                recovered.append(replace(task, status=TeamTaskStatus.PENDING))
                changed = True
            else:
                recovered.append(
                    replace(
                        task,
                        status=TeamTaskStatus.FAILED,
                        error_code="TEAM_WRITE_OUTCOME_UNKNOWN",
                    )
                )
                changed = True
        if changed:
            current = replace(current, tasks=tuple(recovered))
            await self._store.save(current)
        return current

    async def _execute_task(
        self,
        team: TeamDefinition,
        task: TeamTaskDefinition,
        states: Mapping[str, TeamTaskState],
        *,
        parent_ref: ExecutionRef,
        request: str,
        delegation_policy: DelegationPolicy,
        cancellation_token: RunCancellationToken,
        semaphore: asyncio.Semaphore,
        team_run_id: str,
    ) -> TeamTaskState:
        """把一个 TeamTask 转为 DelegateAgent，写任务增加 Team 级互斥。"""
        current = states[task.task_id]
        running = replace(
            current,
            status=TeamTaskStatus.RUNNING,
            attempts=current.attempts + 1,
            error_code=None,
        )
        mutable_states = states if isinstance(states, dict) else None
        if mutable_states is not None:
            mutable_states[task.task_id] = running
        await self._save_task_state(team, team_run_id, mutable_states or states)
        prompt = _render_input(task.input_template, request=request, states=states)

        async def invoke() -> TeamTaskState:
            try:
                result = await self._delegator.execute(
                    DelegateAgent(
                        parent_ref=parent_ref,
                        target_agent_id=task.agent_id,
                        task=prompt,
                        idempotency_key=f"team:{team.team_id}:{team_run_id}:{task.task_id}",
                        delegation_policy=delegation_policy,
                        cancellation_token=cancellation_token,
                        timeout_seconds=task.timeout_seconds,
                    )
                )
                output = _structured_result(result.output)
                return replace(
                    running,
                    status=TeamTaskStatus.COMPLETED,
                    execution_id=result.ref.execution_id,
                    result=output,
                )
            except asyncio.CancelledError:
                raise
            except AgentDelegationError as exc:
                return replace(
                    running,
                    status=TeamTaskStatus.FAILED,
                    error_code=exc.code,
                )
            except TeamError as exc:
                return replace(
                    running,
                    status=TeamTaskStatus.FAILED,
                    error_code=exc.code,
                )

        async with semaphore:
            if task.access is TeamTaskAccess.WRITE:
                async with self._write_task_lock:
                    return await invoke()
            return await invoke()

    async def _save_task_state(
        self,
        definition: TeamDefinition,
        run_id: str,
        states: Mapping[str, TeamTaskState],
    ) -> None:
        """保存执行中的任务引用，不保存成员上下文。"""
        existing = await self._store.load(run_id)
        if existing is None or existing.team_id != definition.team_id:
            return
        await self._store.save(
            replace(
                existing,
                tasks=tuple(states[task.task_id] for task in definition.tasks),
            )
        )

    async def _save_running(
        self,
        state: TeamRun,
        states: Mapping[str, TeamTaskState],
    ) -> None:
        """保存 RUNNING 快照，终态只能由 _finalize 写入。"""
        await self._store.save(
            replace(
                state,
                status=TeamRunStatus.RUNNING,
                terminal_count=0,
                tasks=tuple(states[task.task_id] for task in state.tasks),
            )
        )

    async def _finalize(
        self,
        state: TeamRun,
        states: Mapping[str, TeamTaskState],
        status: TeamRunStatus,
    ) -> TeamRun:
        """在锁内发布一次唯一 Team 终态，重复恢复直接返回同一事实。"""
        async with self._state_lock:
            current = await self._store.load(state.run_id)
            if current is not None and current.status.terminal:
                return current
            terminal = replace(
                state,
                status=status,
                terminal_count=1,
                tasks=tuple(states[task.task_id] for task in state.tasks),
            )
            await self._store.save(terminal)
            return terminal

    @staticmethod
    def _dependencies_ready(
        task: TeamTaskDefinition,
        states: Mapping[str, TeamTaskState],
        failure_policy: TeamFailurePolicy,
    ) -> bool:
        """判断依赖是否允许解锁；synthesis 模式允许消费失败摘要。"""
        dependencies = [states[dependency] for dependency in task.depends_on]
        if not all(state.status.terminal for state in dependencies):
            return False
        if failure_policy is TeamFailurePolicy.CONTINUE_TO_SYNTHESIS:
            return True
        return all(state.status is TeamTaskStatus.COMPLETED for state in dependencies)

    @staticmethod
    def _block_unreachable(
        definition: TeamDefinition,
        states: dict[str, TeamTaskState],
    ) -> None:
        """普通 continue 下将失败依赖的后继标成 BLOCKED，避免调度空转。"""
        if definition.failure_policy is TeamFailurePolicy.CONTINUE_TO_SYNTHESIS:
            return
        for task in definition.tasks:
            state = states[task.task_id]
            if state.status is not TeamTaskStatus.PENDING:
                continue
            dependencies = [states[dependency] for dependency in task.depends_on]
            if dependencies and all(item.status.terminal for item in dependencies) and any(
                item.status is not TeamTaskStatus.COMPLETED for item in dependencies
            ):
                states[task.task_id] = replace(
                    state,
                    status=TeamTaskStatus.BLOCKED,
                    error_code="TEAM_DEPENDENCY_FAILED",
                )


def generate_fanout_team(
    *,
    team_id: str,
    agents: tuple[AgentDefinition, ...],
    lead_agent_id: str,
    worker_agent_ids: tuple[str, ...],
    max_parallelism: int = 4,
) -> TeamDefinition:
    """根据受信 AgentDefinition 生成“并行工作者 → lead 汇总”的固定 DAG 预览。"""
    definitions = {agent.agent_id: agent for agent in agents}
    requested = (*worker_agent_ids, lead_agent_id)
    if (
        not worker_agent_ids
        or lead_agent_id in worker_agent_ids
        or len(set(worker_agent_ids)) != len(worker_agent_ids)
        or any(agent_id not in definitions for agent_id in requested)
    ):
        raise TeamError("TEAM_GENERATION_AGENT_INVALID")
    worker_tasks = tuple(
        TeamTaskDefinition(
            task_id=agent_id,
            agent_id=agent_id,
            input_template="{{request}}",
            access=TeamTaskAccess.READ,
        )
        for agent_id in worker_agent_ids
    )
    result_sections = "\n\n".join(
        f"{definitions[agent_id].description or agent_id}:\n"
        f"{{{{tasks.{agent_id}.result}}}}"
        for agent_id in worker_agent_ids
    )
    synthesis = TeamTaskDefinition(
        task_id="synthesis",
        agent_id=lead_agent_id,
        input_template=f"用户请求：\n{{{{request}}}}\n\n{result_sections}",
        depends_on=worker_agent_ids,
        access=TeamTaskAccess.READ,
    )
    return TeamDefinition(
        team_id=team_id,
        description=f"由 {len(worker_agent_ids)} 个 worker 和 {lead_agent_id} 生成的 Team",
        tasks=(*worker_tasks, synthesis),
        max_parallelism=max_parallelism,
        failure_policy=TeamFailurePolicy.CONTINUE_TO_SYNTHESIS,
    )


def _validate_acyclic(tasks: tuple[TeamTaskDefinition, ...]) -> None:
    """使用 Kahn 算法拒绝循环依赖。"""
    indegree = {task.task_id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = {task.task_id: [] for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.task_id)
    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        task_id = ready.pop()
        visited += 1
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if visited != len(tasks):
        raise TeamError("TEAM_DEPENDENCY_CYCLE")


def _render_input(
    template: str,
    *,
    request: str,
    states: Mapping[str, TeamTaskState],
) -> str:
    """只展开固定 request/task result 占位符，不解释任意模板表达式。"""
    rendered = template.replace("{{request}}", request)
    for task_id, state in states.items():
        marker = f"{{{{tasks.{task_id}.result}}}}"
        value = dict(state.result)
        if state.error_code is not None:
            value = {"error_code": state.error_code}
        rendered = rendered.replace(marker, str(value))
    return rendered


def _task_state_record(task: TeamTaskState) -> dict[str, object]:
    """生成不含消息、Prompt 和 checkpoint 的 SQLite JSON 记录。"""
    return {
        "task_id": task.task_id,
        "status": str(task.status),
        "execution_id": task.execution_id,
        "result": dict(task.result),
        "error_code": task.error_code,
        "attempts": task.attempts,
    }


def _structured_result(value: Mapping[str, object]) -> dict[str, object]:
    """只允许有界 JSON 结果进入 Team 状态，成员消息对象留在 execution。"""
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise TeamError("TEAM_RESULT_TOO_LARGE")
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise TeamError("TEAM_RESULT_NOT_STRUCTURED") from exc
    if not isinstance(decoded, dict):
        raise TeamError("TEAM_RESULT_NOT_STRUCTURED")
    return decoded


def _task_state_from_record(value: object) -> TeamTaskState:
    """严格恢复 TeamTaskState，拒绝额外字段和非 JSON object 结果。"""
    if not isinstance(value, Mapping) or set(value) != {
        "task_id",
        "status",
        "execution_id",
        "result",
        "error_code",
        "attempts",
    }:
        raise TeamError("TEAM_PERSISTED_TASK_INVALID")
    result = value.get("result")
    if not isinstance(result, Mapping):
        raise TeamError("TEAM_PERSISTED_TASK_INVALID")
    task_id = value.get("task_id")
    execution_id = value.get("execution_id")
    error_code = value.get("error_code")
    attempts = value.get("attempts")
    if (
        not isinstance(task_id, str)
        or (execution_id is not None and not isinstance(execution_id, str))
        or (error_code is not None and not isinstance(error_code, str))
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
    ):
        raise TeamError("TEAM_PERSISTED_TASK_INVALID")
    return TeamTaskState(
        task_id=task_id,
        status=TeamTaskStatus(str(value.get("status"))),
        execution_id=execution_id,
        result=dict(result),
        error_code=error_code,
        attempts=attempts,
    )
