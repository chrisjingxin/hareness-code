"""Compose Turn 意图分类：确定性短语优先、受限小模型其次、unclear 兜底。

TurnIntentResolver 只负责把一条自然语言消息收敛为五个受限意图之一，
本身不拥有 Tool、不读取完整对话、不能写任何状态。分类结果由
ComposeWorkItemEngine 的路由层消费；无论分类器输出还是 schema 校验，
都不可能直接造成 abandon、覆盖或创建 Work Item 的副作用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

MAX_INTENT_DETAIL_CHARS = 400
"""分类器 detail 字段的最大字符数；超长内容一律按非法输出处理。"""


class TurnIntentKind(str, Enum):
    """Compose Turn 的受限意图集合；模型只能输出这五类。"""

    RESUME_CURRENT = "resume_current"
    AMEND_CURRENT = "amend_current"
    START_NEW_WORK = "start_new_work"
    SIDE_QUESTION = "side_question"
    UNCLEAR = "unclear"


class TurnIntentSource(str, Enum):
    """意图来源；显式命令/Interaction reply 优先于确定性短语和分类器。"""

    EXPLICIT = "explicit"
    DETERMINISTIC = "deterministic"
    CLASSIFIER = "classifier"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class TurnIntent:
    """一次 Turn 的解析意图；本身永远不能直接造成任何状态副作用。"""

    kind: TurnIntentKind
    detail: str = ""
    source: TurnIntentSource = TurnIntentSource.CLASSIFIER
    classifier_valid: bool = True


@dataclass(frozen=True, slots=True)
class TurnIntentContext:
    """分类器可见的受限上下文：目标/范围摘要、待决决策、当前 Activity 与本条消息。"""

    message: str
    goal_summary: str = ""
    scope_summary: str = ""
    pending_decision: str | None = None
    current_activity: str | None = None
    has_active_work_item: bool = False


class TurnIntentClassifierPort(Protocol):
    """小上下文分类端口；无 Tool、无状态，输出必须通过严格 schema。"""

    async def classify(self, context: TurnIntentContext) -> Mapping[str, object]: ...


#: 确定性控制短语（继续类）的受限词表；只做整句匹配，不猜测部分匹配。
_RESUME_PHRASES = frozenset(
    {
        "继续",
        "继续做",
        "继续执行",
        "接着做",
        "接着",
        "接着干",
        "往下做",
        "continue",
        "continue working",
        "continue the work",
        "keep going",
        "carry on",
        "resume",
        "please continue",
    }
)


def normalize_message(value: str) -> str:
    """规范化消息用于短语匹配：去首尾空白、折叠空白、ASCII 小写化。"""
    return " ".join(value.strip().split()).lower()


class TurnIntentResolver:
    """确定性短语 → 受限分类器 → unclear 兜底的纯路由，无任何写入能力。"""

    def __init__(self, classifier: TurnIntentClassifierPort | None = None) -> None:
        self._classifier = classifier

    async def resolve(self, context: TurnIntentContext) -> TurnIntent:
        """把单条消息解析为受限意图；任何失败路径都收敛为 unclear。"""
        message = normalize_message(context.message)
        if not message:
            return _invalid_intent()
        if message in _RESUME_PHRASES:
            return TurnIntent(
                kind=TurnIntentKind.RESUME_CURRENT,
                source=TurnIntentSource.DETERMINISTIC,
            )
        if self._classifier is None:
            return _invalid_intent()
        try:
            output = await self._classifier.classify(context)
        except Exception:
            # 分类器异常属于不可用，不阻断 Turn，交给用户选择。
            return _invalid_intent()
        return _parse_classifier_output(output)


def _parse_classifier_output(value: object) -> TurnIntent:
    """严格校验分类器输出；任何字段问题都收敛为 invalid unclear。"""
    if not isinstance(value, Mapping):
        return _invalid_intent()
    if set(value) - {"intent", "detail"}:
        return _invalid_intent()
    raw_intent = value.get("intent")
    if not isinstance(raw_intent, str):
        return _invalid_intent()
    try:
        kind = TurnIntentKind(raw_intent)
    except ValueError:
        return _invalid_intent()
    detail = value.get("detail")
    if detail is None:
        detail = ""
    if not isinstance(detail, str) or len(detail) > MAX_INTENT_DETAIL_CHARS:
        return _invalid_intent()
    return TurnIntent(
        kind=kind,
        detail=detail,
        source=TurnIntentSource.CLASSIFIER,
    )


def _invalid_intent() -> TurnIntent:
    """构造 schema 非法/分类器不可用时的统一 unclear 结果。"""
    return TurnIntent(
        kind=TurnIntentKind.UNCLEAR,
        source=TurnIntentSource.FALLBACK,
        classifier_valid=False,
    )
