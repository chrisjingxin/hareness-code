"""WorkspaceRootRegistry 路径判定与信任注册测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_agent.policy.workspace_roots import (
    DirectoryNotTrustable,
    ExternalPathNotTrusted,
    WorkspaceRootRegistry,
    normalize_host_path,
)


def test_primary_virtual_paths_resolve_unchanged(tmp_path: Path):
    """主根虚拟路径解析结果与改造前一致。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("ok", encoding="utf-8")

    resolved = registry.resolve("/src/main.py")
    assert resolved.root.root_id == "primary"
    assert resolved.backend_path == "/src/main.py"
    assert resolved.display_path == "/src/main.py"


def test_relative_and_traversal_rejected(tmp_path: Path):
    """相对路径与 .. 穿越继续硬拒绝。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    with pytest.raises(ValueError):
        registry.resolve("relative.md")
    with pytest.raises(ValueError, match=r"\.\."):
        registry.resolve("/a/../b")


def test_unc_rejected(tmp_path: Path):
    """UNC 路径硬拒绝。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    with pytest.raises(ValueError, match="UNC"):
        registry.resolve("//server/share/file")


def test_windows_drive_path_triggers_trust_candidate(tmp_path: Path):
    """Windows 盘符路径在未信任时抛出 ExternalPathNotTrusted。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    outside = tmp_path.parent / f"zc142-outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "app.toml"
    target.write_text("x", encoding="utf-8")
    try:
        with pytest.raises(ExternalPathNotTrusted) as exc_info:
            registry.resolve(str(target))
        assert exc_info.value.candidate.directory == normalize_host_path(outside)
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()


def test_trust_session_then_resolve(tmp_path: Path):
    """会话级信任后，外部路径映射为 /@ext/<id>/...。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    outside = tmp_path.parent / f"zc142-session-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "app.toml"
    target.write_text("hello", encoding="utf-8")
    try:
        root = registry.trust(outside, "session")
        assert root.scope == "session"
        resolved = registry.resolve(str(target))
        assert resolved.root.root_id == root.root_id
        assert resolved.backend_path.startswith("/@ext/")
        assert "app.toml" in resolved.backend_path
        assert resolved.display_path == str(normalize_host_path(target))
        assert registry.to_display(resolved.backend_path) == resolved.display_path
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()


def test_trust_once_consumable(tmp_path: Path):
    """once 授权仅对指定 run_id 生效，清理后失效。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    outside = tmp_path.parent / f"zc142-once-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "a.txt"
    target.write_text("a", encoding="utf-8")
    try:
        registry.trust(outside, "once", run_id="run-1")
        resolved = registry.resolve(str(target), run_id="run-1")
        assert resolved.root.scope == "once"
        with pytest.raises(ExternalPathNotTrusted):
            registry.resolve(str(target), run_id="run-2")
        registry.clear_once_for_run("run-1")
        with pytest.raises(ExternalPathNotTrusted):
            registry.resolve(str(target), run_id="run-1")
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()


def test_trust_project_persists(tmp_path: Path):
    """project 作用域写入 additional_directories，重启后仍可用。"""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "lib.py").write_text("x", encoding="utf-8")

    registry = WorkspaceRootRegistry(project, project_dir=project, load_persisted=False)
    registry.trust(outside, "project")
    settings = json.loads((project / ".harness" / "settings.json").read_text(encoding="utf-8"))
    assert any(normalize_host_path(item) == normalize_host_path(outside) for item in settings["additional_directories"])

    reloaded = WorkspaceRootRegistry(project, project_dir=project, load_persisted=True)
    resolved = reloaded.resolve(str(outside / "lib.py"))
    assert resolved.root.scope in {"project", "session", "user", "system"}
    assert resolved.backend_path.startswith("/@ext/")


def test_reject_filesystem_root_and_home(tmp_path: Path):
    """文件系统根与 home 目录本身不可注册。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    with pytest.raises(DirectoryNotTrustable):
        registry.trust(Path(tmp_path.anchor), "session")
    with pytest.raises(DirectoryNotTrustable):
        registry.trust(Path.home(), "session")


def test_reject_path_inside_primary(tmp_path: Path):
    """主工作区内路径无需也不允许再信任。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    inner = tmp_path / "sub"
    inner.mkdir()
    with pytest.raises(DirectoryNotTrustable, match="主工作区"):
        registry.trust(inner, "session")


def test_nested_root_absorbed(tmp_path: Path):
    """新根包含已有根时吸收，避免根集合膨胀。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    parent = tmp_path.parent / f"zc142-parent-{tmp_path.name}"
    child = parent / "child"
    child.mkdir(parents=True)
    try:
        registry.trust(child, "session")
        assert len(registry.roots()) == 2
        registry.trust(parent, "session")
        extras = [root for root in registry.roots() if root.root_id != "primary"]
        assert len(extras) == 1
        assert extras[0].path == normalize_host_path(parent)
    finally:
        child.rmdir()
        parent.rmdir()


def test_readonly_view_cannot_trust(tmp_path: Path):
    """子 Agent 只读视图不能注册额外根。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    outside = tmp_path.parent / f"zc142-ro-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    try:
        view = registry.readonly_view()
        with pytest.raises(DirectoryNotTrustable, match="只读"):
            view.trust(outside, "session")
    finally:
        outside.rmdir()


def test_trust_candidate_for_file(tmp_path: Path):
    """待信任目录取文件所在目录。"""
    registry = WorkspaceRootRegistry(tmp_path, load_persisted=False)
    outside = tmp_path.parent / f"zc142-cand-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "cfg.toml"
    target.write_text("a", encoding="utf-8")
    try:
        candidate = registry.trust_candidate(str(target))
        assert candidate.directory == normalize_host_path(outside)
        assert candidate.reason is None
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()
