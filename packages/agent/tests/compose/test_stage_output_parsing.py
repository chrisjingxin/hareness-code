"""stage 结构化输出解析回归：弱模型 prose 包裹 JSON 的有界回退。"""

from __future__ import annotations

import pytest


def test_strict_json_parses() -> None:
    from harness_agent.compose.stage_agents import parse_structured_output

    assert parse_structured_output('{"outcome": "completed"}') == {"outcome": "completed"}


def test_markdown_fenced_json_parses() -> None:
    from harness_agent.compose.stage_agents import parse_structured_output

    content = "```json\n{\"outcome\": \"completed\"}\n```"
    assert parse_structured_output(content) == {"outcome": "completed"}


def test_prose_wrapped_json_parses_via_bounded_fallback() -> None:
    """E2E 实测失败形态：JSON 前后附加解释文字；切片后按同一 schema 校验。"""
    from harness_agent.compose.stage_agents import parse_structured_output

    content = (
        "已完成实现，以下是结构化结果：\n"
        '{"outcome": "completed", "fail_before": "pytest -q → 2 failed", '
        '"pass_after": "pytest -q → 8 passed"}'
        "\n如需进一步调整请告诉我。"
    )
    assert parse_structured_output(content)["outcome"] == "completed"


def test_plain_text_without_json_fails_closed() -> None:
    from harness_agent.compose.stage_agents import parse_structured_output

    with pytest.raises(ValueError, match="不是有效 JSON"):
        parse_structured_output("没有任何 JSON 的输出")


def test_json_array_rejected_as_non_object() -> None:
    from harness_agent.compose.stage_agents import parse_structured_output

    with pytest.raises(ValueError, match="STAGE_OUTPUT_NOT_OBJECT"):
        parse_structured_output("[1, 2, 3]")


def test_json_with_raw_newlines_in_string_parses() -> None:
    """E2E 实测：markdown 正文含未转义换行（RFC 8259 控制字符）也能解析。"""
    from harness_agent.compose.stage_agents import parse_structured_output

    content = '{"body": "# 标题\n\n## 1. 目标\n\n实现 CLI 工具"}'
    assert parse_structured_output(content)["body"].startswith("# 标题")


def test_broken_json_without_object_still_fails_closed() -> None:
    """结构损坏（非控制字符问题）仍拒绝：不放松结构契约。"""
    from harness_agent.compose.stage_agents import parse_structured_output

    with pytest.raises(ValueError, match="不是有效 JSON"):
        parse_structured_output('{"body": "未闭合')


def test_empty_output_fails_closed() -> None:
    from harness_agent.compose.stage_agents import parse_structured_output

    with pytest.raises(ValueError, match="输出为空"):
        parse_structured_output("")
