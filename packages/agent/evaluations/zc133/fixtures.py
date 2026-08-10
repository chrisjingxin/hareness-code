"""ZC-133 使用的合成文件编辑 fixture。

fixture 只包含去业务化文本和期望操作。评测报告只写入 fixture ID、类别和聚合指标，
不会写入源码、old_string、new_text 或模型原始响应。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FIXTURE_VERSION = "zc133-v1"
OperationKind = Literal["replace", "insert", "delete", "write"]
InsertAction = Literal["before", "after"]
Scenario = Literal["success", "expected_error", "recovery"]


@dataclass(frozen=True, slots=True)
class FixtureOperation:
    """描述一个不包含模型参数的期望文件变更。"""

    kind: OperationKind
    start_line: int = 1
    end_line: int | None = None
    new_text: str = ""
    insert_action: InsertAction | None = None


@dataclass(frozen=True, slots=True)
class EvaluationFixture:
    """一个可重复执行的合成编辑场景。"""

    fixture_id: str
    category: str
    description: str
    source: str
    read_start: int
    read_end: int
    operations: tuple[FixtureOperation, ...]
    expected_content: str
    scenario: Scenario = "success"
    expected_error: str | None = None
    external_after_read: str | None = None
    external_before_commit: str | None = None
    call_thread_id: str = "thread-a"
    call_path: str = "/src/fixture.txt"
    expire_snapshot: bool = False
    wrong_valid_range: bool = False
    path: str = "/src/fixture.txt"
    thread_id: str = "thread-a"
    backend_id: str = "local-fixture"


def split_source_lines(content: str) -> list[str]:
    """按原始换行切分文本，保留 BOM、CRLF 和末尾换行。"""

    return content.splitlines(keepends=True)


def line_block(content: str, start_line: int, end_line: int) -> str:
    """读取 1-based 闭区间的原始文本，用于构造 exact-string 回放。"""

    lines = split_source_lines(content)
    return "".join(lines[start_line - 1 : end_line])


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
    """为行编辑补齐被替换区间的换行，但不改变文本编码标记。"""

    if not new_text or new_text.endswith(("\n", "\r")):
        return new_text
    if not removed.endswith(("\n", "\r")):
        return new_text
    ending = _line_ending(removed, _line_ending(source))
    return f"{new_text}{ending}"


def apply_expected_operations(
    source: str,
    operations: tuple[FixtureOperation, ...],
) -> str:
    """在 fixture 构建阶段生成期望内容；此函数不访问文件系统。"""

    lines = split_source_lines(source)
    for operation in sorted(operations, key=lambda item: item.start_line, reverse=True):
        if operation.kind == "write":
            continue
        if operation.kind == "insert":
            index = operation.start_line - 1 if operation.insert_action == "before" else operation.start_line
            ending = _line_ending(source)
            inserted = operation.new_text
            if inserted and not inserted.endswith(("\n", "\r")):
                inserted += ending
            lines[index:index] = [inserted] if inserted else []
            continue
        end_line = operation.end_line or operation.start_line
        removed = "".join(lines[operation.start_line - 1 : end_line])
        replacement = _replacement_text(operation.new_text, removed, source)
        lines[operation.start_line - 1 : end_line] = (
            split_source_lines(replacement) if replacement else []
        )
    return "".join(lines)


def _fixture(
    fixture_id: str,
    category: str,
    description: str,
    source: str,
    read_start: int,
    read_end: int,
    operations: tuple[FixtureOperation, ...],
    *,
    scenario: Scenario = "success",
    expected_error: str | None = None,
    external_after_read: str | None = None,
    external_before_commit: str | None = None,
    call_thread_id: str = "thread-a",
    call_path: str = "/src/fixture.txt",
    expire_snapshot: bool = False,
    wrong_valid_range: bool = False,
) -> EvaluationFixture:
    """创建 fixture 并集中计算成功场景的期望内容。"""

    expected = apply_expected_operations(source, operations)
    return EvaluationFixture(
        fixture_id=fixture_id,
        category=category,
        description=description,
        source=source,
        read_start=read_start,
        read_end=read_end,
        operations=operations,
        expected_content=expected,
        scenario=scenario,
        expected_error=expected_error,
        external_after_read=external_after_read,
        external_before_commit=external_before_commit,
        call_thread_id=call_thread_id,
        call_path=call_path,
        expire_snapshot=expire_snapshot,
        wrong_valid_range=wrong_valid_range,
    )


def fixture_catalog() -> tuple[EvaluationFixture, ...]:
    """返回 ZC-133 固定版本的 24 个合成 fixture。"""

    single = "def greet():\n    return \"hello\"\n"
    ranged = "class Counter:\n    value = 1\n    limit = 2\n    return_value = value + limit\n"
    duplicate = "def first():\n    return \"same\"\n\ndef second():\n    return \"same\"\n"
    similar = "def first():\n    return 1\ndef second():\n    return 2\n"
    crlf = "alpha\r\nbeta\r\n"
    bom = "\ufeffname = \"a\"\nname = \"b\"\n"
    long_file = "".join(f"line {number:03d}\n" for number in range(1, 161))
    long_line = f"prefix = {'x' * 1600}\n"
    multi = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n"
    stale_source = "version = 1\nvalue = \"old\"\n"
    stale_current = "version = 2\nvalue = \"old\"\n"
    commit_source = "approved = false\n"
    commit_current = "approved = externally-changed\n"

    return (
        _fixture(
            "replace-single-line", "replace", "替换一个已读源行", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "hi"'),),
        ),
        _fixture(
            "replace-range", "replace", "替换连续的两行", ranged, 1, 4,
            (FixtureOperation("replace", 2, 3, "    value = 10\n    limit = 20"),),
        ),
        _fixture(
            "delete-line", "delete", "删除一个已读源行", single, 1, 2,
            (FixtureOperation("delete", 2, 2),),
        ),
        _fixture(
            "insert-before-line", "insert", "在已读锚点前插入一行", single, 1, 2,
            (FixtureOperation("insert", 2, new_text="    log()", insert_action="before"),),
        ),
        _fixture(
            "insert-after-line", "insert", "在已读锚点后插入一行", single, 1, 2,
            (FixtureOperation("insert", 2, new_text="    log()", insert_action="after"),),
        ),
        _fixture(
            "multi-non-overlap", "multi-edit", "一次提交两个非重叠编辑", multi, 1, 8,
            (
                FixtureOperation("replace", 2, 2, "TWO"),
                FixtureOperation("replace", 6, 6, "SIX"),
            ),
        ),
        _fixture(
            "duplicate-text", "duplicate", "重复文本只修改指定函数", duplicate, 1, 5,
            (FixtureOperation("replace", 2, 2, '    return "different"'),),
        ),
        _fixture(
            "similar-function", "similar", "相似函数中修改第二个函数", similar, 1, 4,
            (FixtureOperation("replace", 4, 4, "    return 20"),),
            wrong_valid_range=True,
        ),
        _fixture(
            "long-file-local-read", "long-file", "长文件只读取目标附近区间", long_file, 96, 110,
            (FixtureOperation("replace", 101, 101, "line 101 updated"),),
        ),
        _fixture(
            "long-line", "long-line", "超长单行仍使用原始源行号", long_line, 1, 1,
            (FixtureOperation("replace", 1, 1, "prefix = updated"),),
        ),
        _fixture(
            "empty-file-insert", "empty", "空文件插入首行", "", 1, 1,
            (FixtureOperation("insert", 1, new_text="created", insert_action="before"),),
        ),
        _fixture(
            "bom-utf8", "encoding", "保留 UTF-8 BOM", bom, 1, 2,
            (FixtureOperation("replace", 2, 2, 'name = "changed"'),),
        ),
        _fixture(
            "crlf", "encoding", "保留 CRLF 换行", crlf, 1, 2,
            (FixtureOperation("replace", 2, 2, "changed"),),
        ),
        _fixture(
            "no-final-newline", "encoding", "保留无末尾换行", "alpha\nbeta", 1, 2,
            (FixtureOperation("replace", 2, 2, "changed"),),
        ),
        _fixture(
            "stale-after-read", "stale", "read 后外部修改必须拒绝", stale_source, 1, 2,
            (FixtureOperation("replace", 2, 2, 'value = "new"'),),
            scenario="expected_error", expected_error="STALE_FILE", external_after_read=stale_current,
        ),
        _fixture(
            "commit-conflict", "concurrency", "审批后提交前外部修改必须拒绝", commit_source, 1, 1,
            (FixtureOperation("replace", 1, 1, "approved = true"),),
            scenario="expected_error", expected_error="COMMIT_CONFLICT", external_before_commit=commit_current,
        ),
        _fixture(
            "expired-snapshot", "snapshot", "淘汰的 Snapshot 不能继续编辑", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "new"'),),
            scenario="expected_error", expected_error="SNAPSHOT_EXPIRED", expire_snapshot=True,
        ),
        _fixture(
            "cross-thread", "scope", "Snapshot 不能跨 Thread 复用", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "new"'),),
            scenario="expected_error", expected_error="SNAPSHOT_SCOPE_MISMATCH", call_thread_id="thread-b",
        ),
        _fixture(
            "unread-range", "seen-range", "未读源行不能被编辑", single, 1, 1,
            (FixtureOperation("replace", 2, 2, '    return "new"'),),
            scenario="expected_error", expected_error="UNREAD_RANGE",
        ),
        _fixture(
            "wrong-path", "path", "Snapshot 路径必须与调用路径一致", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "new"'),),
            scenario="expected_error", expected_error="SNAPSHOT_SCOPE_MISMATCH", call_path="/src/other.txt",
        ),
        _fixture(
            "overlapping-edits", "overlap", "同批重叠 edits 必须零写入", ranged, 1, 4,
            (
                FixtureOperation("replace", 2, 3, "first"),
                FixtureOperation("replace", 3, 4, "second"),
            ),
            scenario="expected_error", expected_error="OVERLAPPING_EDITS",
        ),
        _fixture(
            "write-existing", "write", "write_file 对已有文件必须拒绝", single, 1, 2,
            (FixtureOperation("write", new_text="replacement"),),
            scenario="expected_error", expected_error="FILE_ALREADY_EXISTS",
        ),
        _fixture(
            "malformed-recovery", "recovery", "畸形参数失败后按错误提示重试", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "recovered"'),),
            scenario="recovery",
        ),
        _fixture(
            "no-op", "no-op", "拟议内容与当前内容相同时不重复写入", single, 1, 2,
            (FixtureOperation("replace", 2, 2, '    return "hello"'),),
            scenario="expected_error", expected_error="NO_CHANGES",
        ),
        _fixture(
            "invalid-range", "invalid-range", "越过文件末尾的范围必须拒绝", single, 1, 2,
            (FixtureOperation("replace", 99, 99, "invalid"),),
            scenario="expected_error", expected_error="INVALID_RANGE",
        ),
    )
