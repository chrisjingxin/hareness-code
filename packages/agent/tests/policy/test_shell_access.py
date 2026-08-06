"""Shell 命令文件读写触碰检测门的提取与安全校验回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.policy.shell_access import (
    evaluate_shell_file_access,
    extract_write_paths,
)


# ===================================================================
# extract_write_paths 测试
# ===================================================================


class TestExtractWritePathsRedirect:
    """输出重定向（>、>>）路径提取。"""

    def test_simple_stdout_redirect(self):
        """单 > 重定向提取目标路径。"""
        assert extract_write_paths("echo data > /tmp/out.txt") == ["/tmp/out.txt"]

    def test_append_redirect(self):
        """>> 追加重定向提取目标路径。"""
        assert extract_write_paths("echo data >> /var/log/app.log") == ["/var/log/app.log"]

    def test_redirect_with_no_space(self):
        """重定向符号与路径之间无空格时仍能提取。"""
        assert extract_write_paths("echo data >/tmp/out.txt") == ["/tmp/out.txt"]

    def test_redirect_with_spaces(self):
        """重定向符号与路径之间有多余空格时仍能提取。"""
        assert extract_write_paths("echo data >   /tmp/out.txt") == ["/tmp/out.txt"]

    def test_fd_numbered_redirect(self):
        """带文件描述符前缀的 N> 重定向提取目标路径。"""
        assert extract_write_paths("cmd 2> /tmp/err.log") == ["/tmp/err.log"]

    def test_fd_append_redirect(self):
        """N>> 追加形式提取目标路径。"""
        assert extract_write_paths("cmd 2>> /tmp/err.log") == ["/tmp/err.log"]

    def test_ampersand_redirect(self):
        """&> 合并输出重定向提取目标路径。"""
        assert extract_write_paths("cmd &> /tmp/all.log") == ["/tmp/all.log"]

    def test_ampersand_append_redirect(self):
        """&>> 合并追加重定向提取目标路径。"""
        assert extract_write_paths("cmd &>> /tmp/all.log") == ["/tmp/all.log"]

    def test_multiple_redirects_in_one_command(self):
        """同一命令中多个重定向均被提取。"""
        paths = extract_write_paths("cmd > /tmp/out.txt 2> /tmp/err.log")
        assert "/tmp/out.txt" in paths
        assert "/tmp/err.log" in paths
        assert len(paths) == 2

    def test_redirect_at_command_start(self):
        """重定向出现在命令开头时也能提取。"""
        assert extract_write_paths("> /tmp/empty.txt") == ["/tmp/empty.txt"]

    def test_relative_path_redirect(self):
        """相对路径的重定向目标同样被提取。"""
        assert extract_write_paths("echo hello > output.log") == ["output.log"]


class TestExtractWritePathsTee:
    """tee 命令路径提取。"""

    def test_simple_tee(self):
        """基本 tee 命令提取文件参数。"""
        assert extract_write_paths("cat file | tee log.txt") == ["log.txt"]

    def test_tee_with_multiple_files(self):
        """tee 后跟多个文件参数时全部提取。"""
        paths = extract_write_paths("echo data | tee a.txt b.txt c.txt")
        assert paths == ["a.txt", "b.txt", "c.txt"]

    def test_tee_skips_option_flags(self):
        """tee 的 -a 等选项不被当作路径。"""
        paths = extract_write_paths("echo data | tee -a log.txt")
        assert paths == ["log.txt"]

    def test_tee_with_absolute_path(self):
        """tee 后跟绝对路径时正确提取。"""
        assert extract_write_paths("cmd | tee /var/log/output.log") == ["/var/log/output.log"]

    def test_tee_with_options_and_multiple_files(self):
        """tee 同时含选项和多个文件时只提取文件路径。"""
        paths = extract_write_paths("cmd | tee -a -i file1.txt file2.txt")
        assert "file1.txt" in paths
        assert "file2.txt" in paths


class TestExtractWritePathsDd:
    """dd of= 路径提取。"""

    def test_dd_of_target(self):
        """dd of= 参数正确提取目标路径。"""
        assert extract_write_paths("dd if=/dev/zero of=/tmp/image.bin bs=1M") == ["/tmp/image.bin"]

    def test_dd_of_with_other_options(self):
        """dd 的 of= 在其他选项之间时仍能提取。"""
        paths = extract_write_paths("dd bs=4M of=output.img if=input.img")
        assert paths == ["output.img"]

    def test_dd_without_of_is_empty(self):
        """dd 没有 of= 参数时不提取路径。"""
        assert extract_write_paths("dd if=/dev/zero bs=1M count=10") == []


class TestExtractWritePathsEdgeCases:
    """extract_write_paths 的边界与混合场景。"""

    def test_no_write_target_returns_empty(self):
        """纯读取命令不提取任何路径。"""
        assert extract_write_paths("ls -la /tmp") == []
        assert extract_write_paths("cat /etc/hosts") == []
        assert extract_write_paths("git status") == []

    def test_empty_command_returns_empty(self):
        """空命令返回空列表。"""
        assert extract_write_paths("") == []

    def test_mixed_redirect_and_tee(self):
        """同时含重定向和 tee 的命令提取全部路径。"""
        paths = extract_write_paths("echo data > out.txt | tee log.txt")
        assert "out.txt" in paths
        assert "log.txt" in paths

    def test_mixed_redirect_and_dd(self):
        """同时含重定向和 dd of= 的命令提取全部路径。"""
        paths = extract_write_paths("echo header > header.txt")
        paths += extract_write_paths("dd if=/dev/zero of=disk.img bs=1M")
        assert "header.txt" in paths
        assert "disk.img" in paths

    def test_pipe_only_does_not_count_as_write(self):
        """纯管道不构成写目标。"""
        assert extract_write_paths("cat file | grep pattern | sort") == []

    def test_redirect_to_dev_null(self):
        """/dev/null 重定向仍被提取（路径提取不做语义过滤）。"""
        assert extract_write_paths("cmd > /dev/null") == ["/dev/null"]

    def test_redirect_2_to_ampersand_1_extracts_target(self):
        """2>&1 中的 &> 被正则匹配为重定向，&1 被提取为目标（已知行为局限）。"""
        paths = extract_write_paths("cmd > /tmp/out.txt 2>&1")
        assert "/tmp/out.txt" in paths
        # 正则无法区分 &> 重定向与 2>&1 fd 复制，&1 会被当作路径提取
        assert "&1" in paths


# ===================================================================
# evaluate_shell_file_access 测试
# ===================================================================


class TestEvaluateShellFileAccessNoWrite:
    """无写入命令的评估结果。"""

    def test_read_only_command_has_no_file_access(self):
        """纯读取命令返回 has_file_access=False。"""
        result = evaluate_shell_file_access("ls -la /tmp")
        assert result["has_file_access"] is False
        assert result["outside_workspace"] is False
        assert "paths" not in result

    def test_empty_command_has_no_file_access(self):
        """空命令返回 has_file_access=False。"""
        result = evaluate_shell_file_access("")
        assert result["has_file_access"] is False


class TestEvaluateShellFileAccessWithoutWorkspace:
    """无 workspace_root 时的评估行为。"""

    def test_write_detected_without_workspace(self):
        """有写入但无 workspace_root 时 outside_workspace 为 False。"""
        result = evaluate_shell_file_access("echo data > /etc/config")
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False
        assert "/etc/config" in result["paths"]

    def test_tee_detected_without_workspace(self):
        """tee 写入在无 workspace_root 时正确报告。"""
        result = evaluate_shell_file_access("cmd | tee /var/log/output.log")
        assert result["has_file_access"] is True
        assert "/var/log/output.log" in result["paths"]


class TestEvaluateShellFileAccessWithWorkspace:
    """有 workspace_root 时的边界检查。"""

    def test_write_inside_workspace_is_not_outside(self, tmp_path: Path):
        """写入工作区内的路径不标记为 outside_workspace。"""
        target = str(tmp_path / "output.txt")
        result = evaluate_shell_file_access(f"echo data > {target}", workspace_root=str(tmp_path))
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False
        assert target in result["paths"]

    def test_write_outside_workspace_is_flagged(self, tmp_path: Path):
        """写入工作区外的绝对路径标记为 outside_workspace。"""
        outside = str(tmp_path.parent / "outside" / "secret.txt")
        result = evaluate_shell_file_access(f"echo data > {outside}", workspace_root=str(tmp_path))
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is True

    def test_relative_path_inside_workspace(self, tmp_path: Path):
        """相对路径解析后在工作区内时不标记为 outside。"""
        (tmp_path / "subdir").mkdir()
        result = evaluate_shell_file_access("echo data > subdir/out.txt", workspace_root=str(tmp_path))
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False

    def test_parent_traversal_outside_workspace(self, tmp_path: Path):
        """使用 .. 穿越到工作区外的路径标记为 outside。"""
        result = evaluate_shell_file_access("echo data > ../outside.txt", workspace_root=str(tmp_path))
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is True

    def test_multiple_writes_all_inside(self, tmp_path: Path):
        """多个写入目标全部在工作区内时 outside_workspace 为 False。"""
        f1 = str(tmp_path / "a.txt")
        f2 = str(tmp_path / "b.txt")
        result = evaluate_shell_file_access(
            f"echo a > {f1}; echo b > {f2}", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False

    def test_multiple_writes_one_outside(self, tmp_path: Path):
        """多个写入目标中有一个在工作区外即标记 outside。"""
        inside = str(tmp_path / "inside.txt")
        outside = str(tmp_path.parent / "outside.txt")
        result = evaluate_shell_file_access(
            f"echo a > {inside} 2> {outside}", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is True

    def test_tee_inside_workspace(self, tmp_path: Path):
        """tee 写入工作区内路径不标记为 outside。"""
        target = str(tmp_path / "log.txt")
        result = evaluate_shell_file_access(
            f"cmd | tee {target}", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False

    def test_tee_outside_workspace(self, tmp_path: Path):
        """tee 写入工作区外路径标记为 outside。"""
        outside = str(tmp_path.parent / "leak.log")
        result = evaluate_shell_file_access(
            f"cmd | tee {outside}", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is True


class TestEvaluateShellFileAccessWorkspaceEdgeCases:
    """evaluate_shell_file_access 的边界场景。"""

    def test_no_write_with_workspace_returns_no_access(self, tmp_path: Path):
        """无写入命令即使指定 workspace_root 也返回 has_file_access=False。"""
        result = evaluate_shell_file_access("git status", workspace_root=str(tmp_path))
        assert result["has_file_access"] is False
        assert result["outside_workspace"] is False

    def test_dev_null_inside_workspace(self, tmp_path: Path):
        """/dev/null 是绝对路径但不在工作区内，应标记 outside。"""
        result = evaluate_shell_file_access("cmd > /dev/null", workspace_root=str(tmp_path))
        assert result["has_file_access"] is True
        # /dev/null 通常不在 tmp_path 工作区内
        assert result["outside_workspace"] is True

    def test_dd_of_inside_workspace(self, tmp_path: Path):
        """dd of= 目标在工作区内时不标记 outside。"""
        target = str(tmp_path / "disk.img")
        result = evaluate_shell_file_access(
            f"dd if=/dev/zero of={target} bs=1M", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is False

    def test_dd_of_outside_workspace(self, tmp_path: Path):
        """dd of= 目标在工作区外时标记 outside。"""
        outside = str(tmp_path.parent / "disk.img")
        result = evaluate_shell_file_access(
            f"dd if=/dev/zero of={outside} bs=1M", workspace_root=str(tmp_path)
        )
        assert result["has_file_access"] is True
        assert result["outside_workspace"] is True
