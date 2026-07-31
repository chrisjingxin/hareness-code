"""文件操作工具 delete_file 和 apply_patch 的单元测试。"""

from __future__ import annotations

from pathlib import Path

from harness_agent.tools.tools_file import apply_patch, delete_file


def test_delete_file_success(tmp_path: Path):
    """创建临时文件后删除成功。"""
    target = tmp_path / "hello.txt"
    target.write_text("hello", encoding="utf-8")

    result = delete_file("/hello.txt", str(tmp_path))

    assert result == {"success": True, "deleted": "/hello.txt"}
    assert not target.exists()


def test_delete_file_not_found(tmp_path: Path):
    """文件不存在返回错误。"""
    result = delete_file("/nonexistent.txt", str(tmp_path))

    assert result["success"] is False
    assert "不存在" in result["error"]


def test_delete_file_path_traversal(tmp_path: Path):
    """路径穿越被阻止。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    try:
        result = delete_file("/../secret.txt", str(tmp_path))
        assert result["success"] is False
        assert "路径穿越" in result["error"]
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_delete_file_rejects_directory(tmp_path: Path):
    """目录不允许删除。"""
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    result = delete_file("/subdir", str(tmp_path))

    assert result["success"] is False
    assert "目录" in result["error"]
    assert subdir.exists()


def test_apply_patch_modify_file(tmp_path: Path):
    """修改现有文件的补丁。"""
    target = tmp_path / "main.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    patch = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2_modified\n"
        " line3\n"
    )

    result = apply_patch(patch, str(tmp_path))

    assert result == {"success": True, "files_modified": ["main.py"]}
    assert target.read_text(encoding="utf-8") == "line1\nline2_modified\nline3\n"


def test_apply_patch_create_file(tmp_path: Path):
    """创建新文件的补丁。"""
    patch = (
        "--- /dev/null\n"
        "+++ b/new_file.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
    )

    result = apply_patch(patch, str(tmp_path))

    assert result == {"success": True, "files_modified": ["new_file.txt"]}
    created = tmp_path / "new_file.txt"
    assert created.exists()
    assert created.read_text(encoding="utf-8") == "hello\nworld\n"


def test_apply_patch_invalid_format(tmp_path: Path):
    """无效补丁格式返回错误。"""
    result = apply_patch("this is not a valid patch", str(tmp_path))

    assert result["success"] is False
    assert "无法解析" in result["error"]


def test_apply_patch_path_traversal(tmp_path: Path):
    """补丁中路径穿越被阻止。"""
    patch = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root\n"
        "+hacked\n"
    )

    result = apply_patch(patch, str(tmp_path))

    assert result["success"] is False
    assert "路径穿越" in result["error"]
