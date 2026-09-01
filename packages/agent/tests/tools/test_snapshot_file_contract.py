"""Snapshot prior-read canonical 文件工具 contract 的行为测试。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _request(
    name: str,
    args: dict[str, Any],
    *,
    thread_id: str = "thread-a",
    call_id: str | None = None,
) -> Any:
    """创建足以驱动 contract 的最小 ToolCallRequest 形状。"""
    return SimpleNamespace(
        tool_call={"name": name, "id": call_id or f"{name}-{thread_id}", "args": args},
        runtime=SimpleNamespace(context=SimpleNamespace(thread_id=thread_id)),
    )


def _payload(message: Any) -> dict[str, Any]:
    """解析 contract 返回的稳定 JSON 工具结果。"""
    return json.loads(str(message.content))


def _contract(tmp_path: Path, **kwargs: Any):
    """使用本机安全 adapter 构造单一 canonical contract。"""
    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract

    return create_snapshot_file_tool_contract(
        object(),
        snapshot_store=ThreadSnapshotStore(),
        text_backend=LocalTextMutationBackend(tmp_path),
        **kwargs,
    )


def _contract_with_backend(tmp_path: Path, backend: Any, **kwargs: Any):
    """用注入 backend 构造 contract，覆盖保存钩子等提交后行为。"""
    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract

    return create_snapshot_file_tool_contract(
        object(),
        snapshot_store=ThreadSnapshotStore(),
        text_backend=backend,
        **kwargs,
    )


def test_historical_unverified_hunk_would_corrupt_and_is_not_a_model_entrypoint() -> None:
    """保留 ZC-132 错位 hunk 事实，确认 canonical 工具中没有该入口。"""
    original = "aaa\nbbb\nccc\n"
    lines = original.splitlines()
    lines[2:3] = ["ddd"]

    assert "\n".join(lines) + "\n" == "aaa\nbbb\nddd\n"
    from harness_agent.tools import snapshot_file_contract

    assert not hasattr(snapshot_file_contract, "apply_patch")


def test_file_contract_rejects_missing_thread_context(tmp_path: Path) -> None:
    """文件工具缺少当前 Thread 时必须 fail closed，不能落入共享 sentinel scope。"""
    request = SimpleNamespace(
        tool_call={
            "name": "read_file",
            "id": "read-without-thread",
            "args": {"file_path": "/sample.txt"},
        },
        runtime=SimpleNamespace(context=None),
    )
    (tmp_path / "sample.txt").write_text("content\n", encoding="utf-8")

    result = _payload(_contract(tmp_path).dispatch(request))

    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_CONTEXT_REQUIRED"


def test_file_contract_uses_execution_scope_for_managed_child_snapshots() -> None:
    """Managed child 文件 Snapshot 不复用公开父 Thread 的内存句柄。"""
    from harness_agent.tools.snapshot_file_contract import SnapshotFileToolContract

    request = SimpleNamespace(
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                thread_id="parent-thread",
                checkpoint_thread_id="managed-execution-child",
            )
        )
    )

    assert SnapshotFileToolContract._thread_id(request) == "managed-execution-child"


def test_read_returns_short_snapshot_and_edit_requires_seen_unique_text(tmp_path: Path) -> None:
    """局部 read 只授权看过的行；读取同版本第二窗口后才允许唯一替换。"""
    target = tmp_path / "sample.py"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    contract = _contract(tmp_path)

    first = _payload(contract.dispatch(_request("read_file", {"file_path": "/sample.py", "limit": 1})))
    assert first["ok"] is True
    assert first["shown_lines"] == {"start_line": 1, "end_line": 1}
    assert "first" in first["content"]
    assert "second" not in first["content"]

    unread = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.py",
                    "snapshot_id": first["snapshot_id"],
                    "old_string": "second\n",
                    "new_string": "changed\n",
                },
            )
        )
    )
    assert unread["error"]["code"] == "UNREAD_RANGE"
    assert target.read_text(encoding="utf-8") == "first\nsecond\nthird\n"

    second = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/sample.py", "offset": 1, "limit": 1}))
    )
    assert second["snapshot_id"] == first["snapshot_id"]
    edited = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.py",
                    "snapshot_id": second["snapshot_id"],
                    "old_string": "second\n",
                    "new_string": "changed\n",
                },
            )
        )
    )
    assert edited["ok"] is True
    assert edited["changed_range"] == {
        "start_line": 2,
        "end_line": 2,
        "added_lines": 1,
        "removed_lines": 1,
    }
    assert edited["shown_lines"] == {"start_line": 1, "end_line": 3}
    assert edited["content"] == "1\tfirst\n2\tchanged\n3\tthird"
    assert edited["diagnostics"] == {"status": "unavailable"}
    assert target.read_text(encoding="utf-8") == "first\nchanged\nthird\n"

    # 新 Snapshot 已记录返回窗口，连续 edit 不需要再次完整 read_file。
    continued = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.py",
                    "snapshot_id": edited["snapshot_id"],
                    "old_string": "changed\n",
                    "new_string": "finished\n",
                },
                call_id="edit-continued",
            )
        )
    )
    assert continued["ok"] is True
    assert target.read_text(encoding="utf-8") == "first\nfinished\nthird\n"


def test_empty_file_initialization_uses_current_snapshot_and_returns_editable_result(
    tmp_path: Path,
) -> None:
    """空文档可被整体替换，结果 Snapshot 的可见行可继续普通编辑。"""
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/empty.txt"})))

    assert read["ok"] is True
    assert read["line_count"] == 0
    assert read["shown_lines"] == {"start_line": None, "end_line": None}
    request = _request(
        "edit_file",
        {
            "file_path": "/empty.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "",
            "new_string": "first\nsecond\n",
        },
        call_id="initialize-empty",
    )
    assert contract.approval_preflight(request) is True
    description = contract.approval_description(request.tool_call, None, request.runtime)
    assert "有界 diff 预览" in description
    assert "+first" in description

    initialized = _payload(contract.dispatch(request))

    assert initialized["ok"] is True
    assert initialized["changed_range"] == {
        "start_line": 1,
        "end_line": 2,
        "added_lines": 2,
        "removed_lines": 0,
    }
    assert initialized["shown_lines"] == {"start_line": 1, "end_line": 2}
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"

    continued = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/empty.txt",
                    "snapshot_id": initialized["snapshot_id"],
                    "old_string": "second\n",
                    "new_string": "finished\n",
                },
                call_id="edit-initialized",
            )
        )
    )
    assert continued["ok"] is True
    assert target.read_text(encoding="utf-8") == "first\nfinished\n"


def test_empty_initialization_preserves_bom_mode_and_write_stays_create_only(
    tmp_path: Path,
) -> None:
    """只含 BOM 的空文本仍走 edit CAS，并保留原文件 mode。"""
    target = tmp_path / "bom-empty.txt"
    target.write_bytes(b"\xef\xbb\xbf")
    target.chmod(0o640)
    original_mode = target.stat().st_mode & 0o777
    contract = _contract(tmp_path)

    exists = _payload(
        contract.dispatch(
            _request("write_file", {"file_path": "/bom-empty.txt", "content": "overwrite\n"})
        )
    )
    assert exists["error"]["code"] == "FILE_ALREADY_EXISTS"
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/bom-empty.txt"})))
    initialized = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/bom-empty.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "",
                    "new_string": "created\n",
                },
            )
        )
    )

    assert initialized["ok"] is True
    assert target.read_bytes() == b"\xef\xbb\xbfcreated\n"
    assert target.stat().st_mode & 0o777 == original_mode


def test_empty_old_string_rejects_nonempty_stale_cross_thread_noop_and_oversize(
    tmp_path: Path,
) -> None:
    """空匹配不能成为一般插入；所有 Snapshot/大小失败都保持原字节。"""
    nonempty = tmp_path / "nonempty.txt"
    nonempty.write_text("existing\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read_nonempty = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/nonempty.txt"}))
    )
    invalid = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/nonempty.txt",
                    "snapshot_id": read_nonempty["snapshot_id"],
                    "old_string": "",
                    "new_string": "prefix\n",
                },
            )
        )
    )
    assert invalid["error"] == {
        "code": "INVALID_EDIT",
        "message": "空 old_string 只允许初始化当前 Snapshot 对应的空文件。",
        "next_action": "从已读非空内容复制唯一的 old_string 后重试。",
    }
    assert nonempty.read_text(encoding="utf-8") == "existing\n"

    stale_target = tmp_path / "stale-empty.txt"
    stale_target.write_bytes(b"")
    stale_read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/stale-empty.txt"}))
    )
    stale_target.write_text("external\n", encoding="utf-8")
    stale = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/stale-empty.txt",
                    "snapshot_id": stale_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "approved\n",
                },
            )
        )
    )
    assert stale["error"]["code"] == "STALE_FILE"
    assert stale_target.read_text(encoding="utf-8") == "external\n"

    cross_target = tmp_path / "cross-empty.txt"
    cross_target.write_bytes(b"")
    cross_read = _payload(
        contract.dispatch(
            _request("read_file", {"file_path": "/cross-empty.txt"}, thread_id="thread-a")
        )
    )
    cross = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/cross-empty.txt",
                    "snapshot_id": cross_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "cross\n",
                },
                thread_id="thread-b",
            )
        )
    )
    assert cross["error"]["code"] == "SNAPSHOT_SCOPE_MISMATCH"
    assert cross_target.read_bytes() == b""

    noop_target = tmp_path / "noop-empty.txt"
    noop_target.write_bytes(b"")
    noop_read = _payload(contract.dispatch(_request("read_file", {"file_path": "/noop-empty.txt"})))
    noop = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/noop-empty.txt",
                    "snapshot_id": noop_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "",
                },
            )
        )
    )
    assert noop["error"]["code"] == "NO_CHANGES"

    oversized_target = tmp_path / "oversized-empty.txt"
    oversized_target.write_bytes(b"")
    oversized_read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/oversized-empty.txt"}))
    )
    oversized = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/oversized-empty.txt",
                    "snapshot_id": oversized_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "x" * (64 * 1024 + 1),
                },
            )
        )
    )
    assert oversized["error"]["code"] == "EDIT_TEXT_TOO_LARGE"
    assert oversized_target.read_bytes() == b""


def test_truncated_empty_initialization_diff_remains_approvable_and_fingerprint_bound(
    tmp_path: Path,
) -> None:
    """审批只显示有界预览，但仍可批准且不能把批准套到改参内容。"""
    target = tmp_path / "large-empty.txt"
    target.write_bytes(b"")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/large-empty.txt"})))
    initial_content = "".join(f"line-{index}\n" for index in range(240))
    approved = _request(
        "edit_file",
        {
            "file_path": "/large-empty.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "",
            "new_string": initial_content,
        },
        call_id="large-empty-approval",
    )

    assert contract.approval_preflight(approved) is True
    description = contract.approval_description(approved.tool_call, None, approved.runtime)
    assert "预览因上限截断" in description
    assert "[diff 因行数或字节上限截断]" in description
    assert "批准将提交本次调用已固定的完整拟议内容" in description

    committed = _payload(contract.dispatch(approved))
    assert committed["ok"] is True
    assert target.read_text(encoding="utf-8") == initial_content

    bound_target = tmp_path / "bound-empty.txt"
    bound_target.write_bytes(b"")
    bound_read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/bound-empty.txt"}))
    )
    bound = _request(
        "edit_file",
        {
            "file_path": "/bound-empty.txt",
            "snapshot_id": bound_read["snapshot_id"],
            "old_string": "",
            "new_string": initial_content,
        },
        call_id="bound-empty-approval",
    )
    assert contract.approval_preflight(bound) is True

    changed = _request(
        "edit_file",
        {
            "file_path": "/bound-empty.txt",
            "snapshot_id": bound_read["snapshot_id"],
            "old_string": "",
            "new_string": "not-approved\n",
        },
        call_id="bound-empty-approval",
    )
    rejected = _payload(contract.dispatch(changed))
    assert rejected["error"]["code"] == "COMMIT_CONFLICT"
    replay = _payload(contract.dispatch(bound))
    assert replay["error"]["code"] == "COMMIT_CONFLICT"
    assert bound_target.read_bytes() == b""


def test_empty_initialization_conflict_consumes_approval_and_drift_returns_actual_snapshot(
    tmp_path: Path,
) -> None:
    """空初始化的旧批准不可重放，保存钩子结果只绑定实际版本。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend

    conflict_target = tmp_path / "conflict-empty.txt"
    conflict_target.write_bytes(b"")
    contract = _contract(tmp_path)
    read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/conflict-empty.txt"}))
    )
    request = _request(
        "edit_file",
        {
            "file_path": "/conflict-empty.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "",
            "new_string": "approved\n",
        },
        call_id="empty-conflict",
    )
    assert contract.approval_preflight(request) is True
    conflict_target.write_text("external\n", encoding="utf-8")
    conflict = _payload(contract.dispatch(request))
    assert conflict["error"]["code"] == "COMMIT_CONFLICT"
    conflict_target.write_bytes(b"")
    replay = _payload(contract.dispatch(request))
    assert replay["error"]["code"] == "COMMIT_CONFLICT"
    assert conflict_target.read_bytes() == b""

    class DriftAfterInitializationBackend(LocalTextMutationBackend):
        """模拟初始化提交后保存钩子立刻改写实际文件。"""

        def compare_and_replace_text(self, path: str, expected: Any, proposed: str):
            result = super().compare_and_replace_text(path, expected, proposed)
            (tmp_path / path.lstrip("/")).write_text("formatted\n", encoding="utf-8")
            return result

    drift_target = tmp_path / "drift-empty.txt"
    drift_target.write_bytes(b"")
    drift_contract = _contract_with_backend(
        tmp_path,
        DriftAfterInitializationBackend(tmp_path),
    )
    drift_read = _payload(
        drift_contract.dispatch(_request("read_file", {"file_path": "/drift-empty.txt"}))
    )
    drift = _payload(
        drift_contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/drift-empty.txt",
                    "snapshot_id": drift_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "approved\n",
                },
            )
        )
    )
    assert drift["ok"] is True
    assert drift["warning"]["code"] == "POST_WRITE_DRIFT"
    assert "+approved" in drift["warning"]["proposed_diff"]
    assert "+formatted" in drift["warning"]["actual_diff"]
    actual = _payload(
        drift_contract.dispatch(_request("read_file", {"file_path": "/drift-empty.txt"}))
    )
    assert actual["snapshot_id"] == drift["snapshot_id"]
    assert actual["content"] == "1\tformatted"


