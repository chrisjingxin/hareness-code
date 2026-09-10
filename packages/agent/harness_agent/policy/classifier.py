"""AUTO 模式 F4 层 LLM 安全分类器。

设计文档中 AUTO 四层过滤器的 F4 层：前三层（acceptEdits 快速通道、安全工具
白名单、破坏性命令守卫）都无法给出结论时，调用 LLM 对工具调用做两阶段安全
分类。本模块只负责分类本身与决策缓存，规则裁决、敏感路径检查由调用方
（``AutoClassifierMiddleware``）在分类前完成。

两阶段流程（取 QwenCode + Claude Code 的保守组合）：

- 第一阶段快速判断：低 max_tokens，要求模型立即给出
  ``{"decision", "confidence", "reason"}``。高置信度 allow 直接放行；
  其余情况（block、低置信度、格式异常）进入第二阶段。
- 第二阶段复核：更高 max_tokens，允许简短分析后给出最终结论；
  最终 block 会使连续拒绝计数加一。

fail-closed 约定：分类器连续拒绝达到阈值后回退人工审批并重置计数；
模型调用异常、超时或两阶段都无法解析出合法结论时同样回退人工审批。
分类器在任何情况下都不会因为自身故障而自动放行工具调用。

分类器模型建议以 10 秒超时构建（见 ``agent_host`` 装配处），本模块不依赖
具体的模型工厂，任何提供 ``invoke``/``ainvoke`` 的聊天模型均可注入。
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from harness_agent.runtime.provider_retry import (
    run_with_provider_retry,
    run_with_provider_retry_sync,
)

logger = logging.getLogger(__name__)

# 第一阶段快速判断的输出预算：只够输出一行 JSON。
STAGE1_MAX_TOKENS = 120
# 第二阶段复核的输出预算：允许简短分析加最终 JSON。
STAGE2_MAX_TOKENS = 600
# 连续拒绝阈值：达到后下一次 F4 调用回退人工审批并重置计数。
MAX_REJECT_STREAK = 3
# 工具参数序列化后的展示上限，避免超长参数撑爆分类请求。
ARGS_CLIP_CHARS = 4_000
# 决策缓存容量：按 Run 命名空间与 tool_call id 去重，防止预检与执行层重复调用分类器。
DECISION_CACHE_LIMIT = 1_024

ClassifierNamespace = tuple[str, str, str] | None
"""分类决策的 Run 命名空间；生产路径包含 thread、run 和 execution 身份。"""

_STAGE1_SYSTEM_PROMPT = """你是编码 Agent 工具调用的安全分类器。根据工具名与参数判断该操作能否自动放行，拿不准时一律 block。
只输出一个 JSON 对象，不要任何解释或代码块：
{"decision": "allow"或"block", "confidence": "high"或"low", "reason": "一句话原因"}
判断标准：
- allow：明确无害的操作，如只读查询、工作区内的构建/测试/lint、环境信息查看。
- block：删除或覆盖文件、不可逆操作、强制推送、数据库删改、下载并执行远程脚本、访问凭据密钥、向外部上传数据、混淆或难以判断的命令。
"""

_STAGE2_SYSTEM_PROMPT = """你是编码 Agent 工具调用安全分类的二阶段复核员。一阶段结论可能出错，请谨慎复核，拿不准时一律 block。
先用不超过三行文字给出分析，然后另起一行只输出一个 JSON 对象：
{"decision": "allow"或"block", "reason": "一句话原因"}
判断标准同一阶段：只有明确无害的操作才允许 allow；删除或覆盖文件、不可逆操作、远程代码执行、凭据访问、数据外传、混淆命令等必须 block。
"""


class ClassifierModel(Protocol):
    """分类器所需的最小模型协议：同步/异步调用加参数绑定。"""

    def bind(self, **kwargs: Any) -> Any: ...

    def invoke(self, messages: list[Any], config: Any = None) -> Any: ...

    async def ainvoke(self, messages: list[Any], config: Any = None) -> Any: ...


#: 分类调用必须与主对话的事件流隔离：分类器运行在模型节点内部，若不显式
#: 清空 callbacks，langchain 会把它挂为主运行的子事件，分类 JSON 文本会
#: 被 TUI 当作助手输出渲染。
_ISOLATED_RUN_CONFIG: dict[str, Any] = {"callbacks": []}


def describe_tool_call(tool_name: str, tool_args: dict[str, Any]) -> str:
    """把工具调用序列化为分类器的输入文本，超长参数截断。"""
    try:
        args_text = json.dumps(tool_args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args_text = str(tool_args)
    if len(args_text) > ARGS_CLIP_CHARS:
        args_text = args_text[:ARGS_CLIP_CHARS] + "…（已截断）"
    return f"工具: {tool_name}\n参数: {args_text}"


def classifier_namespace_for_context(context: object) -> ClassifierNamespace:
    """从 RunContext 提取分类缓存命名空间，阻止共享 Engine 跨 Run 串缓存。"""
    if context is None:
        # 没有 runtime context 的静态单元测试仍共享一个明确的 unscoped 槽位；
        # 生产共享图始终传入经过校验的 thread/run/execution 三元身份。
        return None
    thread_id = getattr(context, "thread_id", None)
    run_id = getattr(context, "run_id", None)
    execution_id = getattr(context, "execution_id", None)
    if all(isinstance(value, str) and value for value in (thread_id, run_id, execution_id)):
        return (thread_id, run_id, execution_id)
    return None


def classifier_namespace_for_request(request: object) -> ClassifierNamespace:
    """从模型/工具请求读取当前 Run 的分类缓存命名空间。"""
    runtime = getattr(request, "runtime", None)
    return classifier_namespace_for_context(getattr(runtime, "context", None))


def extract_verdict(text: str) -> dict[str, Any] | None:
    """从模型输出中提取最后一个含 decision 字段的 JSON 对象。

    第二阶段允许模型先输出分析文字再给结论，因此取最后一个合法对象；
    解析失败或字段缺失返回 None。
    """
    candidates = re.findall(r"\{[^{}]*\}", text or "")
    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("decision"), str):
            return parsed
    return None


class SafetyClassifier:
    """两阶段 LLM 安全分类器，附带连续拒绝计数与决策缓存。

    决策缓存按 ``(thread_id, run_id, execution_id, tool_call_id)`` 记录最终处置
    （allow/deny/ask），供 HITL 预检与执行层守卫复用，保证同一次工具调用最多
    被分类一次，同时阻止兼容提供方复用短 tool_call id 时跨 Run 串状态。
    """

    def __init__(
        self,
        model: ClassifierModel,
        *,
        max_reject_streak: int = MAX_REJECT_STREAK,
        cache_limit: int = DECISION_CACHE_LIMIT,
        provider_retry: Any | None = None,
    ) -> None:
        """初始化分类器。

        Args:
            model: 聊天模型实例；建议以约 10 秒超时构建，fail-closed 依赖它。
            max_reject_streak: 连续拒绝阈值，达到后回退人工审批并重置。
            cache_limit: 决策缓存条目上限，先进先出淘汰。
            provider_retry: 分类器直接模型调用使用的有界 retry owner。
        """
        self._model = model
        self._max_reject_streak = max(1, max_reject_streak)
        self._cache_limit = max(1, cache_limit)
        self._provider_retry = provider_retry
        # 共享 AgentEngine 复用分类器实例；连续拒绝也必须随 Run 隔离，
        # 否则一个 Run 的危险调用会耗尽另一个 Run 的分类额度。
        self._reject_streaks: OrderedDict[ClassifierNamespace, int] = OrderedDict()
        self._decisions: OrderedDict[tuple[ClassifierNamespace, str], tuple[str, str]] = OrderedDict()

    # —— 决策缓存：预检与执行层守卫共享 ——

    def record_decision(
        self,
        tool_call_id: str,
        decision: str,
        reason: str,
        *,
        namespace: ClassifierNamespace = None,
    ) -> None:
        """记录指定 Run 一次工具调用的最终处置，供后续环节复用。"""
        if not tool_call_id:
            return
        key = (namespace, tool_call_id)
        self._decisions[key] = (decision, reason)
        self._decisions.move_to_end(key)
        while len(self._decisions) > self._cache_limit:
            self._decisions.popitem(last=False)

    def lookup_decision(
        self,
        tool_call_id: str,
        *,
        namespace: ClassifierNamespace = None,
    ) -> tuple[str, str] | None:
        """查询指定 Run 已记录的处置；未记录返回 None。"""
        if not tool_call_id:
            return None
        key = (namespace, tool_call_id)
        cached = self._decisions.get(key)
        if cached is not None:
            self._decisions.move_to_end(key)
        return cached

    # —— 分类入口 ——

    async def aclassify(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        namespace: ClassifierNamespace = None,
    ) -> tuple[str, str]:
        """异步分类一次工具调用，返回 (decision, reason)。

        decision 取值："allow" 自动放行；"deny" 硬拦截；"ask" 回退人工审批。
        """
        if self._get_reject_streak(namespace) >= self._max_reject_streak:
            self._set_reject_streak(namespace, 0)
            logger.info("approval_classifier_fallback tool=%s reason=reject_streak", tool_name)
            return "ask", "LLM 分类器连续拒绝达到阈值，回退人工审批"

        prompt = describe_tool_call(tool_name, tool_args)
        stage1 = await self._invoke_stage1(prompt)
        if stage1 is None:
            # 第一阶段异常：直接进入第二阶段复核，仍有机会给出确定结论。
            stage2 = await self._invoke_stage2(prompt, first_stage=None)
            return self._finalize(tool_name, stage2, namespace)
        if (
            stage1.get("decision") == "allow"
            and stage1.get("confidence") == "high"
        ):
            self._set_reject_streak(namespace, 0)
            reason = f"LLM 分类器一阶段高置信度放行：{stage1.get('reason') or '无理由'}"
            logger.info("approval_classifier decision=allow stage=1 tool=%s", tool_name)
            return "allow", reason
        stage2 = await self._invoke_stage2(prompt, first_stage=stage1)
        return self._finalize(tool_name, stage2, namespace)

    def classify(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        namespace: ClassifierNamespace = None,
    ) -> tuple[str, str]:
        """同步分类入口：复用两阶段逻辑，供同步执行路径与测试使用。"""
        if self._get_reject_streak(namespace) >= self._max_reject_streak:
            self._set_reject_streak(namespace, 0)
            logger.info("approval_classifier_fallback tool=%s reason=reject_streak", tool_name)
            return "ask", "LLM 分类器连续拒绝达到阈值，回退人工审批"

        prompt = describe_tool_call(tool_name, tool_args)
        stage1 = self._invoke_stage1_sync(prompt)
        if stage1 is None:
            stage2 = self._invoke_stage2_sync(prompt, first_stage=None)
            return self._finalize(tool_name, stage2, namespace)
        if (
            stage1.get("decision") == "allow"
            and stage1.get("confidence") == "high"
        ):
            self._set_reject_streak(namespace, 0)
            reason = f"LLM 分类器一阶段高置信度放行：{stage1.get('reason') or '无理由'}"
            logger.info("approval_classifier decision=allow stage=1 tool=%s", tool_name)
            return "allow", reason
        stage2 = self._invoke_stage2_sync(prompt, first_stage=stage1)
        return self._finalize(tool_name, stage2, namespace)

    def reset_reject_streak(self, *, namespace: ClassifierNamespace = None) -> None:
        """重置指定 Run 的连续拒绝计数；用户通过弹窗批准后由调用方触发。"""
        self._set_reject_streak(namespace, 0)

    # —— 内部实现 ——

    def _finalize(
        self,
        tool_name: str,
        stage2: dict[str, Any] | None,
        namespace: ClassifierNamespace,
    ) -> tuple[str, str]:
        """汇总第二阶段结论：allow 放行，block 计数，其余 fail-closed 回退。"""
        if stage2 is None:
            logger.info("approval_classifier_fallback tool=%s reason=unavailable", tool_name)
            return "ask", "LLM 分类器不可用或输出无法解析，回退人工审批"
        if stage2.get("decision") == "allow":
            self._set_reject_streak(namespace, 0)
            reason = f"LLM 分类器复核放行：{stage2.get('reason') or '无理由'}"
            logger.info("approval_classifier decision=allow stage=2 tool=%s", tool_name)
            return "allow", reason
        streak = self._get_reject_streak(namespace) + 1
        self._set_reject_streak(namespace, streak)
        reason = f"LLM 分类器复核拦截：{stage2.get('reason') or '无理由'}"
        logger.info(
            "approval_classifier decision=deny stage=2 tool=%s streak=%d",
            tool_name,
            streak,
        )
        return "deny", reason

    def _get_reject_streak(self, namespace: ClassifierNamespace) -> int:
        """读取并刷新指定 Run 的连续拒绝计数。"""
        streak = self._reject_streaks.get(namespace, 0)
        if namespace in self._reject_streaks:
            self._reject_streaks.move_to_end(namespace)
        return streak

    def _set_reject_streak(self, namespace: ClassifierNamespace, streak: int) -> None:
        """写入指定 Run 的拒绝计数，并限制命名空间缓存容量。"""
        if streak <= 0:
            self._reject_streaks.pop(namespace, None)
            return
        self._reject_streaks[namespace] = streak
        self._reject_streaks.move_to_end(namespace)
        while len(self._reject_streaks) > self._cache_limit:
            self._reject_streaks.popitem(last=False)

    def _bind(self, max_tokens: int) -> Any:
        """按阶段绑定输出预算；模型不支持绑定时退回原实例。"""
        try:
            return self._model.bind(max_tokens=max_tokens)
        except Exception:  # noqa: BLE001 - bind 失败不应阻断分类降级路径
            return self._model

    async def _invoke_stage1(self, prompt: str) -> dict[str, Any] | None:
        """第一阶段异步调用；异常返回 None 交由第二阶段兜底。"""
        try:
            bound = self._bind(STAGE1_MAX_TOKENS)
            response = await run_with_provider_retry(
                lambda: bound.ainvoke(
                    [
                        SystemMessage(content=_STAGE1_SYSTEM_PROMPT),
                        HumanMessage(content=prompt),
                    ],
                    config=_ISOLATED_RUN_CONFIG,
                ),
                self._provider_retry,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed：异常不得抛出到图执行
            logger.warning(
                "approval_classifier_error stage=1 error=%s", type(exc).__name__
            )
            return None
        return extract_verdict(_message_text(response))

    def _invoke_stage1_sync(self, prompt: str) -> dict[str, Any] | None:
        """第一阶段同步调用；异常返回 None 交由第二阶段兜底。"""
        try:
            bound = self._bind(STAGE1_MAX_TOKENS)
            response = run_with_provider_retry_sync(
                lambda: bound.invoke(
                    [
                        SystemMessage(content=_STAGE1_SYSTEM_PROMPT),
                        HumanMessage(content=prompt),
                    ],
                    config=_ISOLATED_RUN_CONFIG,
                ),
                self._provider_retry,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed：异常不得抛出到图执行
            logger.warning(
                "approval_classifier_error stage=1 error=%s", type(exc).__name__
            )
            return None
        return extract_verdict(_message_text(response))

    async def _invoke_stage2(
        self, prompt: str, first_stage: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """第二阶段异步复核；异常返回 None，由调用方 fail-closed 回退。"""
        try:
            bound = self._bind(STAGE2_MAX_TOKENS)
            response = await run_with_provider_retry(
                lambda: bound.ainvoke(
                    [
                        SystemMessage(content=_STAGE2_SYSTEM_PROMPT),
                        HumanMessage(content=_stage2_prompt(prompt, first_stage)),
                    ],
                    config=_ISOLATED_RUN_CONFIG,
                ),
                self._provider_retry,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed：异常不得抛出到图执行
            logger.warning(
                "approval_classifier_error stage=2 error=%s", type(exc).__name__
            )
            return None
        return extract_verdict(_message_text(response))

    def _invoke_stage2_sync(
        self, prompt: str, first_stage: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """第二阶段同步复核；异常返回 None，由调用方 fail-closed 回退。"""
        try:
            bound = self._bind(STAGE2_MAX_TOKENS)
            response = run_with_provider_retry_sync(
                lambda: bound.invoke(
                    [
                        SystemMessage(content=_STAGE2_SYSTEM_PROMPT),
                        HumanMessage(content=_stage2_prompt(prompt, first_stage)),
                    ],
                    config=_ISOLATED_RUN_CONFIG,
                ),
                self._provider_retry,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed：异常不得抛出到图执行
            logger.warning(
                "approval_classifier_error stage=2 error=%s", type(exc).__name__
            )
            return None
        return extract_verdict(_message_text(response))


def _stage2_prompt(prompt: str, first_stage: dict[str, Any] | None) -> str:
    """组装第二阶段输入：原始工具调用加一阶段结论（若有）。"""
    if first_stage is None:
        return f"{prompt}\n\n一阶段未能给出有效结论，请直接复核。"
    first_text = json.dumps(first_stage, ensure_ascii=False)
    return f"{prompt}\n\n一阶段结论：{first_text}"


def _message_text(response: Any) -> str:
    """从模型响应中取出文本内容，兼容 AIMessage 与带 result 的包装。"""
    if isinstance(response, AIMessage):
        content = response.content
    else:
        content = getattr(response, "content", None)
    if isinstance(content, list):
        # 内容块列表：拼接其中的纯文本块。
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")
