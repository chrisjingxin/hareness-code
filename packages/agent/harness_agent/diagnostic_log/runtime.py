"""Agent 本地诊断日志：调用线程只校验入队，文件 I/O 在单后台 task 执行。"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from harness_agent.diagnostic_log.contract import validate_record
from harness_agent.diagnostic_log.generated import MAX_DIAGNOSTIC_RECORD_BYTES


DiagnosticLevel = Literal["debug", "info", "warn", "error"]
Component = Literal["cli", "agent"]
_LEVEL_ORDER: dict[str, int] = {"debug": 0, "info": 1, "warn": 2, "error": 3}
_CLOSED_FILE = re.compile(r"^(cli|agent)-\d+-\d+-\d{4}\.jsonl$")
_ACTIVE_FILE = re.compile(r"^(cli|agent)-\d+-\d+-\d{4}\.active\.jsonl$")
_DATE_DIRECTORY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


def safe_context_value(value: object) -> str | None:
    """返回可通过 envelope 白名单的安全身份，否则省略该字段。"""
    if isinstance(value, str) and _SAFE_NAME.fullmatch(value):
        return value
    return None


class _NullDiagnosticLog:
    """无 logger 时的业务入口：child/info 都是空操作。"""

    def child(self, _context: Mapping[str, object]) -> "_NullDiagnosticLog":
        return self

    def debug(self, _event: str, _fields: Mapping[str, object]) -> None:
        return None

    def info(self, _event: str, _fields: Mapping[str, object]) -> None:
        return None

    def warn(self, _event: str, _fields: Mapping[str, object]) -> None:
        return None

    def error(self, _event: str, _fields: Mapping[str, object]) -> None:
        return None


NULL_LOG = _NullDiagnosticLog()


class _GuardedLog:
    """把任意 duck-typed logger 收成不抛异常的业务入口。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def child(self, context: Mapping[str, object]) -> "_GuardedLog":
        nested = getattr(self._inner, "child", None)
        if callable(nested):
            try:
                return _GuardedLog(nested(context))
            except Exception:
                return self
        return self

    def debug(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("info", event, fields)

    def warn(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("warn", event, fields)

    def error(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("error", event, fields)

    def _emit(self, level: str, event: str, fields: Mapping[str, object]) -> None:
        try:
            getattr(self._inner, level)(event, fields)
        except Exception:
            return


def ensure_log(log: Any | None = None) -> Any:
    """业务模块拿到的 logger：永不为 None，info/warn/error/debug 不抛。"""
    if log is None:
        return NULL_LOG
    if isinstance(log, (DiagnosticLog, _NullDiagnosticLog, _GuardedLog)):
        return log
    return _GuardedLog(log)


def bind_execution_log(
    log: Any | None,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
    parent_execution_id: str | None = None,
    agent_id: str | None = None,
    activity_id: str | None = None,
) -> Any:
    """把已有 execution 身份绑到 child logger；非法值省略，不抛回业务路径。"""
    context: dict[str, object] = {}
    for key, value in (
        ("thread_id", thread_id),
        ("run_id", run_id),
        ("execution_id", execution_id),
        ("parent_execution_id", parent_execution_id),
        ("agent_id", agent_id),
        ("activity_id", activity_id),
    ):
        if isinstance(value, str):
            safe = safe_context_value(value)
            if safe is not None:
                context[key] = safe
    bound = ensure_log(log)
    return bound.child(context) if context else bound


@dataclass(frozen=True)
class DiagnosticSettings:
    """进程内生效的安全日志设置，数值均已通过配置边界校验。"""

    level: DiagnosticLevel = "info"
    retention_days: int = 14
    max_total_bytes: int = 200 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class _QueuedRecord:
    level: DiagnosticLevel
    sequence: int
    encoded: bytes


class DiagnosticLog:
    """可安全分发给业务 owner 的窄日志接口。"""

    def __init__(self, runtime: "_Runtime", context: Mapping[str, object] | None = None) -> None:
        self._runtime = runtime
        self._context = MappingProxyType(dict(context or {}))

    def child(self, context: Mapping[str, object]) -> "DiagnosticLog":
        """返回带不可变关联上下文的子 logger。"""
        return DiagnosticLog(self._runtime, {**self._context, **context})

    def debug(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("info", event, fields)

    def warn(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("warn", event, fields)

    def error(self, event: str, fields: Mapping[str, object]) -> None:
        self._emit("error", event, fields)

    def _emit(self, level: DiagnosticLevel, event: str, fields: Mapping[str, object]) -> None:
        try:
            self._runtime.emit(self._context, level, event, fields)
        except Exception:
            return


class DiagnosticLifecycle:
    """仅供 Agent composition root 持有的 writer 生命周期。"""

    def __init__(self, runtime: "_Runtime") -> None:
        self._runtime = runtime

    def start(self) -> None:
        self._runtime.start()

    def reconfigure(self, settings: DiagnosticSettings, source: str = "config") -> None:
        self._runtime.reconfigure(settings, source)

    async def close(self) -> dict[str, object]:
        return await self._runtime.close()

    def snapshot(self) -> dict[str, object]:
        return self._runtime.snapshot()


def create_diagnostic_log(
    *,
    component: Component,
    project_fingerprint: str,
    root: Path | None = None,
    settings: DiagnosticSettings | None = None,
    queue_limits: tuple[int, int, int, int] = (4096, 8 * 1024 * 1024, 128, 256 * 1024),
    process_id: int | None = None,
    started_at_ms: int | None = None,
    start_worker: bool = True,
    close_timeout_seconds: float = 2.0,
    writer: Any | None = None,
) -> tuple[DiagnosticLog, DiagnosticLifecycle]:
    """创建共享 queue/writer，以及普通 logger 和生命周期 controller。"""
    runtime = _Runtime(
        component=component,
        project_fingerprint=project_fingerprint,
        root=root or Path.home() / ".harness" / "logs",
        settings=settings or DiagnosticSettings(level=_environment_level() or "info"),
        queue_limits=queue_limits,
        process_id=process_id if process_id is not None else os.getpid(),
        started_at_ms=started_at_ms if started_at_ms is not None else int(time.time() * 1000),
        start_worker=start_worker,
        close_timeout_seconds=close_timeout_seconds,
        writer=writer,
    )
    log = DiagnosticLog(runtime)
    runtime.public_log = log
    log.info(
        "logging.started",
        {
            "effective_level": runtime.settings.level,
            "max_queue_records": queue_limits[0],
            "max_queue_bytes": queue_limits[1],
            "reserved_queue_records": queue_limits[2],
            "reserved_queue_bytes": queue_limits[3],
            "max_file_bytes": runtime.settings.max_file_bytes,
        },
    )
    return log, DiagnosticLifecycle(runtime)


class _Runtime:
    def __init__(
        self,
        *,
        component: Component,
        project_fingerprint: str,
        root: Path,
        settings: DiagnosticSettings,
        queue_limits: tuple[int, int, int, int],
        process_id: int,
        started_at_ms: int,
        start_worker: bool,
        close_timeout_seconds: float,
        writer: Any | None,
    ) -> None:
        self.component = component
        self.project_fingerprint = project_fingerprint
        self.settings = settings
        self.process_id = process_id
        self.started_at_ms = started_at_ms
        self.queue = _BoundedQueue(queue_limits)
        self.writer = writer or _SegmentWriter(component, root, process_id, started_at_ms, lambda: self.settings)
        self.close_timeout_seconds = close_timeout_seconds
        self.public_log: DiagnosticLog | None = None
        self.next_sequence = 1
        self.written = 0
        self.contract_violations = 0
        self.reported_contract_violations = 0
        self.oversize = 0
        self.reported_dropped = {level: 0 for level in _LEVEL_ORDER}
        self.disabled = False
        self.closing = False
        self.started = start_worker
        self._drain_task: asyncio.Task[None] | None = None
        self._writer_failed_reported = False

    def emit(self, context: Mapping[str, object], level: DiagnosticLevel, event: str, fields: Mapping[str, object]) -> None:
        if self.closing or self.disabled or _LEVEL_ORDER[level] < _LEVEL_ORDER[self.settings.level]:
            return
        sequence = self.next_sequence
        self.next_sequence += 1
        record: dict[str, object] = {
            "schema_version": 1,
            "timestamp_ms": int(time.time() * 1000),
            "level": level,
            "event": event,
            "component": self.component,
            "process": {
                "pid": self.process_id,
                "started_at_ms": self.started_at_ms,
                "record_sequence": sequence,
            },
            "project_fingerprint": self.project_fingerprint,
            **context,
            "fields": dict(fields),
        }
        try:
            encoded = (
                json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > MAX_DIAGNOSTIC_RECORD_BYTES:
                self.oversize += 1
                return
            validate_record(record)
            self.queue.enqueue(_QueuedRecord(level, sequence, encoded))
            self._schedule_drain()
        except (TypeError, ValueError, OverflowError):
            self.contract_violations += 1
        except Exception:
            # jsonschema.ValidationError 不应泄漏进业务路径。
            self.contract_violations += 1

    def start(self) -> None:
        self.started = True
        self._schedule_drain()

    def reconfigure(self, settings: DiagnosticSettings, source: str) -> None:
        previous = self.settings.level
        self.settings = settings
        self.emit(
            {},
            "info",
            "logging.reconfigured",
            {
                "previous_level": previous,
                "effective_level": settings.level,
                "source": source,
            },
        )

    async def close(self) -> dict[str, object]:
        if not self.disabled:
            self.emit(
                {},
                "info",
                "logging.stopped",
                {
                    "flush_outcome": "completed",
                    "queued_count": len(self.queue),
                    "written_count": self.written,
                    "dropped_count": sum(self.queue.dropped.values()),
                    "duration_ms": 0,
                },
            )
        self.closing = True
        self.started = True
        self._schedule_drain()
        outcome = "completed"
        try:
            if self._drain_task is not None:
                await asyncio.wait_for(
                    asyncio.shield(self._drain_task),
                    timeout=self.close_timeout_seconds,
                )
        except TimeoutError:
            self.queue.clear()
            outcome = "timeout"
            self.disabled = True
            try:
                await asyncio.wait_for(self.writer.abort(), timeout=0.05)
            except (TimeoutError, OSError):
                pass
        else:
            await self.writer.close()
        if self.disabled and outcome != "timeout":
            outcome = "disabled"
        return {
            "outcome": outcome,
            "written_records": self.written,
            "dropped_records": sum(self.queue.dropped.values()),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "queued_records": len(self.queue),
            "queued_bytes": self.queue.bytes,
            "written_records": self.written,
            "next_record_sequence": self.next_sequence,
            "contract_violations": self.contract_violations,
            "dropped": dict(self.queue.dropped),
            "oversize": self.oversize,
            "disabled": self.disabled,
        }

    def _schedule_drain(self) -> None:
        if not self.started or self.disabled:
            return
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while len(self.queue):
                records = self.queue.take_all()
                for record in records:
                    await self.writer.append(record.encoded)
                self.written += len(records)
                await self._write_aggregate_events()
        except Exception as exc:
            self.disabled = True
            await self._report_writer_failed(exc)
            await self.writer.abort()

    async def _write_aggregate_events(self) -> None:
        pending = {
            level: self.queue.dropped[level] - self.reported_dropped[level]
            for level in _LEVEL_ORDER
        }
        if sum(pending.values()) > 0 or self.oversize > 0:
            encoded = self._internal_record(
                "warn",
                "logging.dropped",
                {
                    "debug_count": pending["debug"],
                    "info_count": pending["info"],
                    "warn_count": pending["warn"],
                    "error_count": pending["error"],
                    "invalid_count": 0,
                    "oversize_count": self.oversize,
                    "reason": "record_too_large" if self.oversize else "queue_full",
                },
            )
            if encoded is not None:
                await self.writer.append(encoded)
            self.reported_dropped = dict(self.queue.dropped)
            self.oversize = 0
        violations = self.contract_violations - self.reported_contract_violations
        if violations > 0:
            encoded = self._internal_record(
                "warn",
                "logging.contract_violation",
                {
                    "invalid_level_count": 0,
                    "invalid_event_count": 0,
                    "invalid_field_count": violations,
                },
            )
            if encoded is not None:
                await self.writer.append(encoded)
            self.reported_contract_violations = self.contract_violations

    async def _report_writer_failed(self, error: BaseException) -> None:
        """writer 不可恢复时最多记一条事件和一条固定 stderr，不递归入队。"""
        if self._writer_failed_reported:
            return
        self._writer_failed_reported = True
        encoded = self._internal_record(
            "error",
            "logging.writer_failed",
            {
                "failure_stage": "append",
                "error_type": type(error).__name__,
                "summary_code": "WRITE_FAILED",
            },
        )
        if encoded is not None:
            try:
                await self.writer.append(encoded)
            except Exception:
                pass
        try:
            sys.stderr.write("HARNESS_DIAGNOSTIC_WRITER_FAILED\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _internal_record(self, level: DiagnosticLevel, event: str, fields: dict[str, object]) -> bytes | None:
        sequence = self.next_sequence
        self.next_sequence += 1
        record = {
            "schema_version": 1,
            "timestamp_ms": int(time.time() * 1000),
            "level": level,
            "event": event,
            "component": self.component,
            "process": {
                "pid": self.process_id,
                "started_at_ms": self.started_at_ms,
                "record_sequence": sequence,
            },
            "project_fingerprint": self.project_fingerprint,
            "fields": fields,
        }
        try:
            validate_record(record)
            return (
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except Exception:
            return None


class _BoundedQueue:
    def __init__(self, limits: tuple[int, int, int, int]) -> None:
        self.max_records, self.max_bytes, self.reserved_records, self.reserved_bytes = limits
        self.records: deque[_QueuedRecord] = deque()
        self.bytes = 0
        self.dropped: dict[str, int] = {level: 0 for level in _LEVEL_ORDER}

    def __len__(self) -> int:
        return len(self.records)

    def enqueue(self, record: _QueuedRecord) -> None:
        priority = record.level in {"warn", "error"}
        max_records = self.max_records if priority else self.max_records - self.reserved_records
        max_bytes = self.max_bytes if priority else self.max_bytes - self.reserved_bytes
        if record.level == "info":
            self._evict_until_fits("debug", record, max_records, max_bytes)
        if priority:
            self._evict_until_fits("debug", record, max_records, max_bytes)
            self._evict_until_fits("info", record, max_records, max_bytes)
        if len(self.records) + 1 > max_records or self.bytes + len(record.encoded) > max_bytes:
            self.dropped[record.level] += 1
            return
        self.records.append(record)
        self.bytes += len(record.encoded)

    def take_all(self) -> list[_QueuedRecord]:
        result = sorted(self.records, key=lambda record: record.sequence)
        self.records.clear()
        self.bytes = 0
        return result

    def clear(self) -> None:
        self.records.clear()
        self.bytes = 0

    def _evict_until_fits(
        self,
        level: str,
        incoming: _QueuedRecord,
        max_records: int,
        max_bytes: int,
    ) -> None:
        while len(self.records) + 1 > max_records or self.bytes + len(incoming.encoded) > max_bytes:
            index = next((i for i, record in enumerate(self.records) if record.level == level), None)
            if index is None:
                return
            record = self.records[index]
            del self.records[index]
            self.bytes -= len(record.encoded)
            self.dropped[level] += 1


class _SegmentWriter:
    def __init__(
        self,
        component: Component,
        root: Path,
        process_id: int,
        started_at_ms: int,
        settings: Any,
    ) -> None:
        self.component = component
        self.root = root
        self.process_id = process_id
        self.started_at_ms = started_at_ms
        self._settings = settings
        self.segment = 0
        self.bytes = 0
        self.file: Any = None
        self.active_path: Path | None = None

    async def append(self, encoded: bytes) -> None:
        if self.file is None:
            await self._open_segment()
        if self.bytes and self.bytes + len(encoded) > self._settings().max_file_bytes:
            await self._finalize_segment()
            await self._open_segment()
        await asyncio.to_thread(_write_all, self.file, encoded)
        self.bytes += len(encoded)

    async def close(self) -> None:
        await self._finalize_segment()

    async def abort(self) -> None:
        if self.file is not None:
            await asyncio.to_thread(self.file.close)
            self.file = None

    async def _open_segment(self) -> None:
        date = datetime.fromtimestamp(self.started_at_ms / 1000, tz=UTC).date().isoformat()
        directory = self.root / date
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True, mode=0o700)
        if await asyncio.to_thread(self.root.is_symlink):
            raise OSError("Diagnostic log root must not be a symlink")
        if os.name != "nt":
            await asyncio.to_thread(self.root.chmod, 0o700)
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            await asyncio.to_thread(directory.chmod, 0o700)
        while True:
            name = f"{self.component}-{self.started_at_ms}-{self.process_id}-{self.segment:04d}.active.jsonl"
            path = directory / name
            try:
                # 无缓冲 binary writer：突然崩溃最多丢 queue 尾部，不把已经由
                # worker 接受的整批记录继续滞留在 Python 用户态 buffer。
                self.file = await asyncio.to_thread(path.open, "xb", buffering=0)
                if os.name != "nt":
                    await asyncio.to_thread(path.chmod, 0o600)
                self.active_path = path
                self.bytes = 0
                await self._retain_closed()
                return
            except FileExistsError:
                self.segment += 1

    async def _finalize_segment(self) -> None:
        if self.file is None or self.active_path is None:
            return
        active_path = self.active_path
        await asyncio.to_thread(self.file.close)
        self.file = None
        self.active_path = None
        closed_path = active_path.with_name(active_path.name.replace(".active.jsonl", ".jsonl"))
        await asyncio.to_thread(active_path.rename, closed_path)
        self.segment += 1

    async def _retain_closed(self) -> None:
        await asyncio.to_thread(self._retain_closed_sync)

    def _retain_closed_sync(self) -> None:
        candidates: list[tuple[Path, os.stat_result]] = []
        protected_bytes = 0
        if not self.root.exists():
            return
        for directory in self.root.iterdir():
            if not _DATE_DIRECTORY.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if _ACTIVE_FILE.fullmatch(path.name):
                    protected_bytes += path.stat().st_size
                    continue
                if not _CLOSED_FILE.fullmatch(path.name):
                    continue
                candidates.append((path, path.stat()))
        candidates.sort(key=lambda item: item[1].st_mtime)
        cutoff = time.time() - self._settings().retention_days * 86_400
        total = protected_bytes + sum(info.st_size for _, info in candidates)
        for path, info in candidates:
            if info.st_mtime < cutoff or total > self._settings().max_total_bytes:
                try:
                    path.unlink()
                    total -= info.st_size
                except OSError:
                    continue


def default_process_fields(command_kind: str) -> dict[str, object]:
    """返回不含用户数据的进程启动字段。"""
    return {
        "command_kind": command_kind,
        "runtime_version": f"python-{sys.version_info.major}.{sys.version_info.minor}",
        "platform": platform.system().lower(),
        "arch": platform.machine().lower(),
    }


def _environment_level() -> DiagnosticLevel | None:
    value = os.environ.get("HARNESS_LOG_LEVEL")
    if value in _LEVEL_ORDER:
        return value  # type: ignore[return-value]
    return None


def _write_all(file: Any, encoded: bytes) -> None:
    """处理底层 partial write；只允许完整 JSONL record 进入 segment。"""
    remaining = memoryview(encoded)
    while remaining:
        written = file.write(remaining)
        if not isinstance(written, int) or written <= 0:
            raise OSError("Diagnostic writer made no progress")
        remaining = remaining[written:]