def test_empty_initialization_uses_remote_native_cas_and_rejects_read_only_provider(
    tmp_path: Path,
) -> None:
    """远端初始化只调用原生 compare-and-replace，缺少 CAS 时零写入。"""
    import hashlib

    from harness_agent.threads.text_backend import (
        ContentIdentity,
        RemoteTextMutationBackend,
        TextDocument,
        TextMutationError,
    )

    class ReadOnlyProvider:
        def __init__(self) -> None:
            self.content = ""

        def _document(self, path: str) -> TextDocument:
            raw = self.content.encode("utf-8")
            return TextDocument(
                path=path,
                content=self.content,
                identity=ContentIdentity(
                    digest=hashlib.sha256(raw).hexdigest(),
                    byte_length=len(raw),
                    line_ending="lf" if "\n" in self.content else "none",
                    has_final_newline=self.content.endswith("\n"),
                ),
            )

        def read_text_document(self, path: str) -> TextDocument:
            return self._document(path)

    class NativeCasProvider(ReadOnlyProvider):
        def compare_and_replace_text(
            self,
            path: str,
            expected: ContentIdentity,
            proposed: str,
        ) -> TextDocument:
            if self._document(path).identity != expected:
                raise TextMutationError("COMMIT_CONFLICT")
            self.content = proposed
            return self._document(path)

    native_provider = NativeCasProvider()
    native = _contract_with_backend(
        tmp_path,
        RemoteTextMutationBackend(native_provider, backend_id="remote-native"),
    )
    native_read = _payload(native.dispatch(_request("read_file", {"file_path": "/remote.txt"})))
    native_result = _payload(
        native.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/remote.txt",
                    "snapshot_id": native_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "remote\n",
                },
            )
        )
    )
    assert native_result["ok"] is True
    assert native_provider.content == "remote\n"

    read_only_provider = ReadOnlyProvider()
    read_only = _contract_with_backend(
        tmp_path,
        RemoteTextMutationBackend(read_only_provider, backend_id="remote-read-only"),
    )
    read_only_read = _payload(
        read_only.dispatch(_request("read_file", {"file_path": "/remote.txt"}))
    )
    unsupported = _payload(
        read_only.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/remote.txt",
                    "snapshot_id": read_only_read["snapshot_id"],
                    "old_string": "",
                    "new_string": "must-not-write\n",
                },
            )
        )
    )
    assert unsupported["error"]["code"] == "BACKEND_CAS_UNSUPPORTED"
    assert read_only_provider.content == ""


