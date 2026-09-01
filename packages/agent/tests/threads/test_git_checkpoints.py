"""GitCheckpointService 单测：测试隐式 Git 树快照录制、Diff 计算与工作区安全还原。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness_agent.threads.git_checkpoints import GitCheckpointService


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """初始化一个干净的临时 Git 仓库用于测试。"""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)

    (repo / "hello.txt").write_text("hello world\n", encoding="utf-8")
    (repo / "main.py").write_text("print('init')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True)


def test_is_git_repository(temp_git_repo: Path, tmp_path: Path) -> None:
    """测试 Git 仓库检测能力。"""
    service = GitCheckpointService()
    assert service.is_git_repository(temp_git_repo) is True

    non_git = tmp_path / "non_git_dir"
    non_git.mkdir()
    assert service.is_git_repository(non_git) is False


def test_create_tree_snapshot_does_not_pollute_git_head(temp_git_repo: Path) -> None:
    """测试创建树快照：返回 40 字符 Tree OID，且不产生 commit、不改变 HEAD 与 index。"""
    service = GitCheckpointService()
    initial_head = _git_output(temp_git_repo, "rev-parse", "HEAD").strip()
    index_before = _git_output(temp_git_repo, "ls-files", "-s")

    (temp_git_repo / "main.py").write_text("print('modified turn 1')\n", encoding="utf-8")

    tree_oid = service.create_tree_snapshot(temp_git_repo)
    assert isinstance(tree_oid, str)
    assert len(tree_oid) == 40

    assert _git_output(temp_git_repo, "rev-parse", "HEAD").strip() == initial_head
    assert _git_output(temp_git_repo, "ls-files", "-s") == index_before


def test_diff_stats_and_restore(temp_git_repo: Path) -> None:
    """测试两轮快照之间的 diff_stats 计算与无损还原。"""
    service = GitCheckpointService()

    tree_1 = service.create_tree_snapshot(temp_git_repo)

    (temp_git_repo / "main.py").write_text("print('modified turn 2')\nline2\n", encoding="utf-8")
    (temp_git_repo / "test2.txt").write_text("new file content\n", encoding="utf-8")

    tree_2 = service.create_tree_snapshot(temp_git_repo)
    assert tree_1 != tree_2

    diff = service.compute_diff_stats(temp_git_repo, base_tree_oid=tree_1, target_tree_oid=tree_2)
    assert set(diff["files"]) == {"main.py", "test2.txt"}
    assert diff["insertions"] > 0

    restored_count = service.restore_tree_snapshot(temp_git_repo, tree_1)
    assert restored_count >= 1

    assert (temp_git_repo / "main.py").read_text(encoding="utf-8") == "print('init')\n"
    assert not (temp_git_repo / "test2.txt").exists()


def test_restore_does_not_change_head_or_index(temp_git_repo: Path) -> None:
    """还原只改工作区文件，不移动 HEAD，也不改写用户已暂存的 index。"""
    service = GitCheckpointService()
    (temp_git_repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=temp_git_repo, check=True, capture_output=True)

    head_before = _git_output(temp_git_repo, "rev-parse", "HEAD").strip()
    index_before = _git_output(temp_git_repo, "ls-files", "-s")

    (temp_git_repo / "hello.txt").write_text("changed in snapshot\n", encoding="utf-8")
    tree = service.create_tree_snapshot(temp_git_repo)
    (temp_git_repo / "extra.txt").write_text("extra\n", encoding="utf-8")

    service.restore_tree_snapshot(temp_git_repo, tree)

    assert _git_output(temp_git_repo, "rev-parse", "HEAD").strip() == head_before
    assert _git_output(temp_git_repo, "ls-files", "-s") == index_before
    assert (temp_git_repo / "hello.txt").read_text(encoding="utf-8") == "changed in snapshot\n"
    assert (temp_git_repo / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    assert not (temp_git_repo / "extra.txt").exists()


def test_create_tree_snapshot_empty_repo_without_commits(tmp_path: Path) -> None:
    """测试尚无初始 commit 的全新 Git 仓库（HEAD 为空）依然能正确创建快照。"""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

    service = GitCheckpointService()
    assert service.is_git_repository(repo) is True

    # 空仓库无文件时创建快照
    empty_tree = service.create_tree_snapshot(repo)
    assert isinstance(empty_tree, str)
    assert len(empty_tree) == 40

    # 写入新文件后创建快照
    (repo / "hello.txt").write_text("123\n", encoding="utf-8")
    tree_1 = service.create_tree_snapshot(repo)
    assert tree_1 != empty_tree

    diff = service.compute_diff_stats(repo, empty_tree, tree_1)
    assert diff["files"] == ["hello.txt"]
    assert diff["insertions"] == 1

    # 还原到空状态
    service.restore_tree_snapshot(repo, empty_tree)
    assert not (repo / "hello.txt").exists()
