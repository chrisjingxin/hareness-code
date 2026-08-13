"""ZC-133 的 mock replay、指标汇总和显式 opt-in 真实模型入口。"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .fixtures import FIXTURE_VERSION, EvaluationFixture, FixtureOperation, fixture_catalog, line_block
from .simulator import InMemoryEditSimulator, SimulationOutcome, expected_old_text


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """一个待比较的模型工具契约候选。"""

    name: str
    family: str
    insertion_syntax: str


CANDIDATE_SPECS: tuple[CandidateSpec, ...] = (
    CandidateSpec("exact-string", "exact-string", "anchor-string"),
    CandidateSpec("snapshot-single-explicit", "snapshot-single", "explicit-action"),
    CandidateSpec("snapshot-single-zero-width", "snapshot-single", "zero-width"),
    CandidateSpec("snapshot-edits-explicit", "snapshot-edits", "explicit-action"),
    CandidateSpec("snapshot-edits-zero-width", "snapshot-edits", "zero-width"),
)


@dataclass(frozen=True, slots=True)
class EvaluationAttempt:
    """单个 fixture、候选和重复轮次的去敏结果。"""

    fixture_id: str
    candidate: str
    repetition: int
    schema_valid: bool
    safe: bool
    completed: bool
    recovery_success: bool | None
    silent_corruption: bool
    partial_write: bool
    error_code: str | None
    tool_calls: int
    rereads: int
    input_tokens: int
    output_tokens: int
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """按候选聚合的安全性、完成率和成本指标。"""

    candidate: str
    family: str
    insertion_syntax: str
    attempts: int
    safe_attempts: int
    completed_attempts: int
    schema_valid_attempts: int
    silent_corruption_count: int
    partial_write_count: int
    recovery_attempts: int
    recovery_successes: int
    error_codes: dict[str, int]
    completion_rate: float
    completion_ci95: tuple[float, float]
    safety_rate: float
    safety_ci95: tuple[float, float]
    schema_valid_rate: float
    recovery_rate: float | None
    median_input_tokens: float
    median_output_tokens: float
    median_tool_calls: float
    median_rereads: float
    median_latency_ms: float

    def to_dict(self) -> dict[str, object]:
        """转换为不含原文的 JSON 结构。"""

        return {
            "candidate": self.candidate,
            "family": self.family,
            "insertion_syntax": self.insertion_syntax,
            "attempts": self.attempts,
            "safe_attempts": self.safe_attempts,
            "completed_attempts": self.completed_attempts,
            "schema_valid_attempts": self.schema_valid_attempts,
            "silent_corruption_count": self.silent_corruption_count,
            "partial_write_count": self.partial_write_count,
            "recovery_attempts": self.recovery_attempts,
            "recovery_successes": self.recovery_successes,
            "error_codes": dict(sorted(self.error_codes.items())),
            "completion_rate": self.completion_rate,
            "completion_ci95": list(self.completion_ci95),
            "safety_rate": self.safety_rate,
            "safety_ci95": list(self.safety_ci95),
            "schema_valid_rate": self.schema_valid_rate,
            "recovery_rate": self.recovery_rate,
            "median_input_tokens": self.median_input_tokens,
            "median_output_tokens": self.median_output_tokens,
            "median_tool_calls": self.median_tool_calls,
            "median_rereads": self.median_rereads,
            "median_latency_ms": self.median_latency_ms,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """完整评测报告；只包含 fixture 元数据和聚合数字。"""

    mode: str
    fixture_version: str
    fixture_count: int
    fixture_ids: tuple[str, ...]
    repetitions: int
    profile_id: str | None
    model_name: str | None
    provider_label: str | None
    summaries: tuple[CandidateSummary, ...]
    decision: str
    decision_reason: str
    attempts: tuple[EvaluationAttempt, ...]

    def to_dict(self) -> dict[str, object]:
        """转换为可落盘的去敏 JSON。"""

        return {
            "mode": self.mode,
            "fixture_version": self.fixture_version,
            "fixture_count": self.fixture_count,
            "fixture_ids": list(self.fixture_ids),
            "repetitions": self.repetitions,
            "profile_id": self.profile_id,
            "model_name": self.model_name,
            "provider_label": self.provider_label,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "candidates": [summary.to_dict() for summary in self.summaries],
        }


def _line_ending(text: str, fallback: str = "\n") -> str:
    """读取原文中的换行风格，用于构造 exact-string replay。"""

    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return fallback


def _with_line_ending(new_text: str, old_text: str, source: str) -> str:
    """让 exact-string 的 new_string 保持被替换区间的行尾。"""

    if not new_text or new_text.endswith(("\n", "\r")):
        return new_text
    if old_text.endswith(("\n", "\r")):
        return new_text + _line_ending(old_text, _line_ending(source))
    return new_text


def _inserted_text(operation: FixtureOperation, source: str) -> str:
    """为 exact-string 插入候选补齐源文件换行。"""

    if operation.new_text.endswith(("\n", "\r")):
        return operation.new_text
    return operation.new_text + _line_ending(source)


def _snapshot_edit(operation: FixtureOperation, spec: CandidateSpec, source: str) -> dict[str, object]:
    """把 fixture 操作投影为 Snapshot 候选参数。"""

    if operation.kind == "insert":
        if spec.insertion_syntax == "explicit-action":
            action = "insert_before_line" if operation.insert_action == "before" else "insert_after_line"
            return {"action": action, "line": operation.start_line, "new_text": operation.new_text}
        return {
            "start_line": operation.start_line,
            "end_line": operation.start_line - 1,
            "new_text": operation.new_text,
        }
    return {
        "start_line": operation.start_line,
        "end_line": operation.end_line or operation.start_line,
        "new_text": operation.new_text,
    }


def _exact_call(fixture: EvaluationFixture, operation: FixtureOperation, index: int) -> dict[str, object]:
    """把 fixture 操作投影为 exact-string + prior-read 参数。"""

    if operation.kind == "write":
        return {
            "name": "write_file",
            "args": {"file_path": fixture.call_path, "content": operation.new_text},
        }
    old_text = expected_old_text(fixture, index)
    if operation.kind == "insert":
        anchor = old_text
        inserted = _inserted_text(operation, fixture.source)
        new_text = inserted + anchor if operation.insert_action == "before" else anchor + inserted
    else:
        new_text = _with_line_ending(operation.new_text, old_text, fixture.source)
    return {
        "name": "edit_file",
        "args": {
            "file_path": fixture.call_path,
            "snapshot_id": f"snap-{fixture.fixture_id}",
            "old_string": old_text,
            "new_string": new_text,
        },
    }


def build_replay_calls(fixture: EvaluationFixture, spec: CandidateSpec) -> tuple[dict[str, object], ...]:
    """根据 fixture 和候选 schema 生成固定 tool-call replay，不调用模型。"""

    if fixture.scenario == "recovery":
        malformed = {"name": "edit_file", "args": {"file_path": fixture.call_path}}
        return (malformed,) + build_replay_calls(
            EvaluationFixture(
                fixture_id=fixture.fixture_id,
                category=fixture.category,
                description=fixture.description,
                source=fixture.source,
                read_start=fixture.read_start,
                read_end=fixture.read_end,
                operations=fixture.operations,
                expected_content=fixture.expected_content,
                call_path=fixture.call_path,
                path=fixture.path,
                thread_id=fixture.thread_id,
                backend_id=fixture.backend_id,
            ),
            spec,
        )
    if spec.family == "exact-string":
        if fixture.fixture_id == "overlapping-edits":
            return ({"name": "edit_file", "args": {"file_path": fixture.call_path}},)
        return tuple(_exact_call(fixture, operation, index) for index, operation in enumerate(fixture.operations))
    if any(operation.kind == "write" for operation in fixture.operations):
        operation = fixture.operations[0]
        return ({"name": "write_file", "args": {"file_path": fixture.call_path, "content": operation.new_text}},)

    operations = list(fixture.operations)
    if fixture.wrong_valid_range:
        shifted: list[FixtureOperation] = []
        for operation in operations:
            shifted.append(
                FixtureOperation(
                    kind=operation.kind,
                    start_line=max(1, operation.start_line - 1),
                    end_line=(max(1, (operation.end_line or operation.start_line) - 1)),
                    new_text=operation.new_text,
                    insert_action=operation.insert_action,
                )
            )
        operations = shifted
    payloads = [_snapshot_edit(operation, spec, fixture.source) for operation in operations]
    common = {"file_path": fixture.call_path, "snapshot_id": f"snap-{fixture.fixture_id}"}
    if spec.family == "snapshot-edits":
        return ({"name": "edit_file", "args": {**common, "edits": payloads}},)
    return tuple(
        {"name": "edit_file", "args": {**common, **payload}}
        for payload in payloads
    )


def _estimate_tokens(value: object) -> int:
    """使用固定字符估算记录 mock 的输入/输出规模，不引入 tokenizer 依赖。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return max(1, math.ceil(len(encoded) / 4))


