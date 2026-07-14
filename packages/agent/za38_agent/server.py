"""JSON-RPC 2.0 server over stdin/stdout (newline-delimited)."""
import asyncio
import json
import sys
import uuid
from typing import Any


class JsonRpcServer:
    """Async JSON-RPC server reading from stdin, writing to stdout."""

    def __init__(self) -> None:
        self.agent = None  # Set in Task 3
        self._running = True
        self._handlers = {
            "initialize": self._handle_initialize,
            "query": self._handle_query,
            "cancel": self._handle_cancel,
            "respond": self._handle_respond,
            "shutdown": self._handle_shutdown,
        }

    async def run(self) -> None:
        """Main loop: read lines from stdin, dispatch messages."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            line = await reader.readline()
            if not line:
                break
            msg = None
            try:
                msg = json.loads(line.decode("utf-8"))
                await self.dispatch(msg)
            except json.JSONDecodeError:
                await self.send_error(None, -32700, "Parse error")
            except Exception as e:
                msg_id = msg.get("id") if msg else None
                await self.send_error(msg_id, -32603, str(e))

    async def dispatch(self, msg: dict[str, Any]) -> None:
        """Route a JSON-RPC message to the appropriate handler."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        handler = self._handlers.get(method)
        if handler is None:
            await self.send_error(msg_id, -32601, f"Method not found: {method}")
            return

        params_with_id = {**params, "_id": msg_id}
        result = await handler(params_with_id)
        if result is not None and msg_id is not None:
            await self.send_response(msg_id, result)

    async def send(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout."""
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def send_response(self, msg_id: int | None, result: Any) -> None:
        await self.send({"jsonrpc": "2.0", "result": result, "id": msg_id})

    async def send_error(self, msg_id: int | None, code: int, message: str) -> None:
        await self.send({"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": msg_id})

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_initialize(self, params: dict) -> dict:
        return {
            "server_info": {"name": "za38-agent", "version": "0.1.0"},
            "capabilities": {"streaming": True, "hitl": True},
        }

    async def _handle_query(self, params: dict) -> dict | None:
        thread_id = params.get("thread_id") or str(uuid.uuid4())
        msg_id = params.get("_id")

        # Send response immediately (request accepted)
        if msg_id is not None:
            await self.send_response(msg_id, {"thread_id": thread_id, "accepted": True})

        # If agent is set, stream events (Task 3 will wire this up)
        if self.agent is not None:
            await self._stream_agent_response(params["message"], thread_id)
        else:
            # Echo mode: just send done
            await self.send_notification("stream/done", {
                "thread_id": thread_id,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            })

        return None  # Response already sent

    async def _stream_agent_response(self, message: str, thread_id: str) -> None:
        """Stream agent response — implemented in Task 3."""
        raise NotImplementedError("Agent streaming not yet implemented")

    async def _handle_cancel(self, params: dict) -> dict:
        return {"cancelled": True}

    async def _handle_respond(self, params: dict) -> dict:
        return {"accepted": True}

    async def _handle_shutdown(self, params: dict) -> dict:
        self._running = False
        return {}
