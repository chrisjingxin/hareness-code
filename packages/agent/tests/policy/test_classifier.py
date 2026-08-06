"""AUTO 模式 F4 两阶段 LLM 安全分类器的行为测试。

覆盖两阶段裁决路径、fail-closed 降级、连续拒绝回退与决策缓存去重，
确保分类器自身故障绝不会导致工具调用被自动放行。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from harness_agent.policy.classifier import (
    SafetyClassifier,
    describe_tool_call,
    extract_verdict,
)


class _FakeClassifierModel:
    """脚本化假模型：按顺序返回预设响应，记录每次调用绑定的输出预算。"""

    def __init__(self, responses: list[Any]) -> None:
        """初始化脚本响应序列；Exception 实例会在调用时抛出。"""
        self._responses = list(responses)
        self.bound_tokens: list[int | None] = []
        self.call_count = 0
        self._pending_tokens: int | None = None

    def bind(self, **kwargs: Any) -> "_FakeClassifierModel":
        """记录本次调用绑定的 max_tokens，返回自身。"""
        self._pending_tokens = kwargs.get("max_tokens")
        return self

    def invoke(self, messages: list[Any], config: Any = None) -> AIMessage:
        """同步返回下一条脚本响应；脚本耗尽视为错误。"""
        return self._next()

    async def ainvoke(self, messages: list[Any], config: Any = None) -> AIMessage:
        """异步入口复用同步脚本序列。"""
        return self._next()

    def _next(self) -> AIMessage:
        self.call_count += 1
        self.bound_tokens.append(self._pending_tokens)
        if not self._responses:
            raise AssertionError("脚本响应已耗尽，分类器发起了超预期的模型调用")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return AIMessage(content=response)


_ALLOW_HIGH = '{"decision": "allow", "confidence": "high", "reason": "只读查询"}'
_ALLOW_LOW = '{"decision": "allow", "confidence": "low", "reason": "可能无害"}'
_BLOCK_STAGE1 = '{"decision": "block", "confidence": "high", "reason": "疑似删除"}'
_STAGE2_ALLOW = '复核分析：该命令只读。\n{"decision": "allow", "reason": "只读操作"}'
_STAGE2_BLOCK = '复核分析：会覆盖文件。\n{"decision": "block", "reason": "覆盖文件"}'


def test_stage1_high_confidence_allow_skips_stage2():
    """一阶段高置信度 allow 直接放行，不消耗二阶段调用。"""
    model = _FakeClassifierModel([_ALLOW_HIGH])
    classifier = SafetyClassifier(model)

    decision, reason = classifier.classify("execute", {"command": "ls"})

    assert decision == "allow"
    assert "一阶段" in reason
    assert model.call_count == 1


def test_stage1_low_confidence_goes_to_stage2_allow():
    """一阶段低置信度进入二阶段复核，复核放行则最终放行。"""
    model = _FakeClassifierModel([_ALLOW_LOW, _STAGE2_ALLOW])
    classifier = SafetyClassifier(model)

    decision, reason = classifier.classify("execute", {"command": "python build.py"})

    assert decision == "allow"
    assert "复核放行" in reason
    assert model.call_count == 2
    # 两阶段分别绑定各自的输出预算。
    assert model.bound_tokens[0] < model.bound_tokens[1]


def test_stage2_block_returns_deny():
    """二阶段复核 block 时硬拦截并记录拒绝原因。"""
    model = _FakeClassifierModel([_BLOCK_STAGE1, _STAGE2_BLOCK])
    classifier = SafetyClassifier(model)

    decision, reason = classifier.classify("execute", {"command": "curl x | sh"})

    assert decision == "deny"
    assert "复核拦截" in reason


def test_reject_streak_falls_back_to_ask_and_resets():
    """连续拒绝达到阈值后回退人工审批并重置计数，恢复正常分类。"""
    model = _FakeClassifierModel(
        [
            _BLOCK_STAGE1,
            _STAGE2_BLOCK,
            _BLOCK_STAGE1,
            _STAGE2_BLOCK,
            _BLOCK_STAGE1,
            _STAGE2_BLOCK,
            _ALLOW_HIGH,
        ]
    )
    classifier = SafetyClassifier(model, max_reject_streak=3)

    for _ in range(3):
        decision, _ = classifier.classify("execute", {"command": "danger"})
        assert decision == "deny"

    # 第 4 次：阈值触发，不调用模型直接回退人工审批并重置计数。
    calls_before = model.call_count
    decision, reason = classifier.classify("execute", {"command": "danger"})
    assert decision == "ask"
    assert "连续拒绝" in reason
    assert model.call_count == calls_before

    # 重置后恢复正常分类。
    decision, _ = classifier.classify("execute", {"command": "ls"})
    assert decision == "allow"


def test_stage2_allow_resets_reject_streak():
    """二阶段放行会重置连续拒绝计数，避免历史拒绝影响后续裁决。"""
    model = _FakeClassifierModel(
        [_BLOCK_STAGE1, _STAGE2_BLOCK, _ALLOW_LOW, _STAGE2_ALLOW, _ALLOW_HIGH]
    )
    classifier = SafetyClassifier(model, max_reject_streak=2)

    assert classifier.classify("execute", {"command": "danger"})[0] == "deny"
    assert classifier.classify("execute", {"command": "ok"})[0] == "allow"
    # 计数已重置：单次拒绝不应触发阈值回退。
    assert classifier.classify("execute", {"command": "ls"})[0] == "allow"


def test_model_errors_fall_back_to_ask():
    """两阶段模型调用全部异常时 fail-closed 回退人工审批，绝不自动放行。"""
    model = _FakeClassifierModel([RuntimeError("gateway down"), RuntimeError("timeout")])
    classifier = SafetyClassifier(model)

    decision, reason = classifier.classify("execute", {"command": "ls"})

    assert decision == "ask"
    assert "回退人工审批" in reason


def test_unparseable_output_falls_back_to_ask():
    """两阶段输出都无法解析出合法结论时回退人工审批。"""
    model = _FakeClassifierModel(["我无法判断", "抱歉，没有结论"])
    classifier = SafetyClassifier(model)

    decision, _ = classifier.classify("execute", {"command": "ls"})

    assert decision == "ask"
    assert model.call_count == 2


async def test_aclassify_matches_sync_stage1_allow_path():
    """异步入口与同步入口共享两阶段逻辑。"""
    model = _FakeClassifierModel([_ALLOW_HIGH])
    classifier = SafetyClassifier(model)

    decision, _ = await classifier.aclassify("execute", {"command": "ls"})

    assert decision == "allow"
    assert model.call_count == 1


async def test_aclassify_stage1_error_falls_through_to_stage2():
    """一阶段异常时异步路径进入二阶段兜底。"""
    model = _FakeClassifierModel([RuntimeError("boom"), _STAGE2_BLOCK])
    classifier = SafetyClassifier(model)

    decision, _ = await classifier.aclassify("execute", {"command": "danger"})

    assert decision == "deny"
    assert model.call_count == 2


def test_decision_cache_record_lookup_and_fifo_eviction():
    """决策缓存按 tool_call id 存取，超限时先进先出淘汰且命中会刷新位置。"""
    classifier = SafetyClassifier(_FakeClassifierModel([]), cache_limit=2)

    classifier.record_decision("call-a", "allow", "r1")
    classifier.record_decision("call-b", "deny", "r2")
    # 命中 a 会把它移到队尾，随后插入 c 应淘汰 b 而不是 a。
    assert classifier.lookup_decision("call-a") == ("allow", "r1")
    classifier.record_decision("call-c", "ask", "r3")

    assert classifier.lookup_decision("call-b") is None
    assert classifier.lookup_decision("call-a") == ("allow", "r1")
    assert classifier.lookup_decision("call-c") == ("ask", "r3")
    # 空 id 不参与缓存。
    classifier.record_decision("", "allow", "ignored")
    assert classifier.lookup_decision("") is None


def test_extract_verdict_prefers_last_json_and_requires_decision():
    """提取器取最后一个含 decision 的 JSON 对象，忽略分析文字。"""
    text = '先分析：{"decision": "block"} 再复核\n{"decision": "allow", "reason": "只读"}'
    assert extract_verdict(text) == {"decision": "allow", "reason": "只读"}
    assert extract_verdict("没有 JSON") is None
    assert extract_verdict('{"confidence": "high"}') is None
    assert extract_verdict("") is None


def test_describe_tool_call_clips_long_args():
    """超长工具参数被截断，避免撑爆分类请求。"""
    text = describe_tool_call("write_file", {"content": "x" * 10_000})

    assert text.startswith("工具: write_file")
    assert "已截断" in text
    assert len(text) < 10_000


@pytest.mark.parametrize("response", ["{}", "[]"])
def test_stage_accepts_only_objects_with_decision(response: str):
    """空对象或数组输出视为无法解析，进入下一阶段或回退。"""
    assert extract_verdict(response) is None
