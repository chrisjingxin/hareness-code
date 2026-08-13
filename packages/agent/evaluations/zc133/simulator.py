"""ZC-133 的纯内存文件编辑 simulator。

该模块不调用生产工具、不读取文件系统，也不实现 SnapshotStore。它只把候选模型参数
转换成内存中的 proposed content，用来验证 stale、scope、seen range、overlap 和原子性。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .fixtures import EvaluationFixture, line_block, split_source_lines


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """评测专用的短 Snapshot 句柄及其隐藏验证数据。"""

    snapshot_id: str
    thread_id: str
    path: str
    backend_id: str
    content: str
    content_hash: str
    seen_lines: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    """一次候选工具调用的稳定、去敏结果。"""

    ok: bool
    code: str | None
    changed: bool
    writes: int
    schema_valid: bool
    content: str


@dataclass(frozen=True, slots=True)
class _LineEdit:
    """已完成参数类型检查的行编辑。"""

    start_line: int
    end_line: int
    new_text: str
    insertion: bool = False


def content_hash(content: str) -> str:
    """计算 simulator 内部使用的强内容身份。"""

    return sha256(content.encode("utf-8")).hexdigest()


def _line_count(content: str) -> int:
    return len(split_source_lines(content))


def _line_ending(text: str, fallback: str = "\n") -> str:
    """读取文本中的首个换行风格。"""

    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return fallback


def _replacement_text(new_text: str, removed: str, source: str) -> str:
    """补齐替换文本的行尾，同时保持已有 CRLF/LF 语义。"""

    if not new_text or new_text.endswith(("\n", "\r")):
        return new_text
    if not removed.endswith(("\n", "\r")):
        return new_text
    return new_text + _line_ending(removed, _line_ending(source))


def _apply_edits(content: str, edits: list[_LineEdit]) -> str:
    """按原始行号从后向前应用已验证的内存编辑。"""

    lines = split_source_lines(content)
    for edit in sorted(edits, key=lambda item: item.start_line, reverse=True):
        if edit.insertion:
            index = edit.start_line - 1
            if edit.end_line == edit.start_line + 1:
                index = edit.start_line
            inserted = edit.new_text
            if inserted and not inserted.endswith(("\n", "\r")):
                inserted += _line_ending(content)
            lines[index:index] = [inserted] if inserted else []
            continue
        start = edit.start_line - 1
        end = edit.end_line
        removed = "".join(lines[start:end])
        replacement = _replacement_text(edit.new_text, removed, content)
        lines[start:end] = split_source_lines(replacement) if replacement else []
    return "".join(lines)


def _failure(
    code: str,
    content: str,
    *,
    schema_valid: bool = True,
) -> SimulationOutcome:
    """构造不写盘的稳定失败结果。"""

    return SimulationOutcome(
        ok=False,
        code=code,
        changed=False,
        writes=0,
        schema_valid=schema_valid,
        content=content,
    )


class InMemoryEditSimulator:
    """执行候选文件工具调用，并在每次失败时保持内容不变。"""

    def __init__(self, fixture: EvaluationFixture) -> None:
        """以 fixture 原文初始化一个隔离的内存文件。"""

        self.fixture = fixture
        self.current_content = fixture.source
        self.snapshot: SnapshotRecord | None = None
        self.baseline_content: str | None = None
        self.baseline_read_content: str | None = None
        self.baseline_thread_id: str | None = None

    def read(self, *, thread_id: str, path: str, start_line: int, end_line: int) -> SnapshotRecord | None:
        """模拟一次局部读取并记录 seen range；错误输入不创建 Snapshot。"""

        if path != self.fixture.path:
            return None
        total = _line_count(self.current_content)
        if total == 0:
            if start_line != 1 or end_line != 1:
                return None
        elif start_line < 1 or end_line < start_line or end_line > total:
            return None
        snapshot = SnapshotRecord(
            snapshot_id=f"snap-{self.fixture.fixture_id}",
            thread_id=thread_id,
            path=path,
            backend_id=self.fixture.backend_id,
            content=self.current_content,
            content_hash=content_hash(self.current_content),
            seen_lines=((start_line, end_line),),
        )
        self.snapshot = snapshot
        self.baseline_content = self.current_content
        self.baseline_read_content = line_block(self.current_content, start_line, end_line)
        self.baseline_thread_id = thread_id
        return snapshot

    def external_change(self, content: str) -> None:
        """注入 fixture 声明的外部 IDE/保存钩子变化。"""

        self.current_content = content

    def expire_snapshot(self) -> None:
        """模拟 LRU/TTL 淘汰。"""

        self.snapshot = None

    def execute(
        self,
        candidate: str,
        call: dict[str, Any],
        *,
        thread_id: str,
        commit_content: str | None = None,
    ) -> SimulationOutcome:
        """执行一个 exact-string 或 Snapshot 候选调用。"""

        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            return _failure("INVALID_TOOL_CALL", self.current_content, schema_valid=False)
        args = call.get("args")
        if not isinstance(args, dict):
            return _failure("INVALID_TOOL_ARGS", self.current_content, schema_valid=False)
        path = args.get("file_path")
        if not isinstance(path, str):
            return _failure("INVALID_TOOL_ARGS", self.current_content, schema_valid=False)
        if path != self.fixture.path:
            return _failure("SNAPSHOT_SCOPE_MISMATCH", self.current_content)

        name = call["name"]
        if name == "write_file":
            return _failure("FILE_ALREADY_EXISTS", self.current_content)
        if name != "edit_file":
            return _failure("UNKNOWN_TOOL", self.current_content, schema_valid=False)

        if candidate == "exact-string":
            return self._execute_exact(args, thread_id=thread_id, commit_content=commit_content)
        if candidate.startswith("snapshot-"):
            return self._execute_snapshot(candidate, args, thread_id=thread_id, commit_content=commit_content)
        return _failure("UNKNOWN_CANDIDATE", self.current_content, schema_valid=False)

    def _execute_exact(
        self,
        args: dict[str, Any],
        *,
        thread_id: str,
        commit_content: str | None,
    ) -> SimulationOutcome:
        """执行 exact-string + prior-read 基线。"""

        old_text = args.get("old_string")
        new_text = args.get("new_string")
        snapshot_id = args.get("snapshot_id")
        if (
            not isinstance(snapshot_id, str)
            or not isinstance(old_text, str)
            or not isinstance(new_text, str)
        ):
            return _failure("INVALID_TOOL_ARGS", self.current_content, schema_valid=False)
        snapshot = self.snapshot
        if snapshot is None:
            return _failure("SNAPSHOT_EXPIRED", self.current_content)
        if (
            snapshot_id != snapshot.snapshot_id
            or thread_id != snapshot.thread_id
            or args.get("file_path") != snapshot.path
        ):
            return _failure("SNAPSHOT_SCOPE_MISMATCH", self.current_content)
        if content_hash(self.current_content) != snapshot.content_hash:
            return _failure("STALE_FILE", self.current_content)
        if self.baseline_content is None or self.baseline_read_content is None:
            return _failure("SNAPSHOT_REQUIRED", self.current_content)
        if not old_text:
            if self.baseline_content != "":
                return _failure("INVALID_EDIT", self.current_content)
            proposed = new_text
        else:
            if old_text not in self.baseline_read_content:
                if old_text in self.current_content:
                    return _failure("UNREAD_RANGE", self.current_content)
                return _failure("OLD_TEXT_NOT_FOUND", self.current_content)
            if self.baseline_read_content.count(old_text) != 1:
                return _failure("AMBIGUOUS_MATCH", self.current_content)
            proposed = self.baseline_content.replace(old_text, new_text, 1)
        if proposed == self.current_content:
            return _failure("NO_CHANGES", self.current_content)
        if commit_content is not None:
            self.current_content = commit_content
            return _failure("COMMIT_CONFLICT", self.current_content)
        self.current_content = proposed
        return SimulationOutcome(True, None, True, 1, True, self.current_content)

    def _execute_snapshot(
        self,
        candidate: str,
        args: dict[str, Any],
        *,
        thread_id: str,
        commit_content: str | None,
    ) -> SimulationOutcome:
        """执行 Snapshot 单 edit 或 edits[] 候选。"""

        snapshot = self.snapshot
        if snapshot is None:
            return _failure("SNAPSHOT_EXPIRED", self.current_content)
        if (
            args.get("snapshot_id") != snapshot.snapshot_id
            or thread_id != snapshot.thread_id
            or args.get("file_path") != snapshot.path
        ):
            return _failure("SNAPSHOT_SCOPE_MISMATCH", self.current_content)
        if content_hash(self.current_content) != snapshot.content_hash:
            return _failure("STALE_FILE", self.current_content)

        family = "snapshot-edits" if candidate.startswith("snapshot-edits") else "snapshot-single"
        if family == "snapshot-edits":
            raw_edits = args.get("edits")
            if not isinstance(raw_edits, list) or not raw_edits:
                return _failure("INVALID_TOOL_ARGS", self.current_content, schema_valid=False)
        else:
            raw_edits = [args]
        edits: list[_LineEdit] = []
        for raw_edit in raw_edits:
            if not isinstance(raw_edit, dict):
                return _failure("INVALID_TOOL_ARGS", self.current_content, schema_valid=False)
            parsed = self._parse_edit(raw_edit, candidate)
            if parsed is None:
                return _failure("INVALID_RANGE", self.current_content, schema_valid=False)
            if not self._valid_bounds(parsed):
                return _failure("INVALID_RANGE", self.current_content)
            if not self._seen(parsed, snapshot.seen_lines):
                return _failure("UNREAD_RANGE", self.current_content)
            edits.append(parsed)
        if self._overlaps(edits):
            return _failure("OVERLAPPING_EDITS", self.current_content)
        proposed = _apply_edits(self.current_content, edits)
        if proposed == self.current_content:
            return _failure("NO_CHANGES", self.current_content)
        if commit_content is not None:
            self.current_content = commit_content
            return _failure("COMMIT_CONFLICT", self.current_content)
        self.current_content = proposed
        return SimulationOutcome(True, None, True, 1, True, self.current_content)

    def _valid_bounds(self, edit: _LineEdit) -> bool:
        """区分文件越界与“文件内但模型未读”的范围错误。"""

        total = _line_count(self.current_content)
        if edit.insertion:
            return edit.start_line == 1 if total == 0 else 1 <= edit.start_line <= total
        return total > 0 and 1 <= edit.start_line <= edit.end_line <= total

    def _parse_edit(self, raw: dict[str, Any], candidate: str) -> _LineEdit | None:
        """解析显式插入 action 或仅供候选比较的 zero-width 语法。"""

        new_text = raw.get("new_text")
        if not isinstance(new_text, str):
            return None
        action = raw.get("action")
        if action in {"insert_before_line", "insert_after_line"}:
            line = raw.get("line")
            if not isinstance(line, int) or isinstance(line, bool):
                return None
            return _LineEdit(
                start_line=line,
                end_line=line if action == "insert_before_line" else line + 1,
                new_text=new_text,
                insertion=True,
            )
        start = raw.get("start_line")
        end = raw.get("end_line")
        if not isinstance(start, int) or isinstance(start, bool):
            return None
        if not isinstance(end, int) or isinstance(end, bool):
            return None
        if end < start and candidate.endswith("zero-width") and end == start - 1:
            return _LineEdit(start, end, new_text, insertion=True)
        if start < 1 or end < start:
            return None
        return _LineEdit(start, end, new_text)

    @staticmethod
    def _seen(edit: _LineEdit, ranges: tuple[tuple[int, int], ...]) -> bool:
        """判断源区间或插入锚点是否属于模型已读范围。"""

        required = edit.start_line if edit.insertion else range(edit.start_line, edit.end_line + 1)
        values = [required] if isinstance(required, int) else required
        return all(any(start <= line <= end for start, end in ranges) for line in values)

    @staticmethod
    def _overlaps(edits: list[_LineEdit]) -> bool:
        """拒绝同批重叠替换或落在替换区间内的插入。"""

        occupied: set[int] = set()
        for edit in edits:
            current = {edit.start_line} if edit.insertion else set(range(edit.start_line, edit.end_line + 1))
            if occupied & current:
                return True
            occupied.update(current)
        return False


def expected_old_text(fixture: EvaluationFixture, operation_index: int = 0) -> str:
    """返回 exact-string replay 使用的原始区间文本。"""

    operation = fixture.operations[operation_index]
    end_line = operation.end_line or operation.start_line
    return line_block(fixture.source, operation.start_line, end_line)
