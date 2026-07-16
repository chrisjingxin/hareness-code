"""za38 v2 stdio JSON-RPC Peer：承载多运行控制面、统一事件和双向交互请求。"""

from __future__ import annotations

import asyncio
import inspect
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

from pydantic import ValidationError

from harness_agent import __version__
from harness_agent.config import ConfigError, Za38Config, load_config
from harness_agent.protocol_generated import (
    MAX_FRAME_BYTES,
    MAX_TOOL_PAYLOAD_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    SERVER_CAPABILITIES,
    ApprovalResponse,
    InitializeParams,
    QuestionResponse,
    RunCancelParams,
    RunStartParams,
)

logger = logging.getLogger(__name__)
INTERACTION_TIMEOUT_MS = 300_000


class RpcError(Exception):
    """可安全返回给客户端的预期 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        """保存错误码、文案和可选结构化详情。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(slots=True)
class ActiveRun:
    """一次执行的隔离状态；sequence 同时覆盖事件与反向请求。"""

    thread_id: str
    run_id: str
    message: str
    task: asyncio.Task[None] | None = None
    sequence: int = 0
    status: str = "running"
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    tool_stream_ids: dict[str, str] = field(default_factory=dict)
    tool_result_ids: dict[str, str] = field(default_factory=dict)
    started_tool_ids: set[str] = field(default_factory=set)
    last_tool_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class InteractionSpec:
    """从 LangGraph interrupt 规范化出的协议请求及恢复所需原始信息。"""

    request_id: str
    type: str
    payload: dict[str, Any]
    interrupt_id: str
    questions: list[Mapping[str, Any]] = field(default_factory=list)


AgentFactory = Callable[[Za38Config, Path], Any | Awaitable[Any]]


