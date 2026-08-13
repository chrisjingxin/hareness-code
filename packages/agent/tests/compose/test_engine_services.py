"""engine_services 标记分隔正文解析回归：弱模型长 markdown 不再依赖 JSON 转义。"""

from __future__ import annotations

import pytest


def test_extract_between_markers() -> None:
    from harness_agent.compose.engine_services import _extract_between

    text = "说明\n---BEGIN-BODY---\n# 标题\n正文\n---END-BODY---\n结尾"
    assert _extract_between(text, "---BEGIN-BODY---", "---END-BODY---") == "# 标题\n正文"


def test_extract_missing_end_fails_closed() -> None:
    from harness_agent.compose.engine_services import _extract_between

    assert _extract_between("---BEGIN-BODY---\n无结尾", "---BEGIN-BODY---", "---END-BODY---") is None


def test_extract_missing_start_fails_closed() -> None:
    from harness_agent.compose.engine_services import _extract_between

    assert _extract_between("只有正文", "---BEGIN-BODY---", "---END-BODY---") is None


def test_extract_empty_body_fails_closed() -> None:
    from harness_agent.compose.engine_services import _extract_between

    assert (
        _extract_between(
            "---BEGIN-BODY---\n\n---END-BODY---", "---BEGIN-BODY---", "---END-BODY---"
        )
        is None
    )


def test_plan_extraction_preserves_quotes_and_newlines() -> None:
    """正文内双引号与原始换行原样保留，不经过 JSON 转义。"""
    from harness_agent.compose.engine_services import _extract_between

    text = '---BEGIN-PLAN---\n## 计划\n命令 `jsondiff a.json b.json`\n他说："完成"。"---END-PLAN---\n'
    assert '他说："完成"' in _extract_between(text, "---BEGIN-PLAN---", "---END-PLAN---")


@pytest.mark.asyncio
async def test_plan_prompt_does_not_enumerate_forbidden_placeholders() -> None:
    """Plan prompt 只描述完成态要求，避免弱模型复述禁词污染正文。"""
    from harness_agent.compose.activities.plan import PlanDraftContext
    from harness_agent.compose.engine_services import ManagedPlanDriver

    class _CapturingBase:
        task = ""

        @staticmethod
        def _stage(name: str) -> str:
            return name

        async def _run(self, *, stage: str, task: str, raw: bool = False):
            self.task = task
            return {
                "raw": (
                    "---BEGIN-PLAN---\n# 实施计划\n---END-PLAN---\n"
                    "---BEGIN-TODO---\n- [ ] 实现\n"
                    "  验收=完成\n  验证=python -m pytest -q\n"
                    "---END-TODO---"
                )
            }, "execution-1"

    base = _CapturingBase()
    driver = ManagedPlanDriver.__new__(ManagedPlanDriver)
    driver._base = base

    await driver.draft_plan(
        PlanDraftContext(
            goal="实现 jsondiff",
            task_digest="task-digest",
            spec_digest="spec-digest",
            spec_body="输出键路径级别差异。",
        )
    )

    assert "todo.md" in base.task
    instruction = base.task.split("---BEGIN-PLAN---", 1)[0]
    assert all(
        token not in instruction
        for token in ("TODO", "TBD", "待定", "待补充", "{{", "}}")
    )