def test_edit_allows_unique_text_copied_from_middle_of_seen_line(tmp_path: Path) -> None:
    """模型读过整行后，可替换该行中间的唯一文本，不能误报下一行为未读。"""
    target = tmp_path / "inline.txt"
    target.write_text("prefix target suffix\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/inline.txt"})))

    edited = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/inline.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "target",
                    "new_string": "changed",
                },
            )
        )
    )

    assert edited["ok"] is True
    assert target.read_text(encoding="utf-8") == "prefix changed suffix\n"


def test_edit_rejects_overlapping_matches_as_ambiguous(tmp_path: Path) -> None:
    """old_string 的重叠出现也是多个候选位置，不能静默选择第一处。"""
    target = tmp_path / "overlapping.txt"
    target.write_text("aaa\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/overlapping.txt"})))

    rejected = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/overlapping.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "aa",
                    "new_string": "changed",
                },
            )
        )
    )

    assert rejected["error"]["code"] == "AMBIGUOUS_MATCH"
    assert target.read_text(encoding="utf-8") == "aaa\n"


def test_edit_rejects_stale_cross_thread_ambiguous_and_legacy_parameters(tmp_path: Path) -> None:
    """所有 Snapshot/unique-match 前置条件失败时保持原始字节不变。"""
    target = tmp_path / "sample.txt"
    original = "same\nsame\nthird\n"
    target.write_text(original, encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/sample.txt", "limit": 3})))

    cross_thread = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "third\n",
                    "new_string": "changed\n",
                },
                thread_id="thread-b",
            )
        )
    )
    assert cross_thread["error"]["code"] == "SNAPSHOT_SCOPE_MISMATCH"

    ambiguous = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "same\n",
                    "new_string": "different\n",
                },
            )
        )
    )
    assert ambiguous["error"]["code"] == "AMBIGUOUS_MATCH"

    legacy = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "third\n",
                    "new_string": "changed\n",
                    "replace_all": True,
                },
            )
        )
    )
    assert legacy["error"]["code"] == "FILE_TOOL_SCHEMA_INVALID"

    unknown = _payload(
        contract.dispatch(
            _request(
                "write_file",
                {"file_path": "/new.txt", "content": "new", "overwrite": True},
            )
        )
    )
    assert unknown["error"]["code"] == "FILE_TOOL_SCHEMA_INVALID"
    assert not (tmp_path / "new.txt").exists()

    target.write_text("external\n", encoding="utf-8")
    stale = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/sample.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "same\n",
                    "new_string": "different\n",
                },
            )
        )
    )
    assert stale["error"]["code"] == "STALE_FILE"
    assert target.read_text(encoding="utf-8") == "external\n"