class JsonRpcServer:
    """管理 Python Agent 生命周期与 v2 双向 JSON-RPC stdio 控制面。"""

    def __init__(
        self,
        *,
        agent: Any | None = None,
        agent_factory: AgentFactory | None = None,
        allow_echo: bool | None = None,
    ) -> None:
        """初始化运行表、反向请求表、发送锁和方法分发表。"""
        self.agent = agent
        self._agent_factory = agent_factory or self._create_default_agent
        self._allow_echo = (
            os.environ.get("ZA38_ECHO_MODE") == "1" if allow_echo is None else allow_echo
        )
        self._running = True
        self._initialized = False
        self._send_lock = asyncio.Lock()
        self._runs: dict[str, ActiveRun] = {}
        self._pending_requests: dict[str, asyncio.Future[object]] = {}
        self._workspace = Path.cwd().resolve()
        self._config_path: str | None = None
        self._config: Za38Config | None = None
        self._startup_error: str | None = None
        self._enabled_capabilities: set[str] = set()
        self._handlers = {
            "initialize": self._handle_initialize,
            "run.start": self._handle_run_start,
            "run.cancel": self._handle_run_cancel,
            "config.show": self._handle_config_show,
            "config.path": self._handle_config_path,
            "shutdown": self._handle_shutdown,
        }

    async def run(self) -> None:
        """持续读取受限大小的 JSONL 帧，直到 EOF 或正常关闭。"""
        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES + 1)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            while self._running:
                try:
                    line = await reader.readline()
                except ValueError:
                    await self.send_error(None, -32600, "JSON-RPC frame exceeds size limit")
                    break
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self.send_error(None, -32600, "JSON-RPC frame exceeds size limit")
                    continue
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
            self._fail_pending_requests(RpcError(-32004, "Peer connection closed"))

    async def dispatch(self, message: dict[str, Any]) -> None:
        """校验并发消息；response 负责恢复反向请求，request 则进入业务分发。"""
        if message.get("jsonrpc") != "2.0":
            await self.send_error(message.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
            return
        method = message.get("method")
        if method is None:
            if set(message) - {"jsonrpc", "id", "result", "error"}:
                await self.send_error(message.get("id"), -32600, "Response contains unknown fields")
                return
            await self._handle_peer_response(message)
            return
        request_id = message.get("id")
        if not isinstance(request_id, str):
            await self.send_error(None, -32600, "Invalid Request: id must be a string")
            return
        if not isinstance(method, str):
            await self.send_error(request_id, -32600, "Invalid Request: method must be a string")
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            await self.send_error(request_id, -32602, "Invalid params: params must be an object")
            return
        if set(message) - {"jsonrpc", "method", "params", "id"}:
            await self.send_error(request_id, -32600, "Request contains unknown fields")
            return
        if method != "initialize" and not self._initialized:
            await self.send_error(request_id, -32000, "initialize must be the first request")
            return
        handler = self._handlers.get(method)
        if handler is None:
            await self.send_error(request_id, -32601, f"Method not found: {method}")
            return
        try:
            result = await handler(params, request_id)
        except ValidationError as exc:
            await self.send_error(request_id, -32602, "Invalid params", exc.errors(include_url=False))
        except RpcError as exc:
            await self.send_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - 最后的协议隔离层。
            logger.exception("Unhandled JSON-RPC handler error for %s", method)
            await self.send_error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        else:
            if result is not None:
                await self.send_response(request_id, result)

    async def send(self, message: dict[str, Any]) -> None:
        """串行写出单帧 JSON-RPC，并拒绝超限输出。"""
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_FRAME_BYTES:
            raise RpcError(-32603, "Outbound JSON-RPC frame exceeds size limit")
        async with self._send_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def send_response(self, request_id: str, result: Any) -> None:
        """发送 JSON-RPC 成功响应。"""
        await self.send({"jsonrpc": "2.0", "result": result, "id": request_id})

    async def send_error(
        self, request_id: str | None, code: int, message: str, data: object | None = None
    ) -> None:
        """发送保留 code/message/data 的 JSON-RPC 错误响应。"""
        error: dict[str, object] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self.send({"jsonrpc": "2.0", "error": error, "id": request_id})

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """发送无需响应的通知；v2 业务流只使用 event。"""
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_initialize(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """协商 v2 minor 和能力，并返回脱敏启动摘要。"""
        protocol = params.get("protocol")
        if not isinstance(protocol, dict) or protocol.get("major") != PROTOCOL_MAJOR:
            raise RpcError(-32003, "PROTOCOL_MISMATCH", {"supported_major": PROTOCOL_MAJOR})
        min_minor = protocol.get("min_minor")
        max_minor = protocol.get("max_minor")
        if not isinstance(min_minor, int) or not isinstance(max_minor, int) or not (min_minor <= PROTOCOL_MINOR <= max_minor):
            raise RpcError(
                -32003,
                "PROTOCOL_MISMATCH",
                {"supported": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}},
            )
        parsed = InitializeParams.model_validate(params)
        if self._initialized:
            raise RpcError(-32000, "Peer is already initialized")
        if parsed.cwd is not None:
            self._workspace = Path(parsed.cwd).expanduser().resolve()
        self._config_path = parsed.config_path
        self._load_config()
        requested = set(parsed.capabilities)
        self._enabled_capabilities = requested.intersection(SERVER_CAPABILITIES)
        self._initialized = True
        return {
            "protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
            "server": {"name": "za38-agent", "version": __version__},
            "server_capabilities": list(SERVER_CAPABILITIES),
            "enabled_capabilities": sorted(self._enabled_capabilities),
            "agent_commands": [],
            "limits": {
                "max_frame_bytes": MAX_FRAME_BYTES,
                "max_tool_payload_bytes": MAX_TOOL_PAYLOAD_BYTES,
            },
            "config_summary": self._config.redacted() if self._config else None,
            "startup_error": (
                {"code": "CONFIGURATION_ERROR", "message": self._startup_error}
                if self._startup_error
                else None
            ),
        }

    async def _handle_run_start(self, params: dict[str, Any], request_id: str) -> None:
        """先确认 run 标识再创建后台任务，保证响应严格早于首事件。"""
        parsed = RunStartParams.model_validate(params)
        message = parsed.message.strip()
        if not message:
            raise RpcError(-32602, "message must be non-empty")
        thread_id = parsed.thread_id or str(uuid.uuid4())
        run_id = parsed.run_id or str(uuid.uuid4())
        existing = self._runs.get(thread_id)
        if existing and existing.status in {"running", "interrupted"}:
            raise RpcError(-32000, f"Thread {thread_id} already has an active run")
        run = ActiveRun(thread_id=thread_id, run_id=run_id, message=message)
        self._runs[thread_id] = run
        await self.send_response(
            request_id, {"thread_id": thread_id, "run_id": run_id, "accepted": True}
        )
        run.task = asyncio.create_task(self._execute_run(run), name=f"za38-run-{run_id}")
        return None

    async def _handle_run_cancel(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """取消运行，包括正在等待客户端交互的任务。"""
        parsed = RunCancelParams.model_validate(params)
        run = self._require_run(parsed.thread_id, parsed.run_id)
        if run.task and not run.task.done():
            run.task.cancel()
            # create_task 尚未获得首个时间片时，协程内部的 CancelledError 分支不会执行；
            # 这里补发唯一终态，保证“刚接受就取消”也不会让客户端永久等待。
            await asyncio.sleep(0)
            if run.status not in {"cancelled", "completed", "failed"} and run.task.cancelled():
                run.status = "cancelled"
                await self._emit(run, "run.cancelled", {"reason": "Cancelled by client"})
                self._runs.pop(run.thread_id, None)
            return {"cancelled": True, "run_id": run.run_id}
        return {"cancelled": False, "run_id": run.run_id}

    async def _handle_config_show(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回当前脱敏配置。"""
        if _params:
            raise RpcError(-32602, "config.show does not accept params")
        self._load_config()
        if self._config is None:
            raise RpcError(-32010, self._startup_error or "Configuration is unavailable")
        return self._config.redacted()

    async def _handle_config_path(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回配置合并路径。"""
        if _params:
            raise RpcError(-32602, "config.path does not accept params")
        self._load_config()
        return {
            "workspace": str(self._workspace),
            "paths": [str(path) for path in self._config.paths] if self._config else [],
            "explicit_path": self._config_path,
        }

    async def _handle_shutdown(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """停止读取循环并取消全部运行。"""
        if _params:
            raise RpcError(-32602, "shutdown does not accept params")
        self._running = False
        await self._cancel_all_runs()
        return {}

    def _load_config(self) -> None:
        """刷新配置缓存，并保存用户可修复的错误。"""
        try:
            self._config = load_config(workspace=self._workspace, config_path=self._config_path)
            self._startup_error = None
        except ConfigError as exc:
            self._config = None
            self._startup_error = str(exc)

    async def _execute_run(self, run: ActiveRun) -> None:
        """执行并自动恢复中断，保证每个 run 只产生一个终态。"""
        await self._emit(run, "run.started", {"resumed": False})
        resume: Any | None = None
        try:
            agent = await self._ensure_agent()
            if agent is None:
                if not self._allow_echo:
                    raise ConfigError(self._startup_error or "Agent is not configured")
                await self._emit(run, "content.delta", {"text": run.message})
            else:
                while True:
                    resume = await self._stream_agent(agent, run, resume=resume)
                    if resume is None:
                        break
            run.status = "completed"
            await self._emit(
                run,
                "run.completed",
                {
                    "usage": run.usage,
                    "duration_ms": round((time.monotonic() - run.started_at) * 1000),
                    "finish_reason": "completed",
                },
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            await self._emit(run, "run.cancelled", {"reason": "Cancelled by client"})
        except Exception as exc:
            run.status = "failed"
            logger.exception("Agent run failed: %s", run.run_id)
            await self._emit(
                run,
                "run.failed",
                {
                    "error": {
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "retryable": False,
                    }
                },
            )
        finally:
            self._runs.pop(run.thread_id, None)

    async def _ensure_agent(self) -> Any | None:
        """按需构建并缓存 Agent；Echo 测试模式不读取或初始化真实模型。"""
        # Echo 只用于协议测试。即使当前目录恰好存在模型配置，也必须保持
        # 无网络、无凭据依赖的确定性行为，避免测试机器环境改变结果。
        if self.agent is not None:
            return self.agent
        if self._allow_echo:
            return None
        self._load_config()
        if self._config is None or self._config.model is None:
            return None
        created = self._agent_factory(self._config, self._workspace)
        self.agent = await created if inspect.isawaitable(created) else created
        return self.agent

    async def _create_default_agent(self, config: Za38Config, workspace: Path) -> Any:
        """使用 OpenAI 模型与显式选择的本机或远端后端创建 deepagents 图。"""
        from harness_agent.agent import create_harness_agent
        from harness_agent.execution import create_execution_context
        from harness_agent.providers.harness_gateway import create_openai_compatible_model

        execution_context = create_execution_context(config.execution, workspace)
        return create_harness_agent(
            create_openai_compatible_model(config.require_model()),
            cwd=str(workspace),
            interactive=True,
            execution_context=execution_context,
        )

    async def _stream_agent(self, agent: Any, run: ActiveRun, *, resume: Any | None) -> Any | None:
        """把 LangGraph 双流转换为领域事件；遇到 interrupt 时等待客户端并返回恢复值。"""
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        stream_input: Any = (
            Command(resume=resume)
            if resume is not None
            else {"messages": [HumanMessage(content=run.message)]}
        )
        async for event in agent.astream(
            stream_input,
            config={"configurable": {"thread_id": run.thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
        ):
            interaction = self._extract_interaction(event)
            if interaction is not None:
                run.status = "interrupted"
                response = await self._request_interaction(run, interaction)
                run.status = "running"
                await self._emit(
                    run,
                    "interaction.resolved",
                    {"request_id": interaction.request_id, "type": interaction.type},
                )
                return self._resume_value(interaction, response)
            for event_type, payload in self._translate_stream_event(event, run):
                await self._emit(run, event_type, payload)
        return None

    def _translate_stream_event(
        self, event: tuple[Any, ...], run: ActiveRun
    ) -> Iterable[tuple[str, dict[str, Any]]]:
        """翻译文本和工具分片，不让 LangChain 对象跨越协议边界。"""
        if len(event) == 3:
            _namespace, stream_mode, data = event
        elif len(event) == 2:
            stream_mode, data = event
        else:
            return []
        if stream_mode != "messages" or not isinstance(data, tuple) or not data:
            return []
        chunk = data[0]
        self._update_usage(run, getattr(chunk, "usage_metadata", None))
        events: list[tuple[str, dict[str, Any]]] = []
        # dcode 以 LangChain 规范化后的 content_blocks 为准；部分 OpenAI 兼容网关
        # 会在流式首轮只填充该属性，直接读取 content 会产生“有 token、无正文”。
        content = _message_text(chunk)
        if content and type(chunk).__name__ != "ToolMessage":
            events.append(("content.delta", {"text": content}))
        for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
            tool_id = self._resolve_tool_stream_id(run, tool_chunk)
            if tool_chunk.get("name") and tool_id not in run.started_tool_ids:
                run.started_tool_ids.add(tool_id)
                events.append(
                    ("tool.started", {"tool_call_id": tool_id, "name": str(tool_chunk["name"])})
                )
            if tool_chunk.get("args"):
                arguments = _truncate_text(str(tool_chunk["args"]))
                events.append(
                    (
                        "tool.delta",
                        {
                            "tool_call_id": tool_id,
                            "arguments_delta": arguments[0],
                            "truncated": arguments[1],
                            "original_bytes": arguments[2],
                        },
                    )
                )
        if type(chunk).__name__ == "ToolMessage":
            result = _truncate_text(_content_text(getattr(chunk, "content", None)))
            result_id = str(getattr(chunk, "tool_call_id", "") or "")
            tool_id = run.tool_result_ids.get(result_id, result_id) or run.last_tool_id or f"tool-{run.run_id}"
            events.append(
                (
                    "tool.completed",
                    {
                        "tool_call_id": tool_id,
                        "result": {
                            "content": result[0],
                            "is_error": getattr(chunk, "status", None) == "error",
                            "truncated": result[1],
                            "original_bytes": result[2],
                        },
                    },
                )
            )
        return events

    def _resolve_tool_stream_id(self, run: ActiveRun, chunk: Mapping[str, Any]) -> str:
        """用流式 index 关联缺少 id/name 的工具参数分片。"""
        index = chunk.get("index")
        raw_id = str(chunk.get("id") or "")
        key = f"index:{index}" if index is not None else f"id:{raw_id}" if raw_id else "current"
        tool_id = run.tool_stream_ids.get(key)
        if tool_id is None:
            tool_id = raw_id or f"tool-{run.run_id}-{len(run.tool_stream_ids)}"
            run.tool_stream_ids[key] = tool_id
        if raw_id:
            run.tool_result_ids[raw_id] = tool_id
        run.last_tool_id = tool_id
        return tool_id

    def _extract_interaction(self, event: tuple[Any, ...]) -> InteractionSpec | None:
        """从 updates 流提取首个 AskUser 或 HITL interrupt。"""
        if len(event) == 3:
            _namespace, stream_mode, data = event
        elif len(event) == 2:
            stream_mode, data = event
        else:
            return None
        if stream_mode != "updates" or not isinstance(data, Mapping):
            return None
        interrupts = data.get("__interrupt__")
        if not interrupts:
            return None
        interrupt = (interrupts if isinstance(interrupts, (list, tuple)) else [interrupts])[0]
        value = getattr(interrupt, "value", interrupt)
        interrupt_id = str(getattr(interrupt, "id", uuid.uuid4()))
        if isinstance(value, Mapping) and value.get("type") == "ask_user":
            raw_questions = value.get("questions")
            questions = [q for q in raw_questions or [] if isinstance(q, Mapping)]
            normalized = []
            for index, question in enumerate(questions):
                options = [
                    {"label": str(choice.get("value", "")), "value": str(choice.get("value", "")), "description": ""}
                    for choice in question.get("choices", [])
                    if isinstance(choice, Mapping) and choice.get("value")
                ]
                normalized.append(
                    {
                        "id": f"question-{index + 1}",
                        "question": str(question.get("question", "Agent needs input")),
                        "header": "",
                        "body": "",
                        "options": options,
                        "multi_select": False,
                        "allow_other": True,
                    }
                )
            return InteractionSpec(
                request_id=interrupt_id,
                type="question",
                payload={"interrupt_id": interrupt_id, "questions": normalized},
                interrupt_id=interrupt_id,
                questions=questions,
            )
        description = "A tool execution requires approval"
        if isinstance(value, Mapping):
            descriptions = [
                str(request.get("description"))
                for request in value.get("action_requests", [])
                if isinstance(request, Mapping) and request.get("description")
            ]
            if descriptions:
                description = "\n\n".join(descriptions)
        return InteractionSpec(
            request_id=interrupt_id,
            type="approval",
            payload={
                "interrupt_id": interrupt_id,
                "description": description,
                "requests": _bounded_json(value),
                "decisions": ["approve_once", "reject"],
            },
            interrupt_id=interrupt_id,
        )

    async def _request_interaction(self, run: ActiveRun, spec: InteractionSpec) -> object:
        """发送反向 JSON-RPC request；异常时审批拒绝、问答取消。"""
        required_capability = f"interactive.{spec.type}"
        if required_capability not in self._enabled_capabilities:
            logger.info("Interaction %s disabled by capability negotiation", spec.type)
            return (
                {"type": "approval", "request_id": spec.request_id, "decision": "reject"}
                if spec.type == "approval"
                else {"type": "question", "request_id": spec.request_id, "answers": {}}
            )
        run.sequence += 1
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending_requests[spec.request_id] = future
        await self.send(
            {
                "jsonrpc": "2.0",
                "method": "request",
                "id": spec.request_id,
                "params": {
                    "request_id": spec.request_id,
                    "type": spec.type,
                    "thread_id": run.thread_id,
                    "run_id": run.run_id,
                    "sequence": run.sequence,
                    "timeout_ms": INTERACTION_TIMEOUT_MS,
                    "payload": spec.payload,
                },
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=INTERACTION_TIMEOUT_MS / 1000)
        except (TimeoutError, RpcError, ValidationError) as exc:
            logger.warning("Interaction %s failed closed: %s", spec.request_id, exc)
            return (
                {"type": "approval", "request_id": spec.request_id, "decision": "reject"}
                if spec.type == "approval"
                else {"type": "question", "request_id": spec.request_id, "answers": {}}
            )
        finally:
            self._pending_requests.pop(spec.request_id, None)

    async def _handle_peer_response(self, message: dict[str, Any]) -> None:
        """用客户端 response 解析并恢复对应交互 Future。"""
        request_id = message.get("id")
        if not isinstance(request_id, str):
            await self.send_error(None, -32600, "Response id must be a string")
            return
        future = self._pending_requests.get(request_id)
        if future is None or future.done():
            await self.send_error(request_id, -32004, "REQUEST_EXPIRED")
            return
        if "error" in message:
            error = message.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
                future.set_exception(RpcError(-32600, "Invalid JSON-RPC error response"))
                return
            detail = error["message"]
            future.set_exception(RpcError(-32004, str(detail)))
            return
        if "result" not in message:
            future.set_exception(RpcError(-32600, "Response must contain result or error"))
            return
        result = message.get("result")
        try:
            if isinstance(result, dict) and result.get("type") == "approval":
                parsed: object = ApprovalResponse.model_validate(result).model_dump()
            elif isinstance(result, dict) and result.get("type") == "question":
                parsed = QuestionResponse.model_validate(result).model_dump()
            else:
                raise ValueError("Unknown interaction response type")
        except (ValidationError, ValueError) as exc:
            future.set_exception(RpcError(-32602, f"Invalid interaction response: {exc}"))
            return
        if parsed["request_id"] != request_id:  # type: ignore[index]
            future.set_exception(RpcError(-32602, "Response request_id mismatch"))
            return
        future.set_result(parsed)

    def _resume_value(self, spec: InteractionSpec, response: object) -> dict[str, object]:
        """将语言无关交互结果映射回 LangGraph interrupt resume 契约。"""
        assert isinstance(response, dict)
        if spec.type == "approval":
            decision = response.get("decision")
            langgraph_decision = "approve" if decision in {"approve_once", "approve_session"} else "reject"
            return {spec.interrupt_id: {"decisions": [{"type": langgraph_decision}]}}
        answers_by_id = response.get("answers", {})
        answers: list[str] = []
        if isinstance(answers_by_id, Mapping):
            for index, _question in enumerate(spec.questions):
                values = answers_by_id.get(f"question-{index + 1}", [])
                answers.append(str(values[0]) if isinstance(values, list) and values else "")
        status = "answered" if any(answers) else "cancelled"
        return {spec.interrupt_id: {"status": status, "answers": answers}}

    async def _emit(self, run: ActiveRun, event_type: str, payload: dict[str, Any]) -> None:
        """生成统一事件信封，并为同一运行单调递增 sequence。"""
        run.sequence += 1
        await self.send_notification(
            "event",
            {
                "event_id": str(uuid.uuid4()),
                "type": event_type,
                "thread_id": run.thread_id,
                "run_id": run.run_id,
                "sequence": run.sequence,
                "timestamp_ms": int(time.time() * 1000),
                "source": {"kind": "root"},
                "payload": payload,
            },
        )

    def _require_run(self, thread_id: str, run_id: str) -> ActiveRun:
        """拒绝过期或跨线程控制请求。"""
        run = self._runs.get(thread_id)
        if run is None or run.run_id != run_id:
            raise RpcError(-32001, "RUN_NOT_FOUND")
        return run

    def _update_usage(self, run: ActiveRun, usage: Any) -> None:
        """合并分片 usage，避免流式计数回退。"""
        if not isinstance(usage, Mapping):
            return
        run.usage["input_tokens"] = max(
            run.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0)
        )
        run.usage["output_tokens"] = max(
            run.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0)
        )

    async def _cancel_all_runs(self) -> None:
        """取消全部运行并等待 finally 清理。"""
        tasks = [run.task for run in self._runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()

    def _fail_pending_requests(self, error: Exception) -> None:
        """连接退出时解除所有交互等待，避免后台任务泄漏。"""
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()


def _truncate_text(value: str) -> tuple[str, bool, int]:
    """按 UTF-8 字节安全截断工具输出，并保留原始大小。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return value, False, len(encoded)
    clipped = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return clipped, True, len(encoded)


def _content_text(content: object) -> str:
    """提取 LangChain 内容字段中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return "" if content is None else str(content)


def _message_text(message: object) -> str:
    """优先从 LangChain 标准内容块提取正文，并兼容旧式 content 字段。"""
    blocks = getattr(message, "content_blocks", None)
    if isinstance(blocks, list):
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        if text:
            return text
    return _content_text(getattr(message, "content", None))


def _json_safe(value: object) -> object:
    """确保中断详情可 JSON 编码，复杂对象降级为字符串。"""
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _bounded_json(value: object) -> object:
    """限制交互详情的 JSON 字节数，避免审批参数撑爆 stdio 与 TUI。"""
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
