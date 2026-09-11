"""当前 root Run 的受控验证证据与复验。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import uuid

from harness_agent.goals.models import GoalStoreError

# 黑名单只是登记阶段的第一道闸：rerun 通道真正的保证是只能 exact 复用
# 已审批执行过的命令，这里挡掉的是把副作用命令伪装成"验证"的登记请求。
_UNSAFE = re.compile(
    r"(?:rm\s+-rf|sudo\s+|curl\s+|wget\s+|pip\s+install|npm\s+install|pnpm\s+add|"
    r"yarn\s+add|git\s+push|docker\s+push|kubectl\s+apply|chmod\s+777|"
    r"mkfs|dd\s+if=|>\s*/dev/)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """一次已经实际执行过、可按原样复验的命令。"""

    evidence_id: str
    command: str
    cwd: str
    sandbox: str
    env: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class VerificationEvidenceRegistry:
    """只记录当前 root Run 内实际发生的安全验证命令。"""

    _items: dict[str, VerificationEvidence] = field(default_factory=dict)

    def record(
        self,
        *,
        command: str,
        cwd: str,
        sandbox: str,
        env: tuple[tuple[str, str], ...] = (),
    ) -> str:
        """登记一条可复验证据；副作用命令当场拒绝。"""
        if not isinstance(command, str) or not command.strip():
            raise GoalStoreError("GOAL_VERIFICATION_FORBIDDEN")
        cleaned = command.strip()
        if _UNSAFE.search(cleaned) or "\n" in cleaned or ";" in cleaned:
            raise GoalStoreError("GOAL_VERIFICATION_FORBIDDEN")
        evidence_id = f"evidence-{uuid.uuid4().hex}"
        self._items[evidence_id] = VerificationEvidence(
            evidence_id=evidence_id,
            command=cleaned,
            cwd=cwd,
            sandbox=sandbox,
            env=tuple(env),
        )
        return evidence_id

    def lookup(self, evidence_id: str) -> VerificationEvidence | None:
        """读取当前 Run 已登记的证据。"""
        return self._items.get(evidence_id)

    def rerun(self, evidence_id: str) -> VerificationEvidence:
        """只允许 exact 复验当前 Run 已有证据，不能提交任意命令。"""
        evidence = self._items.get(evidence_id)
        if evidence is None:
            raise GoalStoreError("GOAL_VERIFICATION_FORBIDDEN")
        return evidence
