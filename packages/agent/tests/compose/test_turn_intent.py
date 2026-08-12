"""Compose Turn 意图分类测试：确定性短语优先、严格 schema、unclear 兜底。

TurnIntentResolver 只做五分类，分类结果本身不能直接造成 abandon、覆盖或
创建 Work Item 的副作用；schema 非法或分类器异常一律收敛为 unclear。
"""

from __future__ import annotations

import pytest

from harness_agent.compose.turn_intent import (
    TurnIntentContext,
    TurnIntentKind,
    TurnIntentResolver,
    TurnIntentSource,
)


class _FakeClassifier:
    """按脚本返回分类输出；可注入异常并记录调用上下文。"""

    def __init__(self, outputs: list[object] | None = None, *, error: Exception | None = None) -> None:
        self.outputs = list(outputs or [])
        self.error = error
        self.contexts: list[TurnIntentContext] = []

    async def classify(self, context: TurnIntentContext) -> object:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0)


async def _resolve(message: str, classifier: _FakeClassifier | None = None):
    resolver = TurnIntentResolver(classifier)
    return await resolver.resolve(TurnIntentContext(message=message))


async def test_deterministic_resume_phrases_skip_classifier() -> None:
    """受限词表内的继续词直接路由 resume，不消耗模型调用。"""
    for phrase in ("继续", "接着做", "继续执行", "continue", "keep going", " 继续 "):
        classifier = _FakeClassifier([{"intent": "side_question"}])
        intent = await _resolve(phrase, classifier)
        assert intent.kind is TurnIntentKind.RESUME_CURRENT
        assert intent.source is TurnIntentSource.DETERMINISTIC
        assert classifier.contexts == []


async def test_non_phrase_message_delegates_to_classifier_with_bounded_context() -> None:
    """非词表消息交给小上下文分类器，上下文只含摘要与当前事实。"""
    classifier = _FakeClassifier([{"intent": "start_new_work"}])
    context = TurnIntentContext(
        message="帮我做一个新的搜索功能",
        goal_summary="实现搜索",
        scope_summary="仅后端",
        pending_decision="confirm:task",
        current_activity="task",
        has_active_work_item=True,
    )
    intent = await TurnIntentResolver(classifier).resolve(context)
    assert intent.kind is TurnIntentKind.START_NEW_WORK
    assert intent.source is TurnIntentSource.CLASSIFIER
    assert classifier.contexts == [context]


async def test_classifier_valid_outputs_cover_all_five_kinds() -> None:
    """严格 schema 下五个受限意图都可作为分类结果。"""
    for raw in (
        "resume_current",
        "amend_current",
        "start_new_work",
        "side_question",
        "unclear",
    ):
        classifier = _FakeClassifier([{"intent": raw, "detail": "ok"}])
        intent = await _resolve("任意消息", classifier)
        assert intent.kind.value == raw
        assert intent.source is TurnIntentSource.CLASSIFIER
        assert intent.classifier_valid


@pytest.mark.parametrize(
    "output",
    (
        "not-a-mapping",
        {"intent": "delete_everything"},
        {"intent": "resume_current", "extra": "x"},
        {"detail": "缺少 intent"},
        {"intent": 42},
        {"intent": "resume_current", "detail": 7},
        {"intent": "resume_current", "detail": "x" * 401},
    ),
)
async def test_invalid_classifier_output_converges_to_unclear(output: object) -> None:
    """schema 非法输出不能进入任何执行路径，统一为 invalid unclear。"""
    intent = await _resolve("任意消息", _FakeClassifier([output]))
    assert intent.kind is TurnIntentKind.UNCLEAR
    assert not intent.classifier_valid


async def test_classifier_exception_converges_to_unclear_fallback() -> None:
    """模型不可用不阻断 Turn，落入 unclear 等待用户选择。"""
    classifier = _FakeClassifier(error=RuntimeError("model unavailable"))
    intent = await _resolve("任意消息", classifier)
    assert intent.kind is TurnIntentKind.UNCLEAR
    assert intent.source is TurnIntentSource.FALLBACK
    assert not intent.classifier_valid


async def test_empty_message_is_unclear_without_classifier_call() -> None:
    """空白消息不猜测意图，也不浪费一次分类调用。"""
    classifier = _FakeClassifier()
    intent = await _resolve("   ", classifier)
    assert intent.kind is TurnIntentKind.UNCLEAR
    assert not intent.classifier_valid
    assert classifier.contexts == []


async def test_missing_classifier_falls_back_to_unclear() -> None:
    """未注入分类器时非词表消息按 unclear 处理，不能直接执行。"""
    intent = await _resolve("任意消息")
    assert intent.kind is TurnIntentKind.UNCLEAR
    assert intent.source is TurnIntentSource.FALLBACK