def _attempt(
    fixture: EvaluationFixture,
    spec: CandidateSpec,
    repetition: int,
    calls: tuple[dict[str, object], ...],
) -> EvaluationAttempt:
    """运行一次完整 replay，并把内容比较收敛为布尔指标。"""

    started = time.perf_counter()
    simulator = InMemoryEditSimulator(fixture)
    snapshot = simulator.read(
        thread_id=fixture.thread_id,
        path=fixture.path,
        start_line=fixture.read_start,
        end_line=fixture.read_end,
    )
    if snapshot is None:
        return EvaluationAttempt(
            fixture.fixture_id, spec.name, repetition, False, False, False, None,
            True, False, "READ_FAILED", 0, 0, 0, 0, (time.perf_counter() - started) * 1000,
        )
    if fixture.external_after_read is not None:
        simulator.external_change(fixture.external_after_read)
    if fixture.expire_snapshot:
        simulator.expire_snapshot()

    outcomes: list[SimulationOutcome] = []
    rereads = 1
    for index, call in enumerate(calls):
        if spec.family == "exact-string" and index > 0:
            simulator.read(
                thread_id=fixture.thread_id,
                path=fixture.path,
                start_line=fixture.read_start,
                end_line=fixture.read_end,
            )
            rereads += 1
        outcome = simulator.execute(
            spec.name,
            call,
            thread_id=fixture.call_thread_id,
            commit_content=fixture.external_before_commit if index == 0 else None,
        )
        outcomes.append(outcome)
        if not outcome.ok and not (fixture.scenario == "recovery" and index == 0):
            break

    final_content = simulator.current_content
    writes = sum(outcome.writes for outcome in outcomes)
    schema_valid = bool(outcomes) and all(outcome.schema_valid for outcome in outcomes)
    error_codes = [outcome.code for outcome in outcomes if outcome.code is not None]
    expected_final = fixture.expected_content
    if fixture.scenario == "expected_error":
        expected_final = fixture.external_before_commit or fixture.external_after_read or fixture.source
        completed = any(outcome.code == fixture.expected_error for outcome in outcomes)
    else:
        completed = final_content == expected_final and bool(outcomes) and outcomes[-1].ok
    partial_write = any(
        not outcome.ok and sum(previous.writes for previous in outcomes[:index]) > 0
        for index, outcome in enumerate(outcomes)
    )
    if fixture.scenario == "success" and not completed and writes == 0:
        expected_final = simulator.current_content
    silent_corruption = final_content != expected_final and not partial_write
    safe = not silent_corruption and not partial_write
    recovery_success = completed if fixture.scenario == "recovery" else None
    input_tokens = _estimate_tokens({"fixture": fixture.fixture_id, "task": fixture.description, "read": [fixture.read_start, fixture.read_end]})
    output_tokens = sum(_estimate_tokens(call) for call in calls)
    return EvaluationAttempt(
        fixture_id=fixture.fixture_id,
        candidate=spec.name,
        repetition=repetition,
        schema_valid=schema_valid,
        safe=safe,
        completed=completed,
        recovery_success=recovery_success,
        silent_corruption=silent_corruption,
        partial_write=partial_write,
        error_code=error_codes[-1] if error_codes else None,
        tool_calls=len(calls),
        rereads=rereads,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=(time.perf_counter() - started) * 1000,
    )


