"""通过换行分隔 stdin/stdout 通讯的并发 JSON-RPC 2.0 服务端。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from za38_agent.config import ConfigError, Za38Config, load_config

logger = logging.getLogger(__name__)


class RpcError(Exception):
    """可安全返回给客户端的预期 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ActiveRun:
    """一次运行中或等待中断恢复的 Agent 调用状态。"""

    thread_id: str
    run_id: str
    message: str
    task: asyncio.Task[None] | None = None
    sequence: int = 0
    status: str = "running"
    interrupt_id: str | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    started_at: float = field(default_factory=time.monotonic)


AgentFactory = Callable[[Za38Config, Path], Any | Awaitable[Any]]


class JsonRpcServer:
    """管理 Python Agent 生命周期与 JSON-RPC stdio 控制面。"""

    def __init__(
        self,
        *,
        agent: Any | None = None,
        agent_factory: AgentFactory | None = None,
        allow_echo: bool | None = None,
    ) -> None:
        self.agent = agent
        self._agent_factory = agent_factory or self._create_default_agent
        self._allow_echo = (
            os.environ.get("ZA38_ECHO_MODE") == "1" if allow_echo is None else allow_echo
        )
        self._running = True
        self._send_lock = asyncio.Lock()
        self._runs: dict[str, ActiveRun] = {}
        self._workspace = Path.cwd().resolve()
        self._config_path: str | None = None
        self._config: Za38Config | None = None
        self._startup_error: str | None = None
        self._handlers = {
            "initialize": self._handle_initialize,
            "query": self._handle_query,
            "cancel": self._handle_cancel,
            "respond": self._handle_respond,
            "config.show": self._handle_config_show,
            "config.path": self._handle_config_path,
            "shutdown": self._handle_shutdown,
        }

    async def run(self) -> None:
        """持续读取 JSONL 请求，直到 EOF 或收到正常关闭请求。"""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self.send_error(None, -32700, "Parse error")
                    continue
                if not isinstance(message, dict):
                    await self.send_error(None, -32600, "Invalid Request")
                    continue
                await self.dispatch(message)
        finally:
            await self._cancel_all_runs()

    async def dispatch(self, message: dict[str, Any]) -> None:
        """校验并路由单个 JSON-RPC 请求，不能阻塞正在运行的任务。"""
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0":
            await self.send_error(request_id, -32600, "Invalid Request: jsonrpc must be '2.0'")
            return
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str):
            await self.send_error(request_id, -32600, "Invalid Request: method must be a string")
            return
        if not isinstance(params, dict):
            await self.send_error(request_id, -32602, "Invalid params: params must be an object")
            return

        handler = self._handlers.get(method)
        if handler is None:
            await self.send_error(request_id, -32601, f"Method not found: {method}")
            return

        try:
            result = await handler({**params, "_id": request_id})
        except RpcError as exc:
            await self.send_error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - last-resort protocol guard
            logger.exception("Unhandled JSON-RPC handler error for %s", method)
            await self.send_error(request_id, -32603, str(exc))
        else:
            if result is not None and request_id is not None:
                await self.send_response(request_id, result)

    async def send(self, message: dict[str, Any]) -> None:
        """向 stdout 写入恰好一帧 JSON-RPC，避免并发任务帧交错。"""
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._send_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def send_response(self, request_id: int | str | None, result: Any) -> None:
        await self.send({"jsonrpc": "2.0", "result": result, "id": request_id})

    async def send_error(self, request_id: int | str | None, code: int, message: str) -> None:
        await self.send(
            {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": request_id}
        )

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        if cwd is not None:
            try:
                self._workspace = Path(str(cwd)).expanduser().resolve()
            except OSError as exc:
                raise RpcError(-32602, f"Invalid cwd: {exc}") from exc
        config_path = params.get("config_path")
        self._config_path = str(config_path) if config_path else None
        self._load_config()
        return {
            "server_info": {"name": "za38-agent", "version": "0.1.0"},
            "protocol_version": 1,
            "capabilities": {
                "streaming": True,
                "hitl": True,
                "cancellation": True,
                "config": True,
                "echo_mode": self._allow_echo,
            },
            "config": self._config.redacted() if self._config else None,
            "startup_error": self._startup_error,
        }

    async def _handle_query(self, params: dict[str, Any]) -> None:
        message = params.get("message")
        if not isinstance(message, str) or not message.strip():
            raise RpcError(-32602, "Invalid params: message must be a non-empty string")
        thread_id = str(params.get("thread_id") or uuid.uuid4())
        run_id = str(params.get("run_id") or uuid.uuid4())
        existing = self._runs.get(thread_id)
        if existing and existing.status in {"running", "interrupted"}:
            raise RpcError(-32000, f"Thread {thread_id} already has an active run")

        request_id = params.get("_id")
        run = ActiveRun(thread_id=thread_id, run_id=run_id, message=message)
        self._runs[thread_id] = run
        if request_id is not None:
            await self.send_response(request_id, {"thread_id": thread_id, "run_id": run_id, "accepted": True})
        run.task = asyncio.create_task(self._execute_run(run), name=f"za38-run-{run_id}")
        return None

    async def _handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(params)
        if run.status == "interrupted":
            await self._emit(run, "run/cancelled", {"reason": "Cancelled while awaiting user response"})
            self._runs.pop(run.thread_id, None)
            run.status = "cancelled"
            return {"cancelled": True, "run_id": run.run_id}
        if run.task and not run.task.done():
            run.task.cancel()
            return {"cancelled": True, "run_id": run.run_id}
        return {"cancelled": False, "run_id": run.run_id}

    async def _handle_respond(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(params)
        interrupt_id = params.get("interrupt_id")
        if run.status != "interrupted" or interrupt_id != run.interrupt_id:
            raise RpcError(-32001, "No matching interrupt is awaiting a response")
        decisions = params.get("decisions")
        if decisions is None:
            raise RpcError(-32602, "Invalid params: decisions is required")
        run.status = "running"
        run.interrupt_id = None
        # LangGraph 支持按中断 id 恢复；使用映射可避免未来同一图存在多个中断时错配。
        resume_payload = {str(interrupt_id): decisions}
        run.task = asyncio.create_task(
            self._execute_run(run, resume=resume_payload), name=f"za38-resume-{run.run_id}"
        )
        return {"accepted": True, "run_id": run.run_id}

    async def _handle_config_show(self, params: dict[str, Any]) -> dict[str, Any]:
        self._load_config()
        if self._config is None:
            raise RpcError(-32010, self._startup_error or "Configuration is unavailable")
        return self._config.redacted()

    async def _handle_config_path(self, params: dict[str, Any]) -> dict[str, Any]:
        self._load_config()
        return {
            "workspace": str(self._workspace),
            "paths": [str(path) for path in self._config.paths] if self._config else [],
            "explicit_path": self._config_path,
        }

    async def _handle_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self._running = False
        await self._cancel_all_runs()
        return {}

    def _load_config(self) -> None:
        try:
            self._config = load_config(workspace=self._workspace, config_path=self._config_path)
            self._startup_error = None
        except ConfigError as exc:
            self._config = None
            self._startup_error = str(exc)

    async def _execute_run(self, run: ActiveRun, *, resume: Any | None = None) -> None:
        await self._emit(run, "run/started", {"resumed": resume is not None})
        try:
            agent = await self._ensure_agent()
            if agent is None:
                if not self._allow_echo:
                    raise ConfigError(self._startup_error or "Agent is not configured")
                await self._emit(run, "message/delta", {"text": run.message})
            else:
                await self._stream_agent(agent, run, resume=resume)

            if run.status == "interrupted":
                return
            run.status = "completed"
            await self._emit(
                run,
                "run/completed",
                {
                    "usage": run.usage,
                    "duration_ms": round((time.monotonic() - run.started_at) * 1000),
                },
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            await self._emit(run, "run/cancelled", {"reason": "Cancelled by client"})
            raise
        except Exception as exc:
            run.status = "failed"
            logger.exception("Agent run failed: %s", run.run_id)
            await self._emit(run, "run/failed", {"code": type(exc).__name__, "message": str(exc)})
        finally:
            if run.status != "interrupted":
                self._runs.pop(run.thread_id, None)

    async def _ensure_agent(self) -> Any | None:
        if self.agent is not None:
            return self.agent
        self._load_config()
        if self._config is None or self._config.model is None:
            return None
        created = self._agent_factory(self._config, self._workspace)
        self.agent = await created if isinstance(created, Awaitable) else created
        return self.agent

    async def _create_default_agent(self, config: Za38Config, workspace: Path) -> Any:
        from za38_agent.agent import create_za38_agent
        from za38_agent.providers.za38_gateway import create_openai_compatible_model

        model = create_openai_compatible_model(config.require_model())
        return create_za38_agent(model, cwd=str(workspace), interactive=True)

    async def _stream_agent(self, agent: Any, run: ActiveRun, *, resume: Any | None) -> None:
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        stream_input: Any = Command(resume=resume) if resume is not None else {"messages": [HumanMessage(content=run.message)]}
        async for event in agent.astream(
            stream_input,
            config={"configurable": {"thread_id": run.thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
        ):
            for method, payload in self._translate_events(event, run):
                if method in {"approval/requested", "question/requested"}:
                    run.status = "interrupted"
                    run.interrupt_id = str(payload["interrupt_id"])
                await self._emit(run, method, payload)

    def _translate_events(self, event: tuple[Any, ...], run: ActiveRun) -> Iterable[tuple[str, dict[str, Any]]]:
        if len(event) == 3:
            _namespace, stream_mode, data = event
        elif len(event) == 2:
            stream_mode, data = event
        else:
            return []

        if stream_mode == "messages" and isinstance(data, tuple) and data:
            chunk = data[0]
            self._update_usage(run, getattr(chunk, "usage_metadata", None))
            content = _content_text(getattr(chunk, "content", None))
            events: list[tuple[str, dict[str, Any]]] = []
            if content:
                events.append(("message/delta", {"text": content}))
            for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
                tool_id = str(tool_chunk.get("id") or "")
                if tool_chunk.get("name"):
                    events.append(
                        (
                            "tool/started",
                            {"tool_id": tool_id, "tool_name": str(tool_chunk["name"]), "args": {}},
                        )
                    )
                if tool_chunk.get("args"):
                    events.append(("tool/updated", {"tool_id": tool_id, "chunk": str(tool_chunk["args"])}))
            if type(chunk).__name__ == "ToolMessage":
                events.append(
                    (
                        "tool/completed",
                        {
                            "tool_id": str(getattr(chunk, "tool_call_id", "")),
                            "result": _content_text(getattr(chunk, "content", None)),
                            "error": getattr(chunk, "status", None) == "error",
                        },
                    )
                )
            return events

        if stream_mode == "updates" and isinstance(data, Mapping):
            interrupts = data.get("__interrupt__")
            if interrupts:
                return self._translate_interrupts(interrupts)
        return []

    def _translate_interrupts(self, interrupts: object) -> Iterable[tuple[str, dict[str, Any]]]:
        """区分 AskUser 与 HITL 中断，并保留 LangGraph 分配的稳定 interrupt id。"""
        items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
        events: list[tuple[str, dict[str, Any]]] = []
        for interrupt in items:
            value = getattr(interrupt, "value", interrupt)
            interrupt_id = str(getattr(interrupt, "id", uuid.uuid4()))
            if isinstance(value, Mapping) and value.get("type") == "ask_user":
                questions = value.get("questions")
                if isinstance(questions, list) and questions:
                    first_question = questions[0] if isinstance(questions[0], Mapping) else {}
                    choices = first_question.get("choices", []) if isinstance(first_question, Mapping) else []
                    options = [
                        {"label": str(choice["value"]), "value": str(choice["value"])}
                        for choice in choices
                        if isinstance(choice, Mapping) and isinstance(choice.get("value"), str)
                    ]
                    events.append(
                        (
                            "question/requested",
                            {
                                "interrupt_id": interrupt_id,
                                "question": str(first_question.get("question", "Agent needs input")),
                                "options": options,
                                # 保留完整问题组，供支持多题表单的客户端按需渲染。
                                "questions": _json_safe(questions),
                            },
                        )
                    )
                    continue

            description = "A tool execution requires approval"
            if isinstance(value, Mapping):
                requests = value.get("action_requests")
                if isinstance(requests, list) and requests:
                    descriptions = [
                        str(request.get("description"))
                        for request in requests
                        if isinstance(request, Mapping) and request.get("description")
                    ]
                    if descriptions:
                        description = "\n\n".join(descriptions)
            events.append(
                (
                    "approval/requested",
                    {
                        "interrupt_id": interrupt_id,
                        "description": description,
                        "requests": _json_safe(value),
                    },
                )
            )
        return events

    async def _emit(self, run: ActiveRun, method: str, payload: dict[str, Any]) -> None:
        run.sequence += 1
        await self.send_notification(
            method,
            {"thread_id": run.thread_id, "run_id": run.run_id, "sequence": run.sequence, **payload},
        )

    def _require_run(self, params: Mapping[str, Any]) -> ActiveRun:
        thread_id = params.get("thread_id")
        run_id = params.get("run_id")
        run = self._runs.get(str(thread_id))
        if run is None or run.run_id != run_id:
            raise RpcError(-32004, "Run not found")
        return run

    def _update_usage(self, run: ActiveRun, usage: Any) -> None:
        if not isinstance(usage, Mapping):
            return
        run.usage["input_tokens"] = max(run.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0))
        run.usage["output_tokens"] = max(run.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0))

    async def _cancel_all_runs(self) -> None:
        tasks = [run.task for run in self._runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def _json_safe(value: object) -> object:
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value
