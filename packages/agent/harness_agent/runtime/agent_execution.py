"""轻量 AgentExecution registry：管理一次 Run 内的身份、状态和取消收敛。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionBindingError,
    ExecutionRef,
    ExecutionStatus,
)


class ExecutionRegistryError(RuntimeError):
    """执行树违反身份、状态或 Run 封口约束时抛出。"""


class AgentExecutionRegistry:
    """在一个 Agent Host 进程内维护 Run 的执行树。"""

    def __init__(self) -> None:
        """初始化进程级 registry；持久化由后续 Managed 阶段接入。"""
        # ponytail: 进程内 registry，重启恢复留给 Managed execution persistence 阶段。
        self._runs: dict[tuple[str, str], dict[str, AgentExecutionBinding]] = {}
        self._sealed_runs: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    async def accept(self, binding: AgentExecutionBinding) -> AgentExecutionBinding:
        """登记一个 pending execution，并校验父 execution 属于同一 Run。"""
        async with self._lock:
            run_key = _run_key(binding.ref)
            if run_key in self._sealed_runs:
                raise ExecutionRegistryError("EXECUTION_RUN_SEALED")
            executions = self._runs.setdefault(run_key, {})
            existing = executions.get(binding.ref.execution_id)
            if existing is not None:
                if existing == binding:
                    return existing
                raise ExecutionRegistryError("EXECUTION_ID_CONFLICT")
            parent_id = binding.ref.parent_execution_id
            if parent_id is not None:
                parent = executions.get(parent_id)
                if parent is None:
                    raise ExecutionRegistryError("EXECUTION_PARENT_NOT_FOUND")
                if parent.status.terminal:
                    raise ExecutionRegistryError("EXECUTION_PARENT_TERMINAL")
                if binding.depth != parent.depth + 1:
                    raise ExecutionRegistryError("EXECUTION_DEPTH_INVALID")
            executions[binding.ref.execution_id] = binding
            return binding

    async def get(self, ref: ExecutionRef) -> AgentExecutionBinding | None:
        """读取一个 execution 的当前快照。"""
        async with self._lock:
            return self._runs.get(_run_key(ref), {}).get(ref.execution_id)

    async def list(self, ref: ExecutionRef) -> tuple[AgentExecutionBinding, ...]:
        """按登记顺序返回一个 Run 的执行快照。"""
        async with self._lock:
            return tuple(self._runs.get(_run_key(ref), {}).values())

    async def start(
        self,
        ref: ExecutionRef,
        *,
        now_ms: int | None = None,
    ) -> AgentExecutionBinding:
        """将 pending execution 置为 running。"""
        async with self._lock:
            current = self._require(ref)
            timestamp = _now_ms() if now_ms is None else now_ms
            try:
                updated = current.transition(ExecutionStatus.RUNNING, now_ms=timestamp)
            except ExecutionBindingError as exc:
                raise ExecutionRegistryError(str(exc)) from exc
            self._runs[_run_key(ref)][ref.execution_id] = updated
            return updated

    async def finalize(
        self,
        ref: ExecutionRef,
        *,
        status: ExecutionStatus,
        usage: Mapping[str, int] | None = None,
        now_ms: int | None = None,
    ) -> AgentExecutionBinding:
        """将 execution 置为 completed、failed 或 cancelled。"""
        if not status.terminal:
            raise ExecutionRegistryError("EXECUTION_TERMINAL_STATUS_REQUIRED")
        async with self._lock:
            current = self._require(ref)
            if current.status is status:
                return current
            timestamp = _now_ms() if now_ms is None else now_ms
            try:
                updated = current.transition(status, now_ms=timestamp, usage=usage)
            except ExecutionBindingError as exc:
                raise ExecutionRegistryError(str(exc)) from exc
            self._runs[_run_key(ref)][ref.execution_id] = updated
            return updated

    async def cancel_run(
        self,
        ref: ExecutionRef,
        *,
        now_ms: int | None = None,
    ) -> tuple[AgentExecutionBinding, ...]:
        """取消一个 Run 中所有未终结的 execution。"""
        async with self._lock:
            executions = self._runs.get(_run_key(ref), {})
            timestamp = _now_ms() if now_ms is None else now_ms
            updated: list[AgentExecutionBinding] = []
            for execution_id, current in executions.items():
                if current.status.terminal:
                    updated.append(current)
                    continue
                try:
                    current = current.transition(ExecutionStatus.CANCELLED, now_ms=timestamp)
                except ExecutionBindingError as exc:
                    raise ExecutionRegistryError(str(exc)) from exc
                executions[execution_id] = current
                updated.append(current)
            return tuple(updated)

    async def seal_run(self, ref: ExecutionRef) -> None:
        """封口执行树；存在未终结 execution 时拒绝封口。"""
        async with self._lock:
            run_key = _run_key(ref)
            executions = self._runs.get(run_key)
            if executions is None:
                raise ExecutionRegistryError("EXECUTION_RUN_NOT_FOUND")
            if any(not execution.status.terminal for execution in executions.values()):
                raise ExecutionRegistryError("EXECUTION_RUN_NOT_SETTLED")
            self._sealed_runs.add(run_key)

    async def discard_run(self, ref: ExecutionRef) -> None:
        """释放已封口执行树的进程内记录。"""
        async with self._lock:
            run_key = _run_key(ref)
            if run_key not in self._sealed_runs:
                raise ExecutionRegistryError("EXECUTION_RUN_NOT_SEALED")
            # ponytail: registry 只保留活动树；已完成 Run 的幂等历史由 ThreadPersistence 负责。
            self._runs.pop(run_key, None)
            self._sealed_runs.remove(run_key)

    def _require(self, ref: ExecutionRef) -> AgentExecutionBinding:
        """读取当前 execution，不向调用方泄露内部字典。"""
        execution = self._runs.get(_run_key(ref), {}).get(ref.execution_id)
        if execution is None:
            raise ExecutionRegistryError("EXECUTION_NOT_FOUND")
        if execution.ref != ref:
            raise ExecutionRegistryError("EXECUTION_REFERENCE_CONFLICT")
        return execution


def _now_ms() -> int:
    """返回 registry 状态转换使用的毫秒时间。"""
    import time

    return int(time.time() * 1000)


def _run_key(ref: ExecutionRef) -> tuple[str, str]:
    """返回 registry 内部使用的完整 Run 身份。"""
    return ref.thread_id, ref.run_id
