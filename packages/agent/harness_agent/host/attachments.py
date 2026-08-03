"""本机 WebSocket attachment 的签发、认证与生命周期。"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from harness_agent.host.connection import ProtocolConnection, RpcError
from harness_agent.protocol.generated import MAX_FRAME_BYTES


@dataclass(slots=True)
class _AttachmentRecord:
    """一次签发 attachment 的完整 transport 状态，token 不作为身份键。"""

    attachment_id: str
    token: str
    origin: str
    expires_at_ms: int
    capability_ceiling: frozenset[str]
    state: str = "issued"
    connection: ProtocolConnection | None = None
    websocket: Any | None = None
    closed_future: asyncio.Future[None] | None = None


class AttachmentManager:
    """集中管理只绑定 loopback 的一次性 WebSocket attachment。"""

    def __init__(
        self,
        *,
        create_connection: Callable[..., ProtocolConnection],
        dispatch_connection: Callable[[ProtocolConnection, dict[str, Any]], Awaitable[None]],
        close_connection: Callable[[ProtocolConnection], Awaitable[None]],
        register_attachment: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        """保存 Host 提供的 Connection 操作，不拥有 Project 运行资源。"""
        self._create_connection = create_connection
        self._dispatch_connection = dispatch_connection
        self._close_connection = close_connection
        self._register_attachment = register_attachment
        self._records: dict[str, _AttachmentRecord] = {}
        self._lock = asyncio.Lock()
        self._websocket_server: Any | None = None

    async def create(
        self,
        origin: str,
        capability_ceiling: Iterable[str],
    ) -> dict[str, object]:
        """签发绑定 Origin、60 秒过期且只能消费一次的 attachment。"""
        if not (
            origin.startswith("http://127.0.0.1:")
            or origin.startswith("http://localhost:")
        ):
            raise RpcError(-32602, "Attachment origin must be a loopback HTTP origin")
        await self._ensure_listener()
        attachment_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        expires_at_ms = int(time.time() * 1000) + 60_000
        async with self._lock:
            now_ms = int(time.time() * 1000)
            self._records = {
                key: record
                for key, record in self._records.items()
                if record.expires_at_ms > now_ms
            }
            self._records[attachment_id] = _AttachmentRecord(
                attachment_id=attachment_id,
                token=token,
                origin=origin,
                expires_at_ms=expires_at_ms,
                capability_ceiling=frozenset(capability_ceiling),
            )
        socket = self._websocket_server.sockets[0]
        return {
            "attachment_id": attachment_id,
            "endpoint": f"ws://127.0.0.1:{socket.getsockname()[1]}",
            "token": token,
            "expires_at_ms": expires_at_ms,
        }

    async def revoke(self, attachment_id: str) -> ProtocolConnection | None:
        """使 token 失效并关闭认证中/已连接 socket；返回关联 Connection。"""
        async with self._lock:
            record = self._records.get(attachment_id)
            if record is None:
                raise RpcError(
                    -32009,
                    "ATTACHMENT_NOT_FOUND",
                    {"code": "ATTACHMENT_NOT_FOUND", "retryable": False},
                )
            # handler 是否仍存活：authenticating/connected 才有 closed_future 解析者。
            handler_alive = record.state in {"authenticating", "connected"}
            if record.state != "revoked":
                record.state = "revoked"
                if record.closed_future is None:
                    record.closed_future = asyncio.get_running_loop().create_future()
            future = record.closed_future
            websocket = record.websocket
            connection = record.connection
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                # 连接可能已由对端或 Host 关闭；close future 仍会收敛清理。
                pass
            if future is not None and handler_alive:
                # 只有 handler 仍存活时 closed_future 才会被解析；
                # 已 closed/issued 的终态记录没有解析者，等待只会空耗 5 秒。
                try:
                    await asyncio.wait_for(future, timeout=5)
                except TimeoutError:
                    pass
        return connection

    async def close(self) -> None:
        """关闭 listener 并使所有未消费凭证失效；可重复调用。"""
        if self._websocket_server is not None:
            self._websocket_server.close()
            await self._websocket_server.wait_closed()
            self._websocket_server = None
        self._records.clear()

    async def _ensure_listener(self) -> None:
        """惰性启动只绑定 loopback 的 WebSocket adapter。"""
        if self._websocket_server is not None:
            return
        from websockets.asyncio.server import serve

        self._websocket_server = await serve(
            self._handle_websocket,
            "127.0.0.1",
            0,
            max_size=MAX_FRAME_BYTES,
            max_queue=16,
            compression=None,
        )

    async def _handle_websocket(self, websocket: Any) -> None:
        """认证 attachment 后，将文本帧交给 Host 的 protocol dispatcher。"""
        connection: ProtocolConnection | None = None
        writer: asyncio.Task[None] | None = None
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        record: _AttachmentRecord | None = None

        async def send_attached(message: dict[str, Any]) -> None:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                raise RpcError(-32603, "Outbound JSON-RPC frame exceeds size limit")
            try:
                queue.put_nowait(encoded)
            except asyncio.QueueFull as exc:
                raise RpcError(-32004, "Attached connection is too slow") from exc

        async def write_frames() -> None:
            while True:
                await websocket.send(await queue.get())

        try:
            raw_auth = await asyncio.wait_for(websocket.recv(), timeout=5)
            if not isinstance(raw_auth, str):
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            auth = json.loads(raw_auth)
            if set(auth) != {"type", "token"} or auth.get("type") != "auth":
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            token = auth.get("token")
            origin = websocket.request.headers.get("Origin")
            async with self._lock:
                candidate = (
                    next(
                        (
                            item
                            for item in self._records.values()
                            if item.token == token
                        ),
                        None,
                    )
                    if isinstance(token, str)
                    else None
                )
                if (
                    candidate is None
                    or candidate.state != "issued"
                    or candidate.expires_at_ms <= int(time.time() * 1000)
                    or origin != candidate.origin
                ):
                    candidate = None
                else:
                    candidate.state = "authenticating"
                    candidate.websocket = websocket
            if candidate is None:
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            record = candidate
            connection = self._create_connection(
                send_attached,
                capability_ceiling=record.capability_ceiling,
            )
            async with self._lock:
                if record.state == "revoked":
                    rejected = True
                else:
                    rejected = False
                    record.connection = connection
                    record.state = "connected"
            if rejected:
                await self._close_connection(connection)
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            await self._register_attachment(
                record.attachment_id,
                connection.connection_id,
            )
            writer = asyncio.create_task(write_frames(), name="harness-websocket-writer")
            await websocket.send(json.dumps({"type": "ready"}, separators=(",", ":")))
            async for raw in websocket:
                if not isinstance(raw, str):
                    await websocket.close(code=1003, reason="Text frames required")
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.close(code=1007, reason="Invalid JSON")
                    break
                if not isinstance(message, dict):
                    await websocket.close(code=1007, reason="Invalid JSON-RPC")
                    break
                await self._dispatch_connection(connection, message)
        except (TimeoutError, json.JSONDecodeError):
            await websocket.close(code=1008, reason="Attachment rejected")
        finally:
            if record is not None:
                async with self._lock:
                    if record.connection is connection and record.state != "revoked":
                        record.state = "closed"
                    future = record.closed_future
                if future is not None and not future.done():
                    future.set_result(None)
            if connection is not None:
                await self._close_connection(connection)
            if writer is not None:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