def _wilson(successes: int, attempts: int) -> tuple[float, float]:
    """返回二项比例的 95% Wilson 区间。"""

    if attempts == 0:
        return (0.0, 0.0)
    z = 1.96
    proportion = successes / attempts
    denominator = 1 + z * z / attempts
    centre = proportion + z * z / (2 * attempts)
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * attempts)) / attempts)
    return ((centre - margin) / denominator, (centre + margin) / denominator)


def _summarize(spec: CandidateSpec, attempts: list[EvaluationAttempt]) -> CandidateSummary:
    """把 attempt 列表聚合为报告可用的指标。"""

    total = len(attempts)
    completed = sum(attempt.completed for attempt in attempts)
    safe = sum(attempt.safe for attempt in attempts)
    schema_valid = sum(attempt.schema_valid for attempt in attempts)
    recovery = [attempt for attempt in attempts if attempt.recovery_success is not None]
    return CandidateSummary(
        candidate=spec.name,
        family=spec.family,
        insertion_syntax=spec.insertion_syntax,
        attempts=total,
        safe_attempts=safe,
        completed_attempts=completed,
        schema_valid_attempts=schema_valid,
        silent_corruption_count=sum(attempt.silent_corruption for attempt in attempts),
        partial_write_count=sum(attempt.partial_write for attempt in attempts),
        recovery_attempts=len(recovery),
        recovery_successes=sum(bool(attempt.recovery_success) for attempt in recovery),
        error_codes=dict(Counter(attempt.error_code for attempt in attempts if attempt.error_code)),
        completion_rate=completed / total if total else 0.0,
        completion_ci95=_wilson(completed, total),
        safety_rate=safe / total if total else 0.0,
        safety_ci95=_wilson(safe, total),
        schema_valid_rate=schema_valid / total if total else 0.0,
        recovery_rate=(sum(bool(attempt.recovery_success) for attempt in recovery) / len(recovery)) if recovery else None,
        median_input_tokens=statistics.median(attempt.input_tokens for attempt in attempts),
        median_output_tokens=statistics.median(attempt.output_tokens for attempt in attempts),
        median_tool_calls=statistics.median(attempt.tool_calls for attempt in attempts),
        median_rereads=statistics.median(attempt.rereads for attempt in attempts),
        median_latency_ms=statistics.median(attempt.latency_ms for attempt in attempts),
    )