def test_write_is_create_only_and_delete_requires_full_current_snapshot(tmp_path: Path) -> None:
    """write/edit/delete 职责不可互换，delete 不接受局部读取的证明。"""
    contract = _contract(tmp_path)
    created = _payload(
        contract.dispatch(_request("write_file", {"file_path": "/new.txt", "content": "one\ntwo\n"}))
    )
    assert created["ok"] is True
    exists = _payload(
        contract.dispatch(_request("write_file", {"file_path": "/new.txt", "content": "overwrite"}))
    )
    assert exists["error"]["code"] == "FILE_ALREADY_EXISTS"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "one\ntwo\n"

    (tmp_path / "partial.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    partial = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/partial.txt", "limit": 1}))
    )
    delete_partial = _payload(
        contract.dispatch(
            _request(
                "delete_file",
                {"file_path": "/partial.txt", "snapshot_id": partial["snapshot_id"]},
            )
        )
    )
    assert delete_partial["error"]["code"] == "UNREAD_RANGE"

    full = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/partial.txt", "limit": 3}))
    )
    deleted = _payload(
        contract.dispatch(
            _request("delete_file", {"file_path": "/partial.txt", "snapshot_id": full["snapshot_id"]})
        )
    )
    assert deleted["ok"] is True
    assert not (tmp_path / "partial.txt").exists()
    expired = _payload(
        contract.dispatch(
            _request(
                "delete_file",
                {"file_path": "/partial.txt", "snapshot_id": full["snapshot_id"]},
                call_id="delete-again",
            )
        )
    )
    assert expired["error"]["code"] == "SNAPSHOT_EXPIRED"

    (tmp_path / "stale-delete.txt").write_text("before\n", encoding="utf-8")
    stale_read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/stale-delete.txt", "limit": 1}))
    )
    (tmp_path / "stale-delete.txt").write_text("after\n", encoding="utf-8")
    stale_delete = _payload(
        contract.dispatch(
            _request(
                "delete_file",
                {"file_path": "/stale-delete.txt", "snapshot_id": stale_read["snapshot_id"]},
            )
        )
    )
    assert stale_delete["error"]["code"] == "STALE_FILE"
    assert (tmp_path / "stale-delete.txt").read_text(encoding="utf-8") == "after\n"


