"""za38 v2 stdio JSON-RPC Peer：承载多运行控制面、统一事件和双向交互请求。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import threading
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
    ContextCompactParams,
    InitializeParams,
    QuestionResponse,
    RunCancelParams,
    RunStartParams,
    ThreadsListParams,
    ThreadsOpenParams,
)
from harness_agent.skills import SkillError, SkillRegistry
from harness_agent.thread_store import ThreadStore, ThreadStoreError

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
    requested_skill: dict[str, str] | None = None
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
    context_summary: dict[str, object] = field(default_factory=dict)


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
        config_home: Path | None = None,
    ) -> None:
        """初始化运行表、反向请求表、发送锁和方法分发表。

        ``config_home`` 仅供嵌入式测试隔离用户目录；正式 CLI 始终使用
        操作系统解析出的真实 home，不能由 JSON-RPC 客户端传入。
        """
        self.agent = agent
        self._agent_factory = agent_factory or self._create_default_agent
        self._uses_default_agent_factory = agent_factory is None and agent is None
        self._thread_agents: dict[str, Any] = {}
        self._context_updates: dict[str, list[Any]] = {}
        self._context_middlewares: dict[str, Any] = {}
        self._allow_echo = (
            os.environ.get("HARNESS_ECHO_MODE") == "1" if allow_echo is None else allow_echo
        )
        self._running = True
        self._initialized = False
        self._send_lock = asyncio.Lock()
        self._runs: dict[str, ActiveRun] = {}
        self._pending_requests: dict[str, asyncio.Future[object]] = {}
        self._workspace = Path.cwd().resolve()
        self._config_path: str | None = None
        self._config_home = config_home
        self._config: Za38Config | None = None
        self._startup_error: str | None = None
        self._skill_registry: SkillRegistry | None = None
        self._thread_store: ThreadStore | None = None
        self._protocol_minor = PROTOCOL_MINOR
        self._enabled_capabilities: set[str] = set()
        self._handlers = {
            "initialize": self._handle_initialize,
            "run.start": self._handle_run_start,
            "run.cancel": self._handle_run_cancel,
            "context.compact": self._handle_context_compact,
            "config.show": self._handle_config_show,
            "config.path": self._handle_config_path,
            "threads.list": self._handle_threads_list,
            "threads.open": self._handle_threads_open,
            "skills.list": self._handle_skills_list,
            "skills.inspect": self._handle_skills_inspect,
            "skills.set_enabled": self._handle_skills_set_enabled,
            "skills.install": self._handle_skills_install,
            "skills.update": self._handle_skills_update,
            "skills.remove": self._handle_skills_remove,
            "skills.market.list": self._handle_skills_market_list,
            "shutdown": self._handle_shutdown,
        }

    async def run(self) -> None:
        """持续读取受限大小的 JSONL 帧，直到 EOF 或正常关闭。"""
        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES + 1)
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            # Windows ProactorEventLoop 对重定向 stdin 句柄注册 IOCP 会抛 WinError 6，
            # 改用后台线程阻塞读取并喂入 StreamReader，保持分帧逻辑不变。
            def _feed_stdin() -> None:
                stdin = getattr(sys.stdin, "buffer", sys.stdin)
                try:
                    while True:
                        chunk = stdin.readline()
                        if not chunk:
                            break
                        loop.call_soon_threadsafe(reader.feed_data, chunk)
                except Exception:
                    pass
                try:
                    loop.call_soon_threadsafe(reader.feed_eof)
                except RuntimeError:
                    # 事件循环已关闭；stdin 线程退出即可。
                    pass

            threading.Thread(target=_feed_stdin, name="za38-stdin", daemon=True).start()
        else:
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
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
            await self._close_thread_store()

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
        except SkillError as exc:
            await self.send_error(request_id, -32602, str(exc))
        except ThreadStoreError as exc:
            await self.send_error(
                request_id,
                -32020,
                "THREAD_STORE_UNAVAILABLE",
                {"code": str(exc)},
            )
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
        if (
            not isinstance(min_minor, int)
            or not isinstance(max_minor, int)
            or max_minor < 0
            or min_minor > max_minor
            or min_minor > PROTOCOL_MINOR
        ):
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
        self._protocol_minor = min(PROTOCOL_MINOR, max_minor)
        self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
        self._config_path = parsed.config_path
        self._load_config()
        requested = set(parsed.capabilities)
        self._enabled_capabilities = requested.intersection(SERVER_CAPABILITIES)
        self._initialized = True
        return {
            "protocol": {"major": PROTOCOL_MAJOR, "minor": self._protocol_minor},
            "server": {"name": "za38-agent", "version": __version__},
            "server_capabilities": list(SERVER_CAPABILITIES),
            "enabled_capabilities": sorted(self._enabled_capabilities),
            "agent_commands": [],
            "skills_snapshot": self._skill_registry.snapshot(),
            "skill_diagnostics": self._skill_registry.diagnostics[:20],
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
        requested_skill = None
        if parsed.requested_skill is not None:
            if self._skill_registry is None:
                self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
            skill = self._skill_registry.resolve(parsed.requested_skill.id)
            if not skill.user_invocable:
                raise SkillError(f'Skill "{skill.skill_id}" is not user-invocable')
            requested_skill = {"id": skill.skill_id, "args": parsed.requested_skill.args or ""}
        run = ActiveRun(
            thread_id=thread_id,
            run_id=run_id,
            message=message,
            requested_skill=requested_skill,
        )
        if self._thread_persistence_enabled():
            store = await self._ensure_thread_store()
            await store.record_message(thread_id, message)
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

    async def _handle_context_compact(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """在空闲 thread 上按用户命令强制生成结构化摘要，不把能力暴露给模型。"""
        self._require_context_capability()
        parsed = ContextCompactParams.model_validate(params)
        active = self._runs.get(parsed.thread_id)
        if active is not None and active.status in {"running", "interrupted"}:
            raise RpcError(-32000, "CONTEXT_COMPACTION_RUN_ACTIVE")

        store = await self._ensure_thread_store()
        messages = await store.load_context_messages(parsed.thread_id)
        if messages is None:
            raise RpcError(-32004, "THREAD_NOT_RECOVERABLE")
        agent = await self._ensure_agent(parsed.thread_id)
        middleware = self._context_middlewares.get(parsed.thread_id)
        if agent is None or middleware is None:
            raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")

        compacted, update, rewritten = await middleware.compact_now(parsed.thread_id, messages)
        # `compact_now` 复用运行期状态缓冲；当前请求直接返回结果，因此必须消费，
        # 防止下一次 Agent run 重复发出过期的 context.updated 事件。
        middleware.consume_updates(parsed.thread_id)
        if rewritten:
            from langchain_core.messages import RemoveMessage
            from langgraph.graph.message import REMOVE_ALL_MESSAGES

            await agent.aupdate_state(
                # CompiledStateGraph 将非空 checkpoint_ns 解释为子图路径；项目隔离
                # 由 ProjectScopedAsyncSqliteSaver 在根图空 namespace 上自动补齐。
                {"configurable": {"thread_id": parsed.thread_id}},
                {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]},
                as_node="model",
            )
            await store.refresh_thread(parsed.thread_id)
        return {"compacted": rewritten, "context": update.payload()}

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

    async def _handle_threads_list(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """返回当前 project 内最近活跃的 thread；thread_id 仅供客户端内部打开。"""
        self._require_threads_capability()
        parsed = ThreadsListParams.model_validate(params)
        threads = await (await self._ensure_thread_store()).list_threads(parsed.limit)
        return {"threads": [_thread_summary_payload(thread) for thread in threads]}

    async def _handle_threads_open(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """读取当前 project 的一个 thread 历史，拒绝跨 project 或无 checkpoint 记录。"""
        self._require_threads_capability()
        parsed = ThreadsOpenParams.model_validate(params)
        try:
            opened = await (await self._ensure_thread_store()).open_thread(parsed.thread_id)
        except ThreadStoreError as exc:
            if str(exc) in {"THREAD_NOT_FOUND", "THREAD_NOT_RECOVERABLE"}:
                raise RpcError(-32004, str(exc)) from exc
            raise
        return {
            "thread": _thread_summary_payload(opened.summary),
            "messages": [_thread_message_payload(message) for message in opened.messages],
        }

    def _require_skills(self) -> SkillRegistry:
        """返回初始化时建立的 Skill registry。"""
        if self._skill_registry is None:
            self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
        return self._skill_registry

    @staticmethod
    def _reject_params(params: Mapping[str, Any], allowed: set[str], method: str) -> None:
        """拒绝管理接口的未知字段，避免 CLI 拼写错误被静默忽略。"""
        unknown = set(params) - allowed
        if unknown:
            raise RpcError(-32602, f"{method} contains unsupported fields: {', '.join(sorted(unknown))}")

    async def _handle_skills_list(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回当前 catalog 的摘要、快照 ID 和诊断。"""
        self._reject_params(params, {"include_disabled"}, "skills.list")
        include_disabled = params.get("include_disabled", True)
        if not isinstance(include_disabled, bool):
            raise RpcError(-32602, "include_disabled must be boolean")
        registry = self._require_skills()
        return {
            "snapshot": registry.snapshot(),
            "skills": registry.list(include_disabled=include_disabled),
            "diagnostics": registry.diagnostics[:20],
        }

    async def _handle_skills_inspect(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回一个 Skill 的安全元数据。"""
        self._reject_params(params, {"id"}, "skills.inspect")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        return self._require_skills().inspect(skill_id)

    async def _handle_skills_set_enabled(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """保存下一次 thread 生效的 Skill 启停偏好。"""
        self._reject_params(params, {"id", "enabled"}, "skills.set_enabled")
        skill_id = params.get("id")
        enabled = params.get("enabled")
        if not isinstance(skill_id, str) or not skill_id.strip() or not isinstance(enabled, bool):
            raise RpcError(-32602, "id and enabled are required")
        return self._require_skills().set_enabled(skill_id, enabled)

    async def _handle_skills_market_list(self, params: dict[str, Any], _id: str) -> list[dict[str, object]]:
        """列出已安装的企业市场 Provider 或其 catalog。"""
        self._reject_params(params, {"market"}, "skills.market.list")
        market = params.get("market")
        if market is not None and not isinstance(market, str):
            raise RpcError(-32602, "market must be a string")
        return await self._require_skills().marketplace_catalog(market)

    async def _handle_skills_install(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 安装 Skill；Provider 不存在时返回明确错误。"""
        self._reject_params(params, {"market", "name", "version"}, "skills.install")
        market, name, version = params.get("market"), params.get("name"), params.get("version")
        if not isinstance(market, str) or not isinstance(name, str) or (version is not None and not isinstance(version, str)):
            raise RpcError(-32602, "market and name are required strings")
        return await self._require_skills().install(market, name, version)

    async def _handle_skills_update(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 更新市场 Skill。"""
        return await self._handle_skills_install(params, _id)

    async def _handle_skills_remove(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """移除一个已安装市场 Skill。"""
        self._reject_params(params, {"id"}, "skills.remove")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        return self._require_skills().remove(skill_id)

    async def _handle_shutdown(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """停止读取循环并取消全部运行。"""
        if _params:
            raise RpcError(-32602, "shutdown does not accept params")
        self._running = False
        await self._cancel_all_runs()
        await self._close_thread_store()
        return {}

    def _load_config(self) -> None:
        """刷新配置缓存，并保存用户可修复的错误。"""
        try:
            self._config = load_config(
                workspace=self._workspace,
                config_path=self._config_path,
                home=self._config_home,
            )
            self._startup_error = None
        except ConfigError as exc:
            self._config = None
            self._startup_error = str(exc)

    async def _execute_run(self, run: ActiveRun) -> None:
        """执行并自动恢复中断，保证每个 run 只产生一个终态。"""
        await self._emit(
            run,
            "run.started",
            {
                "resumed": False,
                "skills_snapshot_id": self._skill_registry.snapshot_id if self._skill_registry else None,
            },
        )
        resume: Any | None = None
        try:
            if run.requested_skill is not None:
                registry = self._require_skills()
                loaded = registry.load(run.requested_skill["id"], run.requested_skill.get("args", ""))
                await self._emit(
                    run,
                    "skill.loaded",
                    {
                        "skill_id": loaded.record.skill_id,
                        "source": loaded.record.source,
                        "version": loaded.record.version,
                        "snapshot_id": registry.snapshot_id,
                    },
                )
                run.message = (
                    f"The user explicitly selected Skill `{loaded.record.skill_id}`. "
                    f"Read `/.harness/skills/{loaded.record.skill_id}/SKILL.md` with read_file before using it.\n\n"
                    f"User request:\n{run.message}"
                )
            agent = await self._ensure_agent(run.thread_id)
            if agent is None:
                if not self._allow_echo:
                    raise ConfigError(self._startup_error or "Agent is not configured")
                await self._emit(run, "content.delta", {"text": run.message})
            else:
                while True:
                    resume = await self._stream_agent(agent, run, resume=resume)
                    if resume is None:
                        break
            if self._thread_store is not None:
                await self._thread_store.refresh_thread(run.thread_id)
            await self._drain_context_updates(run)
            run.status = "completed"
            await self._emit(
                run,
                "run.completed",
                {
                    "usage": run.usage,
                    "duration_ms": round((time.monotonic() - run.started_at) * 1000),
                    "finish_reason": "completed",
                    "context": run.context_summary,
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
            if self._thread_store is not None and run.status != "completed":
                try:
                    await self._thread_store.refresh_thread(run.thread_id)
                except ThreadStoreError:
                    logger.exception("Unable to refresh checkpoint index for thread %s", run.thread_id)
            self._runs.pop(run.thread_id, None)

    async def _ensure_agent(self, thread_id: str) -> Any | None:
        """按需构建 Agent；默认图按 thread 固定 epoch，外部注入图保持原有单实例契约。"""
        # Echo 只用于协议测试。即使当前目录恰好存在模型配置，也必须保持
        # 无网络、无凭据依赖的确定性行为，避免测试机器环境改变结果。
        if self.agent is not None:
            return self.agent
        if self._allow_echo:
            return None
        self._load_config()
        if self._config is None or self._config.model is None:
            return None
        if self._uses_default_agent_factory:
            cached = self._thread_agents.get(thread_id)
            if cached is not None:
                return cached
            created = self._create_default_agent(self._config, self._workspace, thread_id)
            agent = await created if inspect.isawaitable(created) else created
            self._thread_agents[thread_id] = agent
            return agent
        created = self._agent_factory(self._config, self._workspace)
        self.agent = await created if inspect.isawaitable(created) else created
        return self.agent

    async def _create_default_agent(self, config: Za38Config, workspace: Path, thread_id: str) -> Any:
        """使用 OpenAI 模型与显式选择的本机或远端后端创建 deepagents 图。"""
        from harness_agent.agent import create_harness_agent, create_prompt_epoch
        from harness_agent.execution import create_execution_context
        from harness_agent.providers.harness_gateway import create_openai_compatible_model

        execution_context = create_execution_context(config.execution, workspace)
        store = await self._ensure_thread_store()
        checkpointer = store.checkpointer
        epoch = await store.get_prompt_epoch(thread_id)
        if epoch is None:
            epoch = create_prompt_epoch(
                thread_id=thread_id,
                system_prompt=None,
                workspace=str(getattr(execution_context, "workspace_path", workspace)),
                sandboxed=bool(getattr(execution_context, "sandboxed", False)),
                provider=getattr(execution_context, "provider", None),
                approval_mode=config.execution.approval_mode,
                skill_registry=self._skill_registry,
                enable_memory=True,
                enable_skills=True,
            )
            await store.save_prompt_epoch(epoch)
        return create_harness_agent(
            create_openai_compatible_model(config.require_model()),
            cwd=str(workspace),
            # 无头客户端不协商 question 能力时不注册 ask_user；审批仍由
            # `_request_interaction` 在缺少 approval 能力时 fail closed。
            interactive="interactive.question" in self._enabled_capabilities,
            approval_mode=config.execution.approval_mode,
            execution_context=execution_context,
            skill_registry=self._skill_registry,
            checkpointer=checkpointer,
            prompt_epoch=epoch,
            thread_store=store,
            context_updates=self._context_updates,
            context_middlewares=self._context_middlewares,
            context_window_tokens=config.require_model().context_window_tokens,
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
            config=(self._thread_store.graph_config(run.thread_id) if self._thread_store is not None else {"configurable": {"thread_id": run.thread_id}}),
            stream_mode=["messages", "updates"],
            subgraphs=True,
        ):
            await self._drain_context_updates(run)
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

    async def _drain_context_updates(self, run: ActiveRun) -> None:
        """把中间件的预算状态转成顺序化事件；网关未返回缓存 usage 时保持 unknown。"""
        updates = self._context_updates.pop(run.thread_id, [])
        for update in updates:
            payload = update.payload() if hasattr(update, "payload") else dict(update)
            run.context_summary = payload
            await self._emit(run, "context.updated", payload)

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
        """用真实调用 ID 优先关联工具分片，并为缺失 ID 的续片保留 index 映射。"""
        index = chunk.get("index")
        raw_id = str(chunk.get("id") or "")
        if raw_id:
            # LangChain 每轮模型响应都会从 index=0 重新编号。真实 id 到达时必须
            # 覆盖该临时映射，否则第二轮工具会回写第一轮的卡片和执行结果。
            tool_id = run.tool_result_ids.get(raw_id, raw_id)
            run.tool_result_ids[raw_id] = tool_id
            run.tool_stream_ids[f"id:{raw_id}"] = tool_id
            if index is not None:
                run.tool_stream_ids[f"index:{index}"] = tool_id
        else:
            key = f"index:{index}" if index is not None else "current"
            tool_id = run.tool_stream_ids.get(key)
            if tool_id is None:
                tool_id = f"tool-{run.run_id}-{len(run.tool_stream_ids)}"
                run.tool_stream_ids[key] = tool_id
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
            langgraph_decision = "approve" if decision in {"approve_once", "approve_thread"} else "reject"
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

    def _threads_enabled(self) -> bool:
        """只有协商了读取能力的交互客户端才启用可恢复 thread 存储。"""
        return "threads.read" in self._enabled_capabilities and not self._allow_echo

    def _thread_persistence_enabled(self) -> bool:
        """默认生产图始终持久化 thread；外部注入图保持测试/嵌入调用的无存储契约。"""
        return not self._allow_echo and self._uses_default_agent_factory

    def _require_threads_capability(self) -> None:
        """阻止未协商读取能力的客户端意外读取本地 thread 数据。"""
        if "threads.read" not in self._enabled_capabilities:
            raise RpcError(-32002, "THREADS_CAPABILITY_REQUIRED")
        if self._allow_echo:
            raise RpcError(-32002, "THREADS_UNAVAILABLE_IN_ECHO_MODE")

    def _require_context_capability(self) -> None:
        """手动压缩会改写本机 checkpoint，必须由显式协商能力的交互客户端发起。"""
        if "context.manage" not in self._enabled_capabilities:
            raise RpcError(-32002, "CONTEXT_CAPABILITY_REQUIRED")
        if not self._thread_persistence_enabled():
            raise RpcError(-32002, "CONTEXT_COMPACTION_UNAVAILABLE")

    async def _ensure_thread_store(self) -> ThreadStore:
        """延迟打开用户级数据库；配置读取不应因为存储创建而被阻塞。"""
        if self._thread_store is None:
            if not self._thread_persistence_enabled():
                raise ThreadStoreError("THREADS_UNAVAILABLE_IN_ECHO_MODE")
            self._thread_store = await ThreadStore.open(
                project=self._workspace,
                home=self._config_home,
            )
        return self._thread_store

    async def _close_thread_store(self) -> None:
        """在 sidecar 生命周期末尾关闭 SQLite 连接和 WAL 句柄。"""
        store, self._thread_store = self._thread_store, None
        self._thread_agents.clear()
        self._context_middlewares.clear()
        if store is not None:
            await store.close()

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


def _thread_summary_payload(summary: Any) -> dict[str, object]:
    """把存储层摘要转换为 JSON-RPC 的 thread 字段，禁止携带原始 project 路径。"""
    return {
        "thread_id": summary.thread_id,
        "created_at_ms": summary.created_at_ms,
        "updated_at_ms": summary.updated_at_ms,
        "first_message": summary.first_message,
        "latest_message": summary.latest_message,
        "message_count": summary.message_count,
    }


def _thread_message_payload(message: Any) -> dict[str, object]:
    """把 checkpoint 归一化消息限制为 TUI 可回放的 project/thread/message 数据。"""
    payload: dict[str, object] = {"kind": message.kind, "content": message.content}
    if message.tool_name is not None:
        payload["tool_name"] = message.tool_name
    return payload


def _bounded_json(value: object) -> object:
    """限制交互详情的 JSON 字节数，避免审批参数撑爆 stdio 与 TUI。"""
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