def _choose_decision(summaries: Iterable[CandidateSummary], *, mode: str) -> tuple[str, str]:
    """按安全优先门槛选择唯一结果；证据不足时保留 exact-string。"""

    values = tuple(summaries)
    baseline = next(summary for summary in values if summary.candidate == "exact-string")
    vetoed = [
        summary.candidate
        for summary in values
        if summary.candidate != "exact-string"
        and (summary.silent_corruption_count > 0 or summary.partial_write_count > 0)
    ]
    eligible = [
        summary
        for summary in values
        if summary.candidate != "exact-string"
        and summary.silent_corruption_count == 0
        and summary.partial_write_count == 0
        and summary.completion_ci95[0] >= baseline.completion_ci95[0]
    ]
    if not eligible:
        reason = "安全门槛未产生可替代 exact-string 的候选"
        if vetoed:
            reason += f"；以下候选被 silent corruption/partial write 一票否决：{', '.join(vetoed)}"
        if mode == "mock":
            reason += "；mock replay 不能替代真实企业弱模型证据"
        return "exact-string", reason
    selected = max(eligible, key=lambda summary: (summary.completion_rate, -summary.median_input_tokens))
    return selected.candidate, "候选满足安全门槛和基线完成率下界，可进入设计评审"


def _build_report(
    *,
    mode: str,
    fixtures: tuple[EvaluationFixture, ...],
    repetitions: int,
    attempts: list[EvaluationAttempt],
    profile_id: str | None = None,
    model_name: str | None = None,
    provider_label: str | None = None,
) -> EvaluationReport:
    """构造聚合报告。"""

    summaries = tuple(
        _summarize(
            spec,
            [attempt for attempt in attempts if attempt.candidate == spec.name],
        )
        for spec in CANDIDATE_SPECS
        if any(attempt.candidate == spec.name for attempt in attempts)
    )
    decision, reason = _choose_decision(summaries, mode=mode)
    return EvaluationReport(
        mode=mode,
        fixture_version=FIXTURE_VERSION,
        fixture_count=len(fixtures),
        fixture_ids=tuple(fixture.fixture_id for fixture in fixtures),
        repetitions=repetitions,
        profile_id=profile_id,
        model_name=model_name,
        provider_label=provider_label,
        summaries=summaries,
        decision=decision,
        decision_reason=reason,
        attempts=tuple(attempts),
    )