def test_large_write_returns_only_bounded_context_but_grants_visible_lines_to_new_snapshot(tmp_path: Path) -> None:
    """上下文因行数上限截断时，完整显示的前缀仍可直接用于连续 edit。"""
    contract = _contract(tmp_path)
    content = "".join(f"line-{index}\n" for index in range(250))
    written = _payload(
        contract.dispatch(_request("write_file", {"file_path": "/large.txt", "content": content}))
    )
    assert written["truncated"] is True
    assert written["shown_lines"] == {"start_line": 1, "end_line": 200}
    assert "line-200" not in written["content"]

    continued = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/large.txt",
                    "snapshot_id": written["snapshot_id"],
                    "old_string": "line-1\n",
                    "new_string": "changed\n",
                },
                call_id="edit-large-visible-line",
            )
        )
    )
    assert continued["ok"] is True
    assert (tmp_path / "large.txt").read_text(encoding="utf-8").startswith("line-0\nchanged\n")


def test_file_between_64_kib_and_mutation_limit_can_be_created_and_edited(tmp_path: Path) -> None:
    """64 KiB 只限制 exact-string 参数，不应误伤 2 MiB 内的完整文件。"""
    contract = _contract(tmp_path)
    content = "target\n" + ("padding line\n" * 6_000)
    assert len(content.encode("utf-8")) > 64 * 1024

    written = _payload(
        contract.dispatch(_request("write_file", {"file_path": "/medium.txt", "content": content}))
    )
    assert written["ok"] is True

    edited = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/medium.txt",
                    "snapshot_id": written["snapshot_id"],
                    "old_string": "target\n",
                    "new_string": "changed\n",
                },
            )
        )
    )
    assert edited["ok"] is True
    assert (tmp_path / "medium.txt").read_text(encoding="utf-8").startswith("changed\n")


def test_crlf_bom_round_trip_and_schema_have_no_range_or_replace_all(tmp_path: Path) -> None:
    """模型可复制普通换行文本，adapter 仍保留 BOM/CRLF 字节格式。"""
    target = tmp_path / "config.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/config.txt", "limit": 2})))
    edited = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/config.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "beta\n",
                    "new_string": "changed\n",
                },
            )
        )
    )
    assert edited["ok"] is True
    assert target.read_bytes() == b"\xef\xbb\xbfalpha\r\nchanged\r\n"

    schema = next(tool for tool in contract.tool_definitions if tool.name == "edit_file").args_schema.model_json_schema()
    assert set(schema["properties"]) == {"file_path", "snapshot_id", "old_string", "new_string"}


def test_truncated_read_never_grants_editable_seen_lines(tmp_path: Path) -> None:
    """字节截断文件不会变成可写的 prior-read 证明。"""
    long_line = "x" * (33 * 1024)
    (tmp_path / "long.txt").write_text(long_line, encoding="utf-8")
    contract = _contract(tmp_path)

    truncated = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/long.txt", "limit": 1}))
    )
    assert truncated["truncated"] is True
    assert truncated["shown_lines"] == {"start_line": None, "end_line": None}
    rejected = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/long.txt",
                    "snapshot_id": truncated["snapshot_id"],
                    "old_string": long_line,
                    "new_string": "changed",
                },
            )
        )
    )
    assert rejected["error"]["code"] == "UNREAD_RANGE"
    assert (tmp_path / "long.txt").read_text(encoding="utf-8") == long_line


