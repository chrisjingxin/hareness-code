"""Compose VerificationPort：只经 canonical execution backend 执行命令。

验证命令必须继续经过 Policy、Approval、workspace boundary、sandbox 与
Host 并发锁：拒绝、超时、后端失败都不回退到宿主机旁路执行。每条命令
产生 fresh VerificationEvidence（command、工作目录身份、时间、exit code、
bounded output digest），模型文本不能替代 evidence。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from harness_agent.compose.models import VerificationEvidence

if TYPE_CHECKING:
    from harness_agent.runtime.execution import ExecutionSettings, WorkspaceExecutionResourcePool

DEFAULT_VERIFY_TIMEOUT_SECONDS = 180.0
MAX_OUTPUT_SUMMARY_CHARS = 2_000
TIMEOUT_EXIT_CODE = 124

_SECRET_PATTERNS = (
    # key=value / key: value 形式的凭据赋值
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret)\s*[:=]\s*[^\s,;]+",
    # PEM 私钥/证书块整体替换
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----",
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
)


def _redact_secrets(text: str) -> str:
    """把常见凭据形态替换为占位符，避免验证输出摘要落盘敏感值。"""
    import re

    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.DOTALL)
    return redacted


def _working_dir_identity(workspace_path: str, workspace: Path) -> str:
    """返回工作目录的有界身份：workspace 相对路径，绝不落盘绝对用户目录。"""
    try:
        relative = Path(workspace_path).resolve().relative_to(workspace.resolve())
        return str(relative) or "."
    except (ValueError, OSError):
        return Path(workspace_path).name or workspace.name


class VerificationError(RuntimeError):
    """验证执行边界的稳定错误；code 区分 deny/approval/backend/timeout。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定错误码与诊断文案。"""
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """一条验证命令的执行请求；approve 由 workflow 提供交互通道。"""

    command: str
    label: str
    resource_key: str
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS
    approve: Callable[[str], Awaitable[bool]] | None = None

    def __post_init__(self) -> None:
        """拒绝空命令、空标签、空资源键和非法超时。"""
        if not self.command.strip() or not self.label.strip() or not self.resource_key:
            raise ValueError("VERIFICATION_REQUEST_INVALID")
        if self.timeout_seconds <= 0:
            raise ValueError("VERIFICATION_TIMEOUT_INVALID")


class VerificationPort(Protocol):
    """执行一条有界验证命令并返回 fresh evidence 的 seam。"""

    async def run(self, request: VerificationRequest) -> VerificationEvidence: ...


class ManagedVerificationPort:
    """Host-backed 实现：Policy → Approval → 并发写锁 → canonical backend。

    命令只在 WorkspaceExecutionResourcePool 提供的 canonical execution
    context 中运行；Policy deny 与用户拒绝直接失败，绝不降级到其他执行器。
    """

    def __init__(
        self,
        *,
        pool: WorkspaceExecutionResourcePool,
        settings: ExecutionSettings,
        workspace: Path,
        rules_provider: Callable[[], list[Any]],
        rwlock: Any,
        now_ms: Callable[[], int],
    ) -> None:
        """注入与 Agent 相同的资源池、策略、规则与 Host 并发锁。"""
        self._pool = pool
        self._settings = settings
        self._workspace = workspace
        self._rules_provider = rules_provider
        self._rwlock = rwlock
        self._now_ms = now_ms

    async def run(self, request: VerificationRequest) -> VerificationEvidence:
        """执行一条命令；所有路径都经过既有安全边界并返回 fresh evidence。"""
        from harness_agent.policy.bash_floors import evaluate_safety_floors
        from harness_agent.policy.bash_parser import extract_segments
        from harness_agent.policy.permission_rules import evaluate_tool_rules
        from harness_agent.policy.safe_commands import is_safe_command

        rules = self._rules_provider()
        effect = evaluate_tool_rules("execute", {"command": request.command}, rules)
        if effect == "deny":
            raise VerificationError(
                "POLICY_DENIED", "verification command denied by policy"
            )

        # 审批决策与 Agent execute 的 default 模式一致：allow 规则或安全
        # 白名单跳过弹窗；其余命令需要用户批准。
        floors = evaluate_safety_floors(request.command)
        segments = extract_segments(request.command)
        needs_approval = effect != "allow" and (
            not segments or not all(is_safe_command(segment) for segment in segments)
        )
        if not needs_approval and floors.get("any_floor_triggered"):
            needs_approval = True
        if needs_approval:
            if request.approve is None:
                raise VerificationError(
                    "APPROVAL_REQUIRED",
                    "verification command requires user approval",
                )
            description = (
                f"Compose 验证命令需要审批：{request.label}\n"
                f"命令：{request.command[:500]}"
            )
            approved = await request.approve(description)
            if not approved:
                raise VerificationError(
                    "VERIFICATION_DENIED", "verification command rejected by user"
                )

        # 验证命令可能修改工作区，与 Agent 写工具一致：整个执行期间持有
        # Host 独占写锁，防止其他 Thread 的写操作与验证命令交错。
        await self._rwlock.acquire_write()
        try:
            lease = await self._pool.acquire(
                request.resource_key,
                self._settings,
                self._workspace,
            )
            started_at_ms = self._now_ms()
            try:
                backend = lease.value.backend
                # backend.execute 的 timeout 是终止进程的权威超时；外层
                # wait_for 只作兜底，防止 backend 无视 timeout 时无限挂起。
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        backend.execute,
                        request.command,
                        timeout=int(request.timeout_seconds),
                    ),
                    timeout=request.timeout_seconds + 30.0,
                )
            except TimeoutError:
                raise VerificationError(
                    "VERIFICATION_TIMEOUT",
                    f"command timed out after {request.timeout_seconds:.0f}s",
                ) from None
            except Exception as exc:
                if "timeout" in type(exc).__name__.lower():
                    raise VerificationError(
                        "VERIFICATION_TIMEOUT",
                        f"command timed out after {request.timeout_seconds:.0f}s",
                    ) from exc
                raise VerificationError(
                    "BACKEND_FAILED",
                    f"execution backend failed: {type(exc).__name__}",
                ) from exc
            exit_code = int(response.exit_code) if response.exit_code is not None else -1
            if exit_code < 0 or exit_code > 255:
                raise VerificationError(
                    "BACKEND_FAILED", "backend returned an invalid exit code"
                )
            output = str(response.output or "")
        finally:
            await lease.release()
            await self._rwlock.release_write()
        finished_at_ms = self._now_ms()
        encoded = output.encode("utf-8")
        truncated = len(encoded) > MAX_OUTPUT_SUMMARY_CHARS * 4
        summary = _redact_secrets(output[:MAX_OUTPUT_SUMMARY_CHARS])
        return VerificationEvidence(
            command=request.command,
            working_dir=_working_dir_identity(lease.value.workspace_path, self._workspace),
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            exit_code=exit_code,
            output_digest=hashlib.sha256(encoded).hexdigest(),
            output_summary=summary,
            truncated=truncated,
        )