def run_mock_evaluation(
    *,
    repetitions: int = 3,
    candidates: tuple[CandidateSpec, ...] = CANDIDATE_SPECS,
    fixtures: tuple[EvaluationFixture, ...] | None = None,
) -> EvaluationReport:
    """运行默认 mock replay；不访问模型、密钥或真实工作区。"""

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    selected_fixtures = fixtures or fixture_catalog()
    attempts: list[EvaluationAttempt] = []
    for repetition in range(1, repetitions + 1):
        for fixture in selected_fixtures:
            for spec in candidates:
                attempts.append(_attempt(fixture, spec, repetition, build_replay_calls(fixture, spec)))
    return _build_report(
        mode="mock",
        fixtures=selected_fixtures,
        repetitions=repetitions,
        attempts=attempts,
    )


def _message_text(message: object) -> str:
    """提取模型消息文本，不在错误或报告中回显。"""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content)


def _parse_model_call(message: object) -> dict[str, object] | None:
    """从真实模型响应提取 JSON tool call；失败只返回 None。"""

    tool_calls = getattr(message, "tool_calls", None)
    if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[0], dict):
        call = tool_calls[0]
        name = call.get("name")
        args = call.get("args")
        if isinstance(name, str) and isinstance(args, dict):
            return {"name": name, "args": args}
    text = _message_text(message).strip()
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            decoded = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("tool"), str):
        return None
    args = decoded.get("args")
    return {"name": decoded["tool"], "args": args} if isinstance(args, dict) else None


def _operation_instruction(fixture: EvaluationFixture) -> str:
    """把 fixture 的目标变更写成模型可执行的合成任务说明。"""

    instructions: list[str] = []
    for operation in fixture.operations:
        end_line = operation.end_line or operation.start_line
        if operation.kind == "write":
            instructions.append("尝试使用 write_file 写入已有文件")
        elif operation.kind == "replace":
            instructions.append(
                f"将第 {operation.start_line}-{end_line} 行替换为：{operation.new_text}"
            )
        elif operation.kind == "delete":
            instructions.append(f"删除第 {operation.start_line}-{end_line} 行")
        elif operation.kind == "insert":
            position = "前" if operation.insert_action == "before" else "后"
            instructions.append(
                f"在第 {operation.start_line} 行{position}插入：{operation.new_text}"
            )
    return "；".join(instructions) or fixture.description