def test_notebook_read_stays_read_only_and_does_not_issue_snapshot(tmp_path: Path) -> None:
    """Notebook 可以通过普通 read_file 查看，但不能取得文本 mutation Snapshot。"""
    from deepagents.backends.filesystem import FilesystemBackend
    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract

    (tmp_path / "note.ipynb").write_text('{"cells": []}\n', encoding="utf-8")
    contract = create_snapshot_file_tool_contract(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        snapshot_store=ThreadSnapshotStore(),
        text_backend=LocalTextMutationBackend(tmp_path),
    )

    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/note.ipynb"})))

    assert read["ok"] is True
    assert read["snapshot_id"] is None
    assert '{"cells": []}' in read["content"]


def test_binary_image_read_preserves_multimodal_content_without_snapshot(tmp_path: Path) -> None:
    """图片读取继续返回 multimodal block，不因 Snapshot 文本 contract 回归。"""
    from deepagents.backends.filesystem import FilesystemBackend
    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract

    (tmp_path / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    contract = create_snapshot_file_tool_contract(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        snapshot_store=ThreadSnapshotStore(),
        text_backend=LocalTextMutationBackend(tmp_path),
    )

    message = contract.dispatch(_request("read_file", {"file_path": "/pixel.png"}))

    assert message.status == "success"
    assert message.content_blocks[0]["type"] == "image"
    assert message.content_blocks[0]["mime_type"] == "image/png"


def test_approval_diff_is_the_prepared_content_and_conflict_invalidates_approval(tmp_path: Path) -> None:
    """HITL 前显示的 diff 与 CAS 计划相同，批准期间外部改动不能被旧批准覆盖。"""
    target = tmp_path / "approval.txt"
    target.write_text("before\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/approval.txt", "limit": 1})))
    edit_request = _request(
        "edit_file",
        {
            "file_path": "/approval.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "before\n",
            "new_string": "approved\n",
        },
    )

    assert contract.approval_preflight(edit_request) is True
    description = contract.approval_description(edit_request.tool_call, None, edit_request.runtime)
    assert "文件：/approval.txt" in description
    assert "-before" in description
    assert "+approved" in description
    assert "\n\n+approved" not in description
    details = contract.approval_details(edit_request.tool_call, None, edit_request.runtime)
    assert "-before" not in details.description
    assert details.presentation == {
        "kind": "file_diff",
        "operation": "edit",
        "path": "/approval.txt",
        "added_lines": 1,
        "removed_lines": 1,
        "truncated": False,
        "unified_diff": details.presentation["unified_diff"],
    }
    assert "-before" in details.presentation["unified_diff"]
    assert "+approved" in details.presentation["unified_diff"]

    target.write_text("external\n", encoding="utf-8")
    conflict = _payload(contract.dispatch(edit_request))
    assert conflict["error"]["code"] == "COMMIT_CONFLICT"
    assert target.read_text(encoding="utf-8") == "external\n"

    # 旧批准在冲突后不能因文件恰好恢复原样而被重新 prepare 或重放。
    target.write_text("before\n", encoding="utf-8")
    replay = _payload(contract.dispatch(edit_request))
    assert replay["error"]["code"] == "COMMIT_CONFLICT"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_approval_rejects_changed_arguments_for_the_same_tool_call(tmp_path: Path) -> None:
    """同一 Tool Call 的参数指纹变化必须使已审批计划失效，不得现场重建后提交。"""
    target = tmp_path / "approval-arguments.txt"
    target.write_text("before\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(
        contract.dispatch(_request("read_file", {"file_path": "/approval-arguments.txt"}))
    )
    approved = _request(
        "edit_file",
        {
            "file_path": "/approval-arguments.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "before\n",
            "new_string": "approved\n",
        },
        call_id="edit-approved-arguments",
    )
    changed = _request(
        "edit_file",
        {
            "file_path": "/approval-arguments.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "before\n",
            "new_string": "not-approved\n",
        },
        call_id="edit-approved-arguments",
    )

    assert contract.approval_preflight(approved) is True
    assert "+approved" in contract.approval_description(
        approved.tool_call,
        None,
        approved.runtime,
    )

    rejected = _payload(contract.dispatch(changed))
    assert rejected["error"]["code"] == "COMMIT_CONFLICT"
    assert target.read_text(encoding="utf-8") == "before\n"

    replay = _payload(contract.dispatch(approved))
    assert replay["error"]["code"] == "COMMIT_CONFLICT"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_invalid_snapshot_never_prepares_an_approval(tmp_path: Path) -> None:
    """Snapshot 已 stale 时不会展示可批准 diff，实际执行仍返回原有稳定错误。"""
    target = tmp_path / "stale.txt"
    target.write_text("before\n", encoding="utf-8")
    contract = _contract(tmp_path)
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/stale.txt", "limit": 1})))
    target.write_text("external\n", encoding="utf-8")
    request = _request(
        "edit_file",
        {
            "file_path": "/stale.txt",
            "snapshot_id": read["snapshot_id"],
            "old_string": "before\n",
            "new_string": "approved\n",
        },
    )

    assert contract.approval_preflight(request) is False
    result = _payload(contract.dispatch(request))
    assert result["error"]["code"] == "STALE_FILE"


