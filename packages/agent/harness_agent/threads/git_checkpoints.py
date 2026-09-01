"""Git 树隐式快照与工作区恢复服务。"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict


class DiffStats(TypedDict):
    files: list[str]
    insertions: int
    deletions: int


class GitCheckpointService:
    """基于底层 Git plumbing 命令提供树快照录制、差异统计和文件还原。"""

    def is_git_repository(self, workspace_path: Path | str) -> bool:
        """检查指定路径是否属于有效 Git 仓库。"""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(workspace_path),
                check=False,
                capture_output=True,
                text=True,
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    def create_tree_snapshot(self, workspace_path: Path | str) -> str:
        """创建当前工作区的隐式树快照，返回 40 字符 Tree OID。

        使用隔离的临时 GIT_INDEX_FILE，确保不修改用户原有的 .git/index 和 HEAD。
        """
        cwd = str(workspace_path)
        with self._temporary_index() as tmp_index_path:
            env = dict(os.environ, GIT_INDEX_FILE=tmp_index_path)
            subprocess.run(
                ["git", "read-tree", "HEAD"],
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
            )
            return subprocess.check_output(
                ["git", "write-tree"],
                cwd=cwd,
                env=env,
                text=True,
            ).strip()

    def compute_diff_stats(
        self,
        workspace_path: Path | str,
        base_tree_oid: str,
        target_tree_oid: str,
    ) -> DiffStats:
        """计算从 base_tree_oid 到 target_tree_oid 的文件变动统计。"""
        cwd = str(workspace_path)
        try:
            output = subprocess.check_output(
                ["git", "diff-tree", "--numstat", "-r", base_tree_oid, target_tree_oid],
                cwd=cwd,
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            return {"files": [], "insertions": 0, "deletions": 0}

        files: list[str] = []
        insertions = 0
        deletions = 0
        if not output:
            return {"files": [], "insertions": 0, "deletions": 0}

        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                ins, dels, filename = parts[0], parts[1], parts[2]
                files.append(filename)
                if ins.isdigit():
                    insertions += int(ins)
                if dels.isdigit():
                    deletions += int(dels)

        return {
            "files": files,
            "insertions": insertions,
            "deletions": deletions,
        }

    def restore_tree_snapshot(
        self,
        workspace_path: Path | str,
        target_tree_oid: str,
    ) -> int:
        """将工作区恢复至 target_tree_oid 状态，并返回受影响的文件数。

        检出使用隔离 index，避免改写用户 HEAD 或已暂存内容。
        """
        cwd = str(workspace_path)
        current_tree = self.create_tree_snapshot(workspace_path)
        diff = self.compute_diff_stats(workspace_path, current_tree, target_tree_oid)

        with self._temporary_index() as tmp_index_path:
            env = dict(os.environ, GIT_INDEX_FILE=tmp_index_path)
            subprocess.run(
                ["git", "read-tree", target_tree_oid],
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout-index", "-a", "-f"],
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
            )

        deleted_diff = self.compute_diff_stats(workspace_path, target_tree_oid, current_tree)
        for rel_path in deleted_diff["files"]:
            ls_check = subprocess.run(
                ["git", "ls-tree", target_tree_oid, "--", rel_path],
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            target_file = Path(cwd) / rel_path
            if not ls_check.stdout.strip() and target_file.exists() and target_file.is_file():
                try:
                    target_file.unlink()
                except OSError:
                    pass

        return len(diff["files"])

    @contextmanager
    def _temporary_index(self) -> Iterator[str]:
        """提供独立 GIT_INDEX_FILE，用完即删，避免污染用户 index。"""
        with tempfile.NamedTemporaryFile(delete=False) as tmp_index:
            tmp_index_path = tmp_index.name
        # NamedTemporaryFile 默认在磁盘创建 0 字节空文件。
        # 当仓库尚无 commit（如新建未提交仓库或 HEAD 为空）时，git read-tree HEAD 失败跳过后，
        # 遗留的 0 字节文件会导致 git add -A 报 exit status 128 (index file smaller than expected)。
        # 因此在交由 git 命令使用前先移除该 0 字节占位文件，让 git 自行初始化有效 index。
        if os.path.exists(tmp_index_path):
            try:
                os.remove(tmp_index_path)
            except OSError:
                pass
        try:
            yield tmp_index_path
        finally:
            if os.path.exists(tmp_index_path):
                try:
                    os.remove(tmp_index_path)
                except OSError:
                    pass