def _model_prompt(fixture: EvaluationFixture, spec: CandidateSpec) -> str:
    """生成只含合成代码的模型评测 prompt。"""

    excerpt = line_block(fixture.source, fixture.read_start, fixture.read_end) if fixture.source else "<empty file>"
    path = fixture.call_path
    snapshot_id = f"snap-{fixture.fixture_id}"
    first_operation = fixture.operations[0] if fixture.operations else FixtureOperation("replace")
    first_end_line = first_operation.end_line or first_operation.start_line
    if first_operation.kind == "insert" and spec.insertion_syntax == "explicit-action":
        first_edit_contract = (
            f'{{"action":{json.dumps("insert_before_line" if first_operation.insert_action == "before" else "insert_after_line")},'
            f'"line":{first_operation.start_line},"new_text":"..."}}'
        )
        syntax_note = "插入使用 action 与 line。"
    elif first_operation.kind == "insert":
        first_edit_contract = (
            f'{{"start_line":{first_operation.start_line},"end_line":{first_operation.start_line - 1},'
            '"new_text":"..."}'
        )
        syntax_note = "插入使用 zero-width range，end_line 必须等于 start_line - 1。"
    else:
        first_edit_contract = (
            f'{{"start_line":{first_operation.start_line},"end_line":{first_end_line},'
            '"new_text":"..."}'
        )
        syntax_note = "替换或删除使用 start_line、end_line 与 new_text。"
    first_edit = json.loads(first_edit_contract)
    if spec.family == "exact-string":
        contract = json.dumps(
            {
                "tool": "edit_file",
                "args": {
                    "file_path": path,
                    "snapshot_id": snapshot_id,
                    "old_string": "...",
                    "new_string": "...",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        syntax_note = "已读文件为空时，使用空 old_string 写入初始内容；非空文件不能使用空匹配。"
    elif spec.family == "snapshot-single":
        contract = json.dumps(
            {
                "tool": "edit_file",
                "args": {"file_path": path, "snapshot_id": snapshot_id, **first_edit},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        contract = json.dumps(
            {
                "tool": "edit_file",
                "args": {"file_path": path, "snapshot_id": snapshot_id, "edits": [first_edit]},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if any(operation.kind == "write" for operation in fixture.operations):
        contract = json.dumps(
            {"tool": "write_file", "args": {"file_path": path, "content": "..."}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        syntax_note = ""
    contract += f" {syntax_note}" if syntax_note else ""
    return (
        "你正在参加 Harness Code 文件编辑接口评测。只处理下面的合成 fixture，不要猜测其他文件。"
        f"任务：{_operation_instruction(fixture)}（场景：{fixture.description}）\n候选契约：{contract}\n"
        f"已读区间：{fixture.read_start}-{fixture.read_end}\n文件内容：\n{excerpt}\n"
        "只返回一个 JSON 对象，格式为 {\"tool\": string, \"args\": object}，不要 Markdown、解释或额外字段。"
    )


async def run_real_evaluation(
    *,
    profile_id: str,
    workspace: Path,
    home: Path | None,
    config_path: Path | None,
    repetitions: int = 3,
    candidates: tuple[CandidateSpec, ...] = CANDIDATE_SPECS,
    fixtures: tuple[EvaluationFixture, ...] | None = None,
) -> EvaluationReport:
    """显式 opt-in 调用实际 Profile；失败不记录响应、凭据或源码正文。"""

    from langchain_core.messages import HumanMessage

    from harness_agent.config.config import load_config
    from harness_agent.extensions.providers.harness_gateway import create_openai_compatible_model

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    selected_fixtures = fixtures or fixture_catalog()
    config = load_config(workspace=workspace, home=home, config_path=config_path)
    profile = config.require_model_profile(profile_id)
    model = create_openai_compatible_model(profile.settings)
    attempts: list[EvaluationAttempt] = []
    for repetition in range(1, repetitions + 1):
        for fixture in selected_fixtures:
            for spec in candidates:
                started = time.perf_counter()
                simulator = InMemoryEditSimulator(fixture)
                snapshot = simulator.read(
                    thread_id=fixture.thread_id,
                    path=fixture.path,
                    start_line=fixture.read_start,
                    end_line=fixture.read_end,
                )
                if snapshot is None:
                    attempts.append(EvaluationAttempt(
                        fixture.fixture_id, spec.name, repetition, False, False, False, None,
                        True, False, "READ_FAILED", 0, 0, 0, 0, (time.perf_counter() - started) * 1000,
                    ))
                    continue
                if fixture.external_after_read is not None:
                    simulator.external_change(fixture.external_after_read)
                if fixture.expire_snapshot:
                    simulator.expire_snapshot()
                prompt = _model_prompt(fixture, spec)
                input_tokens = max(1, math.ceil(len(prompt) / 4))
                try:
                    response = await model.ainvoke([HumanMessage(content=prompt)])
                    call = _parse_model_call(response)
                except Exception:
                    call = None
                if call is None:
                    attempts.append(EvaluationAttempt(
                        fixture.fixture_id, spec.name, repetition, False, True, False, None,
                        False, False, "MODEL_OUTPUT_INVALID", 1, 1,
                        input_tokens, 0, (time.perf_counter() - started) * 1000,
                    ))
                    continue
                output_tokens = _estimate_tokens(call)
                outcome = simulator.execute(
                    spec.name,
                    call,
                    thread_id=fixture.call_thread_id,
                    commit_content=fixture.external_before_commit,
                )
                expected = fixture.expected_content
                if fixture.scenario == "expected_error":
                    exact_can_bypass_snapshot_expiry = (
                        fixture.expected_error == "SNAPSHOT_EXPIRED" and spec.family == "exact-string"
                    )
                    if exact_can_bypass_snapshot_expiry:
                        completed = outcome.ok and outcome.content == expected
                    else:
                        expected = fixture.external_before_commit or fixture.external_after_read or fixture.source
                        completed = outcome.code == fixture.expected_error
                else:
                    completed = outcome.ok and outcome.content == expected
                partial = outcome.writes > 0 and not completed
                silent = outcome.content != expected and not partial
                attempts.append(EvaluationAttempt(
                    fixture_id=fixture.fixture_id,
                    candidate=spec.name,
                    repetition=repetition,
                    schema_valid=outcome.schema_valid,
                    safe=not silent and not partial,
                    completed=completed,
                    recovery_success=completed if fixture.scenario == "recovery" else None,
                    silent_corruption=silent,
                    partial_write=partial,
                    error_code=outcome.code,
                    tool_calls=1,
                    rereads=1,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ))
    return _build_report(
        mode="real",
        fixtures=selected_fixtures,
        repetitions=repetitions,
        attempts=attempts,
        profile_id=profile_id,
        model_name=profile.settings.name,
        provider_label=profile.settings.provider_label,
    )


def write_report(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path]:
    """写入去敏 JSON/Markdown 报告并返回两个路径。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "zc133-evaluation.json"
    markdown_path = output_dir / "zc133-evaluation.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rows = [
        "# ZC-133 弱模型文件编辑评测报告",
        "",
        f"- mode: `{report.mode}`",
        f"- fixture version: `{report.fixture_version}`",
        f"- fixture count: `{report.fixture_count}`",
        f"- repetitions: `{report.repetitions}`",
        f"- profile: `{report.profile_id or 'mock'}`",
        f"- decision: `{report.decision}`",
        f"- decision reason: {report.decision_reason}",
        "",
        "| candidate | attempts | safe | silent corruption | partial write | completion | schema valid | median input tokens | median tool calls | median rereads |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in report.summaries:
        rows.append(
            f"| `{summary.candidate}` | {summary.attempts} | {summary.safe_attempts} | "
            f"{summary.silent_corruption_count} | {summary.partial_write_count} | "
            f"{summary.completion_rate:.3f} | {summary.schema_valid_rate:.3f} | "
            f"{summary.median_input_tokens:.1f} | {summary.median_tool_calls:.1f} | "
            f"{summary.median_rereads:.1f} |"
        )
    rows.extend([
        "",
        "报告只包含 fixture ID、候选名称和聚合指标；不包含合成源码、工具参数、模型原始响应、API Key、Header 或 endpoint。",
        "真实模型模式必须由调用者显式传入 `--real-model --profile <id>`，自动测试只使用 mock replay。",
    ])
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return json_path, markdown_path


def utc_report_stamp() -> str:
    """为 CLI 日志提供不含用户数据的 UTC 时间戳。"""

    return datetime.now(UTC).isoformat(timespec="seconds")
