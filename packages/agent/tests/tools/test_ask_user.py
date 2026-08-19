"""ask_user 允许一次问几道相关题，但拒绝一次塞十几题。"""

from __future__ import annotations

import pytest

from harness_agent.tools.ask_user import MAX_ASK_USER_QUESTIONS, _validate_questions


def _choice_question(text: str) -> dict[str, object]:
    return {
        "question": text,
        "type": "multiple_choice",
        "choices": [{"value": "A"}, {"value": "B"}],
    }


def test_related_questions_within_cap_are_valid() -> None:
    """相关的少量问题可以一次提交。"""
    questions = [_choice_question(f"决策 {index}") for index in range(1, 4)]
    _validate_questions(questions)  # type: ignore[arg-type]


def test_more_than_five_questions_are_rejected() -> None:
    """一次超过 5 题无法回答，工具应直接拒绝。"""
    questions = [_choice_question(f"问题 {index}") for index in range(MAX_ASK_USER_QUESTIONS + 1)]
    with pytest.raises(ValueError, match="at most 5"):
        _validate_questions(questions)  # type: ignore[arg-type]
