"""Harness thread 持久化：以用户级 SQLite 保存 LangGraph checkpoint 和当前 project 的线程索引。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


_SCHEMA_VERSION = 1
_MAX_PREVIEW_CHARS = 160


class ThreadStoreError(RuntimeError):
    """线程存储不可用、损坏或版本不兼容时返回的可诊断错误。"""


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """恢复选择器所需的当前 project 线程摘要；内部 ID 不应直接展示给用户。"""

    thread_id: str
    created_at_ms: int
    updated_at_ms: int
    first_message: str
    latest_message: str
    message_count: int


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """由 checkpoint 归一化出的稳定消息历史，供 CLI 表现层回放。"""

    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    """已校验归属 project 的线程快照和可回放消息。"""

    summary: ThreadSummary
    messages: tuple[ThreadMessage, ...]


class ProjectScopedAsyncSqliteSaver(AsyncSqliteSaver):
    """将 LangGraph 自动归一的 checkpoint namespace 固定映射到当前 project。"""

    def __init__(self, connection: aiosqlite.Connection, project_fingerprint: str) -> None:
        """复用同一 SQLite 连接，并保留 project 指纹作为根 namespace。"""
        super().__init__(connection)
        self._project_fingerprint = project_fingerprint

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        """读取时即使根图丢弃 namespace，仍只查询当前 project 的 checkpoint。"""
        return await super().aget_tuple(self._scoped_config(config))

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        """列举时要求 thread 范围，禁止通过底层 saver 跨 project 扫描。"""
        if config is None:
            raise ThreadStoreError("CHECKPOINT_LIST_REQUIRES_THREAD")
        scoped_before = self._scoped_config(before) if before is not None else None
        async for checkpoint in super().alist(
            self._scoped_config(config),
            filter=filter,
            before=scoped_before,
            limit=limit,
        ):
            yield checkpoint

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """写入时给根图补回 project namespace，并为子图保留独立后缀。"""
        return await super().aput(
            self._scoped_config(config),
            checkpoint,
            metadata,
            new_versions,
        )

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        """将中间 writes 与对应 checkpoint 放入同一 project namespace。"""
        await super().aput_writes(
            self._scoped_config(config),
            writes,
            task_id,
            task_path,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """删除操作只能清理当前 project 的根和子图 namespace，不能跨 project。"""
        prefix = f"{self._project_fingerprint}:%"
        async with self.lock, self.conn.cursor() as cursor:
            for table in ("checkpoints", "writes"):
                await cursor.execute(
                    f"DELETE FROM {table} WHERE thread_id = ? AND (checkpoint_ns = ? OR checkpoint_ns LIKE ?)",
                    (str(thread_id), self._project_fingerprint, prefix),
                )
            await self.conn.commit()

    def _scoped_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """合成 namespace：根图为指纹，子图为指纹加 LangGraph 原始后缀。"""
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise ThreadStoreError("CHECKPOINT_CONFIG_INVALID")
        raw_namespace = configurable.get("checkpoint_ns")
        namespace = str(raw_namespace) if raw_namespace is not None else ""
        if namespace in {"", self._project_fingerprint}:
            scoped_namespace = self._project_fingerprint
        elif namespace.startswith(f"{self._project_fingerprint}:"):
            scoped_namespace = namespace
        else:
            scoped_namespace = f"{self._project_fingerprint}:{namespace}"
        return {
            **config,
            "configurable": {**configurable, "checkpoint_ns": scoped_namespace},
        }


class ThreadStore:
    """封装 checkpoint、project namespace 与线程索引，避免调用方理解 SQLite 细节。"""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        checkpointer: ProjectScopedAsyncSqliteSaver,
        path: Path,
        project_fingerprint: str,
    ) -> None:
        """保存已验证的连接和固定 project namespace。"""
        self._connection = connection
        self._checkpointer = checkpointer
        self._path = path
        self._project_fingerprint = project_fingerprint
        self._closed = False
        self._lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        *,
        project: Path,
        home: Path | None = None,
    ) -> "ThreadStore":
        """打开用户级数据库、检查完整性并应用 Harness 自有索引迁移。"""
        base_home = (home or Path.home()).expanduser().resolve()
        data_dir = base_home / ".harness"
        connection: aiosqlite.Connection | None = None
        try:
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(data_dir, 0o700)
            path = data_dir / "threads.sqlite3"
            connection = await aiosqlite.connect(path)
            os.chmod(path, 0o600)
            connection.row_factory = aiosqlite.Row
            project_fingerprint = _project_fingerprint(project)
            checkpointer = ProjectScopedAsyncSqliteSaver(connection, project_fingerprint)
            store = cls(
                connection=connection,
                checkpointer=checkpointer,
                path=path,
                project_fingerprint=project_fingerprint,
            )
            await store._prepare()
            return store
        except ThreadStoreError:
            if connection is not None:
                try:
                    await connection.close()
                except aiosqlite.Error:
                    pass
            raise
        except (OSError, aiosqlite.Error) as exc:
            if connection is not None:
                try:
                    await connection.close()
                except aiosqlite.Error:
                    pass
            raise ThreadStoreError(f"CHECKPOINT_OPEN_FAILED: {exc}") from exc

    @property
    def checkpointer(self) -> ProjectScopedAsyncSqliteSaver:
        """返回注入 DeepAgents 图的异步 LangGraph checkpointer。"""
        return self._checkpointer

    @property
    def database_path(self) -> Path:
        """返回当前用户可手动清理的数据库路径。"""
        return self._path

    @property
    def project_fingerprint(self) -> str:
        """返回仅用于 namespace 和索引过滤的不可逆 project 标识。"""
        return self._project_fingerprint

    def graph_config(self, thread_id: str) -> dict[str, dict[str, str]]:
        """构造 LangGraph 所需的 thread_id 和 project 隔离 checkpoint namespace。"""
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": self._project_fingerprint,
            }
        }

    async def record_message(self, thread_id: str, message: str) -> None:
        """在 run 受理时登记 thread，供恢复选择器在进程重启后发现它。"""
        self._ensure_open()
        now = _now_ms()
        preview = _preview(message)
        try:
            async with self._lock:
                await self._connection.execute(
                    """
                    INSERT INTO harness_threads (
                        project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                        first_message, latest_message, message_count
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                        updated_at_ms = excluded.updated_at_ms,
                        latest_message = excluded.latest_message
                    """,
                    (
                        self._project_fingerprint,
                        thread_id,
                        now,
                        now,
                        preview,
                        preview,
                    ),
                )
                await self._connection.commit()
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_INDEX_WRITE_FAILED: {exc}") from exc

    async def refresh_thread(self, thread_id: str) -> None:
        """在 run 结束后用 checkpoint 消息数更新可恢复线程摘要。"""
        self._ensure_open()
        try:
            messages = await self._messages_for_thread(thread_id)
            count = (
                sum(_normalize_message(message) is not None for message in messages)
                if messages is not None
                else 0
            )
            async with self._lock:
                await self._connection.execute(
                    """
                    UPDATE harness_threads
                    SET updated_at_ms = ?, message_count = ?
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (_now_ms(), count, self._project_fingerprint, thread_id),
                )
                await self._connection.commit()
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_INDEX_REFRESH_FAILED: {exc}") from exc

    async def list_threads(self, limit: int = 80) -> tuple[ThreadSummary, ...]:
        """按最后活动时间返回当前 project 的有限线程摘要。"""
        self._ensure_open()
        if limit < 1 or limit > 200:
            raise ThreadStoreError("CHECKPOINT_LIST_INVALID_LIMIT")
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, created_at_ms, updated_at_ms, first_message,
                           latest_message, message_count
                    FROM harness_threads
                    WHERE project_fingerprint = ?
                    ORDER BY updated_at_ms DESC, thread_id ASC
                    LIMIT ?
                    """,
                    (self._project_fingerprint, limit),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_summary(row) for row in rows)
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_LIST_FAILED: {exc}") from exc

    async def open_thread(self, thread_id: str) -> OpenThread:
        """读取一个归属当前 project 的可恢复 thread；索引和 checkpoint 均缺失即拒绝。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, created_at_ms, updated_at_ms, first_message,
                           latest_message, message_count
                    FROM harness_threads
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                raise ThreadStoreError("THREAD_NOT_FOUND")
            messages = await self._messages_for_thread(thread_id)
            if messages is None:
                raise ThreadStoreError("THREAD_NOT_RECOVERABLE")
            normalized = tuple(_normalize_message(message) for message in messages)
            return OpenThread(
                summary=_summary(row),
                messages=tuple(message for message in normalized if message is not None),
            )
        except ThreadStoreError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_READ_FAILED: {exc}") from exc

    async def close(self) -> None:
        """提交并关闭连接，确保 CLI 退出后用户可安全删除数据库及 WAL 文件。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._connection.commit()
            await self._connection.close()
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_CLOSE_FAILED: {exc}") from exc

    async def _prepare(self) -> None:
        """验证 SQLite 可读性、初始化 LangGraph 表并升级 Harness 线程索引。"""
        try:
            try:
                cursor = await self._connection.execute("PRAGMA integrity_check")
                row = await cursor.fetchone()
                await cursor.close()
            except aiosqlite.Error as exc:
                raise ThreadStoreError(f"CHECKPOINT_DATABASE_CORRUPT: {exc}") from exc
            if not row or row[0] != "ok":
                detail = row[0] if row else "no result"
                raise ThreadStoreError(f"CHECKPOINT_DATABASE_CORRUPT: {detail}")
            cursor = await self._connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            version = int(row[0]) if row else 0
            if version > _SCHEMA_VERSION:
                raise ThreadStoreError(
                    f"CHECKPOINT_SCHEMA_TOO_NEW: found {version}, supports {_SCHEMA_VERSION}"
                )
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA busy_timeout=5000")
            await self._checkpointer.setup()
            if version < 1:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_threads (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        first_message TEXT NOT NULL,
                        latest_message TEXT NOT NULL,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_threads_project_updated
                    ON harness_threads(project_fingerprint, updated_at_ms DESC)
                    """
                )
                await self._connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            await self._connection.commit()
        except ThreadStoreError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadStoreError(f"CHECKPOINT_MIGRATION_FAILED: {exc}") from exc

    def _ensure_open(self) -> None:
        """阻止关闭后的 handler 继续使用失效连接。"""
        if self._closed:
            raise ThreadStoreError("CHECKPOINT_STORE_CLOSED")

    async def _messages_for_thread(self, thread_id: str) -> list[Any] | None:
        """读取普通或 DeltaChannel checkpoint 的完整消息，兼容 DeepAgents 的增量存储。"""
        checkpoint = await self._checkpointer.aget_tuple(self.graph_config(thread_id))
        if checkpoint is None:
            return None
        direct = _checkpoint_messages(checkpoint.checkpoint)
        if direct is not None:
            return direct
        history = await self._checkpointer.aget_delta_channel_history(
            config=self.graph_config(thread_id),
            channels=["messages"],
        )
        return _replay_delta_messages(history)


