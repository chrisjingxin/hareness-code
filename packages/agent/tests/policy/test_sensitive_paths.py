"""敏感路径保护模块的单元测试。"""

from __future__ import annotations

import pytest

from harness_agent.policy.sensitive_paths import is_sensitive_path, requires_safety_check


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "src/.git/HEAD",
        ".bashrc",
        "home/user/.zshrc",
        ".harness/settings.json",
    ],
)
def test_is_sensitive_path_detects_sensitive_targets(path: str):
    """敏感目录下的文件和敏感配置文件应被正确识别。"""
    assert is_sensitive_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "README.md",
    ],
)
def test_is_sensitive_path_allows_normal_files(path: str):
    """普通项目文件不应被误判为敏感路径。"""
    assert is_sensitive_path(path) is False


def test_requires_safety_check_blocks_write_to_sensitive_path():
    """写操作工具目标为敏感路径时应触发安全检查。"""
    assert requires_safety_check("write_file", {"file_path": ".git/config"}) is True


def test_requires_safety_check_allows_write_to_normal_path():
    """写操作工具目标为普通路径时不触发安全检查。"""
    assert requires_safety_check("write_file", {"file_path": "src/main.py"}) is False


def test_requires_safety_check_skips_read_only_tools():
    """只读工具即使目标为敏感路径也不触发安全检查。"""
    assert requires_safety_check("read_file", {"file_path": ".git/config"}) is False


def test_requires_safety_check_blocks_delete_sensitive_file():
    """删除操作目标为敏感文件时应触发安全检查。"""
    assert requires_safety_check("delete_file", {"file_path": ".bashrc"}) is True
