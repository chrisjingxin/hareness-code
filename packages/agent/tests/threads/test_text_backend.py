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


def test_local_adapter_preserves_existing_file_permissions(tmp_path: Path) -> None:
    """原子替换不得把可执行脚本的 mode 降为 mkstemp 默认的 0600。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend

    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho before\n", encoding="utf-8")
    target.chmod(0o755)
    backend = LocalTextMutationBackend(tmp_path)
    current = backend.read_text_document("/script.sh")

    backend.compare_and_replace_text(
        "/script.sh",
        current.identity,
        "#!/bin/sh\necho after\n",
    )

    assert target.stat().st_mode & 0o777 == 0o755


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
    """分页 read 不能伪装成完整文本 identity，也不能据此开放 CAS mutation。"""
    from deepagents.backends.protocol import ReadResult

    from harness_agent.threads.text_backend import (
        RemoteTextMutationBackend,
        TextMutationError,
    )

    class ReadOnlyProvider:
        def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
            return ReadResult(file_data={"content": "hello\n", "encoding": "utf-8"})

    backend = RemoteTextMutationBackend(ReadOnlyProvider(), backend_id="remote-read-only")
    with pytest.raises(TextMutationError) as unsupported_read:
        backend.read_text_document("/a.txt")
    assert unsupported_read.value.code == "BACKEND_TEXT_UNSUPPORTED"


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


def test_local_adapter_replace_failure_keeps_original_bytes_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同目录 replace 失败时原文件不能被截断，临时文件也必须清理。"""
    import harness_agent.threads.text_backend as text_backend
    from harness_agent.threads.text_backend import LocalTextMutationBackend, TextMutationError

    target = tmp_path / "keep.txt"
    original = b"before\r\n"
    target.write_bytes(original)
    backend = LocalTextMutationBackend(tmp_path)
    current = backend.read_text_document("/keep.txt")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(text_backend.os, "replace", fail_replace)
    with pytest.raises(TextMutationError) as failure:
        backend.compare_and_replace_text("/keep.txt", current.identity, "after\r\n")

    assert failure.value.code == "BACKEND_REPLACE_FAILED"
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".harness-text-*"))


def test_local_adapter_rejects_symlink_replacement_without_touching_link_target(tmp_path: Path) -> None:
    """外部将目标替换为符号链接后，adapter 不跟随链接或覆盖其指向内容。"""
    from harness_agent.threads.text_backend import LocalTextMutationBackend, TextMutationError

    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    backend = LocalTextMutationBackend(tmp_path)
    current = backend.read_text_document("/target.txt")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(TextMutationError) as failure:
        backend.compare_and_replace_text("/target.txt", current.identity, "approved\n")

    assert failure.value.code == "PATH_SYMLINK_UNSUPPORTED"
    assert outside.read_text(encoding="utf-8") == "outside\n"