def _project_fingerprint(project: Path) -> str:
    """从规范化 project 路径生成不可逆 namespace，禁止原始路径进入数据库。"""
    return hashlib.sha256(str(project.expanduser().resolve()).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    """延迟导入时间模块，保持路径和数据转换函数的纯粹性。"""
    import time

    return int(time.time() * 1000)


def _preview(value: str) -> str:
    """将用户消息压缩为单行有限摘要，避免选择器被超长或换行文本破坏。"""
    compact = " ".join(value.split())
    return compact[:_MAX_PREVIEW_CHARS] or "(空消息)"


def _summary(row: Mapping[str, Any]) -> ThreadSummary:
    """将 SQLite 行转换为不携带 project 路径的线程摘要。"""
    return ThreadSummary(
        thread_id=str(row["thread_id"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        first_message=str(row["first_message"]),
        latest_message=str(row["latest_message"]),
        message_count=int(row["message_count"]),
    )


def _checkpoint_messages(checkpoint: Mapping[str, Any] | Any) -> list[Any] | None:
    """从非增量 LangGraph checkpoint 读取消息 channel；DeltaChannel 返回 None 交给回放。"""
    if not isinstance(checkpoint, Mapping):
        return None
    channels = checkpoint.get("channel_values")
    if not isinstance(channels, Mapping):
        return None
    messages = channels.get("messages")
    return list(messages) if isinstance(messages, list) else None


def _replay_delta_messages(history: Mapping[str, Any]) -> list[Any]:
    """使用 DeepAgents 的确定性 reducer 回放 DeltaChannel seed 和历史 writes。"""
    entry = history.get("messages")
    if not isinstance(entry, Mapping):
        return []
    seed = entry.get("seed")
    seed_messages = getattr(seed, "value", seed)
    base = list(seed_messages) if isinstance(seed_messages, list) else []
    writes = entry.get("writes")
    values = [write[2] for write in writes if isinstance(write, tuple) and len(write) >= 3] if isinstance(writes, list) else []
    if not values:
        return base
    from deepagents._messages_reducer import _messages_delta_reducer

    return list(_messages_delta_reducer(base, values))


def _normalize_message(value: Any) -> ThreadMessage | None:
    """把 LangChain 消息收敛为 TUI 可安全回放的 project/thread/message 领域值。"""
    name = type(value).__name__
    content = _message_content(getattr(value, "content", ""))
    if name == "HumanMessage":
        return ThreadMessage(kind="user", content=content)
    if name == "AIMessage":
        return ThreadMessage(kind="assistant", content=content)
    if name == "ToolMessage":
        return ThreadMessage(kind="tool", content=content, tool_name=str(getattr(value, "name", "tool")))
    return None


def _message_content(value: Any) -> str:
    """从 LangChain string 或内容块列表中提取稳定文本，避免原始对象越过模块边界。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if value is None else str(value)
