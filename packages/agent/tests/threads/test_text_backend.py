"""TextMutationBackend 的 local/remote/unsupported contract 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_local_adapter_round_trips_bom_crlf_and_uses_cas(tmp_path: Path) -> None:
    """本地 adapter 保留 BOM/CRLF，并在 stale identity 时拒绝提交。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend, TextMutationError

    target = tmp_path / "config.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")
    backend = LocalTextMutationBackend(tmp_path)
    original = backend.read_text_document("/config.txt")
    assert original.identity.has_bom is True
    assert original.identity.line_ending == "crlf"
    assert original.content == "alpha\r\nbeta\r\n"

    updated = backend.compare_and_replace_text(
        "/config.txt",
        original.identity,
        "alpha\nchanged\n",
    )
    assert updated.identity.has_bom is True
    assert updated.identity.line_ending == "crlf"
    assert target.read_bytes() == b"\xef\xbb\xbfalpha\r\nchanged\r\n"

    target.write_bytes(b"\xef\xbb\xbfexternal\r\n")
    with pytest.raises(TextMutationError) as conflict:
        backend.compare_and_replace_text("/config.txt", updated.identity, "blind")
    assert conflict.value.code == "COMMIT_CONFLICT"


def test_local_adapter_create_and_delete_are_compare_safe(tmp_path: Path) -> None:
    """create-if-absent 不覆盖，delete-if-unchanged 需要当前 identity。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend, TextMutationError

    backend = LocalTextMutationBackend(tmp_path)
    created = backend.create_text_document("/new.txt", "hello\n")
    with pytest.raises(TextMutationError) as exists:
        backend.create_text_document("/new.txt", "overwrite")
    assert exists.value.code == "FILE_ALREADY_EXISTS"
    backend.delete_if_unchanged("/new.txt", created.identity)
    assert not (tmp_path / "new.txt").exists()


def test_remote_adapter_without_native_cas_fails_closed() -> None:
    """只有 DeepAgents read/edit 形状的远端对象不能伪装成 CAS backend。"""
    from deepagents.backends.protocol import ReadResult

    from harness_agent.threads.text_backend import (
        ContentIdentity,
        RemoteTextMutationBackend,
        TextMutationError,
    )

    class ReadOnlyProvider:
        def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
            return ReadResult(file_data={"content": "hello\n", "encoding": "utf-8"})

    backend = RemoteTextMutationBackend(ReadOnlyProvider(), backend_id="remote-read-only")
    document = backend.read_text_document("/a.txt")
    assert document.content == "hello\n"
    with pytest.raises(TextMutationError) as unsupported:
        backend.compare_and_replace_text("/a.txt", document.identity, "changed")
    assert unsupported.value.code == "BACKEND_CAS_UNSUPPORTED"


def test_unsupported_adapter_never_blind_writes() -> None:
    """unsupported fake 的所有 mutation 都返回稳定 CAS 能力错误。"""
    from harness_agent.threads.text_backend import (
        ContentIdentity,
        TextMutationError,
        UnsupportedTextMutationBackend,
    )

    backend = UnsupportedTextMutationBackend()
    identity = ContentIdentity(digest="x", byte_length=1)
    with pytest.raises(TextMutationError) as create_error:
        backend.create_text_document("/a.txt", "x")
    with pytest.raises(TextMutationError) as replace_error:
        backend.compare_and_replace_text("/a.txt", identity, "y")
    with pytest.raises(TextMutationError) as delete_error:
        backend.delete_if_unchanged("/a.txt", identity)
    assert {create_error.value.code, replace_error.value.code, delete_error.value.code} == {
        "BACKEND_CAS_UNSUPPORTED"
    }
