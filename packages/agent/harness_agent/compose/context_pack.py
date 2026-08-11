"""Compose ContextPack：每个阶段只拿当前有界输入，不携带完整过程历史。

ContextPack 由 frozen Run snapshot、已确认 artifact 与私有方法资产渲染为
有界任务文本；源码正文、完整 Tool 输出和凭据不进入 pack。
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_agent.compose.models import (
    ComposeStage,
    UnderstandingArtifact,
)

MAX_CONTEXT_PACK_CHARS = 32_000
_MAX_FIELD_CHARS = 4_000


def _bounded(text: str, label: str) -> str:
    """按包内字段上限截断并标注来源，避免静默丢弃关键事实。"""
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[:_MAX_FIELD_CHARS] + f"\n[{label} 超长已截断]"


def _bullets(items: tuple[str, ...], label: str) -> str:
    if not items:
        return f"- {label}：无"
    return "\n".join(f"- {_bounded(item, label)}" for item in items)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """一个阶段的一次有界输入；render() 生成发给 stage agent 的任务文本。"""

    stage: ComposeStage
    user_request: str
    method_asset: str
    revision: int
    goal: str = ""
    constraints: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    change_kind: str = ""
    feedback: str = ""
    answers: tuple[tuple[str, str], ...] = ()
    workspace_root: str = ""

    def render(self) -> str:
        """渲染有界任务文本；超出总上限时从方法资产尾部截断。"""
        facts = [
            "# Compose 阶段任务",
            f"## 用户请求\n{_bounded(self.user_request, 'user_request')}",
            f"## 当前 revision\n{self.revision}",
        ]
        if self.goal:
            facts.append(f"## 已确认目标\n{_bounded(self.goal, 'goal')}")
        if self.change_kind:
            facts.append(f"## 变更类型\n{self.change_kind}")
        if self.constraints:
            facts.append(f"## 约束\n{_bullets(self.constraints, 'constraints')}")
        if self.acceptance:
            facts.append(f"## 验收标准\n{_bullets(self.acceptance, 'acceptance')}")
        if self.out_of_scope:
            facts.append(f"## 非范围\n{_bullets(self.out_of_scope, 'out_of_scope')}")
        if self.answers:
            lines = "\n".join(
                f"- {_bounded(question, 'question')}：{_bounded(answer, 'answer')}"
                for question, answer in self.answers
            )
            facts.append(f"## 用户已决策\n{lines}")
        if self.feedback:
            facts.append(f"## 用户修改意见\n{_bounded(self.feedback, 'feedback')}")
        if self.workspace_root:
            facts.append(f"## 仓库根目录\n{_bounded(self.workspace_root, 'workspace_root')}")
        facts.append("## 方法\n" + self.method_asset)
        rendered = "\n\n".join(facts)
        if len(rendered) <= MAX_CONTEXT_PACK_CHARS:
            return rendered
        marker = "\n\n[上下文已按上限截断，保留用户请求与已确认事实]"
        budget = MAX_CONTEXT_PACK_CHARS - len(marker)
        return rendered[:budget] + marker


def build_understand_pack(
    *,
    user_request: str,
    revision: int,
    method_asset: str,
    workspace_root: str = "",
    answers: tuple[tuple[str, str], ...] = (),
) -> ContextPack:
    """构造 Understand 阶段的有界输入；answers 是已回写产品决策。"""
    return ContextPack(
        stage=ComposeStage.UNDERSTAND,
        user_request=user_request,
        method_asset=method_asset,
        revision=revision,
        answers=answers,
        workspace_root=workspace_root,
    )


def build_plan_pack(
    *,
    user_request: str,
    revision: int,
    method_asset: str,
    understanding: UnderstandingArtifact,
    workspace_root: str = "",
    feedback: str = "",
) -> ContextPack:
    """构造 Plan 阶段的有界输入；feedback 来自用户「修改」门禁。"""
    return ContextPack(
        stage=ComposeStage.PLAN,
        user_request=user_request,
        method_asset=method_asset,
        revision=revision,
        goal=understanding.goal,
        constraints=understanding.constraints,
        acceptance=understanding.acceptance,
        out_of_scope=understanding.out_of_scope,
        change_kind=understanding.change_kind,
        feedback=feedback,
        workspace_root=workspace_root,
    )