def test_post_write_drift_returns_actual_diff_and_snapshot(tmp_path: Path) -> None:
    """保存钩子改写提交结果时，返回 warning，后续 Snapshot 只绑定实际内容。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend

    class DriftAfterWriteBackend(LocalTextMutationBackend):
        """模拟 editor 保存钩子在 replace 后立即改写文件。"""

        def compare_and_replace_text(self, path: str, expected: Any, proposed: str):
            result = super().compare_and_replace_text(path, expected, proposed)
            (tmp_path / path.lstrip("/")).write_text("formatted\n", encoding="utf-8")
            return result

    target = tmp_path / "drift.txt"
    target.write_text("before\n", encoding="utf-8")
    contract = _contract_with_backend(tmp_path, DriftAfterWriteBackend(tmp_path))
    read = _payload(contract.dispatch(_request("read_file", {"file_path": "/drift.txt", "limit": 1})))
    edited = _payload(
        contract.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/drift.txt",
                    "snapshot_id": read["snapshot_id"],
                    "old_string": "before\n",
                    "new_string": "approved\n",
                },
            )
        )
    )
    assert edited["ok"] is True
    assert edited["warning"]["code"] == "POST_WRITE_DRIFT"
    assert "-before" in edited["warning"]["proposed_diff"]
    assert "+approved" in edited["warning"]["proposed_diff"]
    assert "-approved" in edited["warning"]["actual_diff"]
    assert "+formatted" in edited["warning"]["actual_diff"]

    actual_read = _payload(contract.dispatch(_request("read_file", {"file_path": "/drift.txt", "limit": 1})))
    assert actual_read["snapshot_id"] == edited["snapshot_id"]
    assert actual_read["content"] == "1\tformatted"


def test_async_diagnostics_distinguish_zero_timeout_and_unavailable(tmp_path: Path) -> None:
    """diagnostics 只读且有界：零条、超时和缺失 LSP 不能混为同一结果。"""

    async def zero_diagnostics(_path: str) -> dict[str, object]:
        return {"results": {"kind": "full", "items": []}}

    target = tmp_path / "diagnostics.txt"
    target.write_text("before\n", encoding="utf-8")
    available = _contract(tmp_path, diagnostics_provider=zero_diagnostics)
    read = _payload(available.dispatch(_request("read_file", {"file_path": "/diagnostics.txt"})))
    zero = _payload(
        asyncio.run(
            available.adispatch(
                _request(
                    "edit_file",
                    {
                        "file_path": "/diagnostics.txt",
                        "snapshot_id": read["snapshot_id"],
                        "old_string": "before\n",
                        "new_string": "after\n",
                    },
                )
            )
        )
    )
    assert zero["ok"] is True
    assert zero["diagnostics"]["status"] == "ok"
    assert zero["diagnostics"]["count"] == 0

    async def slow_diagnostics(_path: str) -> dict[str, object]:
        await asyncio.sleep(0.02)
        return {"results": []}

    timeout_target = tmp_path / "timeout.txt"
    timeout_target.write_text("before\n", encoding="utf-8")
    timed = _contract(
        tmp_path,
        diagnostics_provider=slow_diagnostics,
        diagnostics_timeout_seconds=0.001,
    )
    timeout_read = _payload(timed.dispatch(_request("read_file", {"file_path": "/timeout.txt"})))
    timeout = _payload(
        asyncio.run(
            timed.adispatch(
                _request(
                    "edit_file",
                    {
                        "file_path": "/timeout.txt",
                        "snapshot_id": timeout_read["snapshot_id"],
                        "old_string": "before\n",
                        "new_string": "after\n",
                    },
                )
            )
        )
    )
    assert timeout["ok"] is True
    assert timeout["diagnostics"]["status"] == "timeout"
    assert timeout_target.read_text(encoding="utf-8") == "after\n"

    unavailable_target = tmp_path / "unavailable.txt"
    unavailable_target.write_text("before\n", encoding="utf-8")
    unavailable = _contract(tmp_path)
    unavailable_read = _payload(
        unavailable.dispatch(_request("read_file", {"file_path": "/unavailable.txt"}))
    )
    unavailable_result = _payload(
        unavailable.dispatch(
            _request(
                "edit_file",
                {
                    "file_path": "/unavailable.txt",
                    "snapshot_id": unavailable_read["snapshot_id"],
                    "old_string": "before\n",
                    "new_string": "after\n",
                },
            )
        )
    )
    assert unavailable_result["diagnostics"] == {"status": "unavailable"}


def test_parallel_adispatch_reads_keep_backend_io_parallel_and_snapshot_scopes_consistent(
    tmp_path: Path,
) -> None:
    """真实 to_thread 读取可重叠，Store 只在线性化元数据时短暂互斥。"""
    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract

    read_barrier = threading.Barrier(4)

    class OverlapBackend(LocalTextMutationBackend):
        """要求四次 backend read 同时到达，防止 Store 锁意外覆盖 I/O。"""

        def read_text_document(self, path: str):
            read_barrier.wait(timeout=2.0)
            return super().read_text_document(path)

    target = tmp_path / "parallel.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    store = ThreadSnapshotStore()
    backend = OverlapBackend(tmp_path)
    contract = create_snapshot_file_tool_contract(
        object(),
        snapshot_store=store,
        text_backend=backend,
    )

    async def read_in_parallel() -> list[dict[str, Any]]:
        requests = [
            _request(
                "read_file",
                {"file_path": "/parallel.txt", "offset": offset, "limit": 1},
                thread_id=thread_id,
                call_id=f"read-{thread_id}-{offset}",
            )
            for thread_id in ("thread-a", "thread-b")
            for offset in (0, 1)
        ]
        return [
            _payload(message)
            for message in await asyncio.gather(
                *(contract.adispatch(request) for request in requests)
            )
        ]

    results = asyncio.run(read_in_parallel())

    assert all(result["ok"] is True for result in results)
    thread_a_id = results[0]["snapshot_id"]
    thread_b_id = results[2]["snapshot_id"]
    assert results[1]["snapshot_id"] == thread_a_id
    assert results[3]["snapshot_id"] == thread_b_id
    assert thread_a_id != thread_b_id
    assert store.resolve(thread_a_id, "thread-a", "/parallel.txt", backend.backend_id).seen_lines == (
        (0, 2),
    )
    assert store.resolve(thread_b_id, "thread-b", "/parallel.txt", backend.backend_id).seen_lines == (
        (0, 2),
    )
    assert store.size == 2
    assert store.total_bytes == 2 * len("first\nsecond\n".encode())


def test_diagnostics_and_metrics_are_bounded_and_do_not_store_source_or_paths(
    tmp_path: Path, caplog: Any
) -> None:
    """LSP 原文不进入工具结果或 Host 指标，日志也只保留相对路径和聚合字段。"""
    from harness_agent.tools.file_tool_metrics import FileToolMetrics

    async def diagnostics(_path: str) -> dict[str, object]:
        return {
            "results": {
                "items": [
                    {
                        "range": {"start": {"line": index}, "end": {"line": index}},
                        "severity": 1,
                        "code": f"E{index}",
                        "message": "source-text-must-not-appear",
                    }
                    for index in range(25)
                ]
            }
        }

    target = tmp_path / "secret-name.txt"
    target.write_text("secret-source\n", encoding="utf-8")
    metrics = FileToolMetrics()
    contract = _contract(tmp_path, diagnostics_provider=diagnostics, metrics=metrics)
    caplog.set_level(logging.INFO, logger="harness_agent.tools.snapshot_file_contract")
    first = _payload(contract.dispatch(_request("read_file", {"file_path": "/secret-name.txt"})))
    second = _payload(contract.dispatch(_request("read_file", {"file_path": "/secret-name.txt"})))
    assert first["snapshot_id"] == second["snapshot_id"]
    edited = _payload(
        asyncio.run(
            contract.adispatch(
                _request(
                    "edit_file",
                    {
                        "file_path": "/secret-name.txt",
                        "snapshot_id": second["snapshot_id"],
                        "old_string": "secret-source\n",
                        "new_string": "changed\n",
                    },
                )
            )
        )
    )
    assert edited["diagnostics"]["count"] == 25
    assert len(edited["diagnostics"]["items"]) == 20
    assert edited["diagnostics"]["truncated"] is True
    assert "source-text-must-not-appear" not in json.dumps(edited, ensure_ascii=False)

    metrics_payload = metrics.snapshot().payload()
    assert metrics_payload["reread_calls"] == 1
    assert metrics_payload["edit_attempts"] == metrics_payload["edit_successes"] == 1
    assert metrics_payload["diagnostics"]["calls"] == 1
    metrics_text = json.dumps(metrics_payload, ensure_ascii=False)
    assert "secret-name" not in metrics_text
    assert "secret-source" not in metrics_text
    assert first["snapshot_id"] not in metrics_text
    assert "path=secret-name.txt" in caplog.text
    assert "secret-source" not in caplog.text
    assert first["snapshot_id"] not in caplog.text


def test_evicted_prepared_plan_is_marked_consumed_and_cannot_be_reprepared(tmp_path: Path) -> None:
    """内存预算淘汰只移除源码，不允许旧审批改为读取新版本后继续提交。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.file_mutation import FileMutationService, MutationMetadata

    service = FileMutationService(LocalTextMutationBackend(tmp_path), max_prepared_bytes=1)
    service.prepare(
        metadata=MutationMetadata(
            operation="write",
            path="/new.txt",
            thread_id="thread-a",
            tool_call_id="call-a",
        ),
        current=None,
        proposed_content="too-large-for-cache",
        fingerprint="fingerprint-a",
    )

    assert service.prepared(
        thread_id="thread-a",
        tool_call_id="call-a",
        fingerprint="fingerprint-a",
    ) is None
    assert service.was_consumed(
        thread_id="thread-a",
        tool_call_id="call-a",
        fingerprint="fingerprint-a",
    ) is True
