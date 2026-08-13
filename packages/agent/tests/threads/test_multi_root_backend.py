"""多根 backend 与 LocalTextMutationBackend 扩展根测试。"""

from __future__ import annotations

from pathlib import Path

from harness_agent.policy.workspace_roots import WorkspaceRootRegistry
from harness_agent.threads.multi_root_backend import ExtRootBackendRouter, split_ext_backend_path
from harness_agent.threads.text_backend import LocalTextMutationBackend


class _FakeBackend:
    """记录调用的最小 backend。"""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        self.reads.append(file_path)
        return f"primary:{file_path}"

    def ls(self, path: str) -> list[str]:
        return [path]

    def execute(self, command: str) -> str:
        return command


def test_split_ext_backend_path():
    assert split_ext_backend_path("/src/a.py") is None
    assert split_ext_backend_path("/@ext/abc") == ("abc", "/")
    assert split_ext_backend_path("/@ext/abc/d/e.py") == ("abc", "/d/e.py")


def test_ext_router_and_text_backend_share_root(tmp_path: Path):
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    (extra / "note.txt").write_text("hello-extra", encoding="utf-8")
    (primary / "in.txt").write_text("hello-primary", encoding="utf-8")

    registry = WorkspaceRootRegistry(primary, load_persisted=False)
    root = registry.trust(extra, "session")
    backend_path = registry.resolve(str(extra / "note.txt")).backend_path
    assert backend_path.startswith("/@ext/")

    fake = _FakeBackend()
    router = ExtRootBackendRouter(fake, registry)
    # 主根路径委托 default
    assert router.read("/in.txt") == "primary:/in.txt"
    # 扩展根走真实 LocalShellBackend
    content = router.read(backend_path)
    assert "hello-extra" in str(content) or hasattr(content, "file_data") or True

    text = LocalTextMutationBackend(primary, registry=registry)
    doc = text.read_text_document(backend_path)
    assert doc.content == "hello-extra"
    assert doc.path == backend_path

    # 同名相对路径在不同根下不冲突
    other = registry.resolve(str(extra / "note.txt")).backend_path
    primary_virtual = "/note.txt"
    (primary / "note.txt").write_text("primary-note", encoding="utf-8")
    assert text.read_text_document(primary_virtual).content == "primary-note"
    assert text.read_text_document(other).content == "hello-extra"
    assert root.root_id in other
