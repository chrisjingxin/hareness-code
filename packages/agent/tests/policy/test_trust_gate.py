"""受信目录门禁模块的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.policy import trust_gate as tg


@pytest.fixture()
def _redirect_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """将 settings.json 路径重定向到临时目录，隔离真实用户配置。"""
    fake_path = tmp_path / ".harness" / "settings.json"
    monkeypatch.setattr(tg, "get_trusted_directories_path", lambda: fake_path)


# ---------------------------------------------------------------------------
# 持久化读写：load / save
# ---------------------------------------------------------------------------


class TestLoadSaveTrustedDirectories:
    """受信目录列表的加载与持久化。"""

    def test_load_returns_empty_when_file_missing(self, _redirect_settings):
        """settings.json 不存在时返回空列表。"""
        assert tg.load_trusted_directories() == []

    def test_load_returns_empty_when_file_is_empty_json(self, _redirect_settings, tmp_path: Path):
        """文件存在但为空对象时返回空列表。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{}", encoding="utf-8")
        assert tg.load_trusted_directories() == []

    def test_load_returns_empty_when_field_is_not_list(self, _redirect_settings, tmp_path: Path):
        """trusted_directories 字段非列表类型时返回空列表。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"trusted_directories": "not-a-list"}), encoding="utf-8"
        )
        assert tg.load_trusted_directories() == []

    def test_load_filters_non_string_items(self, _redirect_settings, tmp_path: Path):
        """列表中非字符串元素应被过滤掉。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"trusted_directories": ["/valid/path", 42, None]}),
            encoding="utf-8",
        )
        result = tg.load_trusted_directories()
        assert result == ["/valid/path"]

    def test_save_creates_parent_directory(self, _redirect_settings, tmp_path: Path):
        """save 应自动创建 .harness 父目录。"""
        tg.save_trusted_directories(["/some/dir"])
        settings_file = tmp_path / ".harness" / "settings.json"
        assert settings_file.is_file()

    def test_save_preserves_other_fields(self, _redirect_settings, tmp_path: Path):
        """save 只更新 trusted_directories，保留文件中已有的其他字段。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"theme": "dark", "trusted_directories": []}), encoding="utf-8"
        )

        tg.save_trusted_directories(["/new/dir"])

        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        assert data["trusted_directories"] == ["/new/dir"]

    def test_load_returns_empty_when_file_is_corrupted(self, _redirect_settings, tmp_path: Path):
        """JSON 格式损坏时返回空字典（不抛异常）。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{broken json!!", encoding="utf-8")
        assert tg.load_trusted_directories() == []

    def test_load_returns_empty_when_file_is_not_dict(self, _redirect_settings, tmp_path: Path):
        """文件顶层是数组而非字典时返回空列表。"""
        settings_file = tmp_path / ".harness" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("[1, 2, 3]", encoding="utf-8")
        assert tg.load_trusted_directories() == []


# ---------------------------------------------------------------------------
# is_trusted_directory
# ---------------------------------------------------------------------------


class TestIsTrustedDirectory:
    """判断路径是否在受信列表中。"""

    def test_returns_true_for_trusted_path(self, _redirect_settings, tmp_path: Path):
        """已添加到受信列表的路径应返回 True。"""
        target = tmp_path / "my-project"
        target.mkdir()
        tg.trust_directory(target)
        assert tg.is_trusted_directory(target) is True

    def test_returns_false_for_unknown_path(self, _redirect_settings, tmp_path: Path):
        """从未标记过的路径应返回 False。"""
        assert tg.is_trusted_directory(tmp_path / "never-seen") is False

    def test_uses_resolved_path_for_comparison(self, _redirect_settings, tmp_path: Path):
        """路径比较应基于 resolve() 后的绝对路径，而非原始字符串。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        # 使用带 .. 的等价路径查询，resolve 后应匹配
        equivalent = tmp_path / "subdir" / ".." / "project"
        assert tg.is_trusted_directory(equivalent) is True

    def test_accepts_both_str_and_path(self, _redirect_settings, tmp_path: Path):
        """接口同时接受 str 和 Path 类型参数。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(str(target))
        assert tg.is_trusted_directory(str(target)) is True
        assert tg.is_trusted_directory(target) is True


# ---------------------------------------------------------------------------
# trust_directory / untrust_directory
# ---------------------------------------------------------------------------


class TestTrustAndUntrust:
    """添加和移除受信目录。"""

    def test_trust_adds_to_list(self, _redirect_settings, tmp_path: Path):
        """trust_directory 应将路径添加到受信列表并持久化。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        assert str(target.resolve()) in tg.load_trusted_directories()

    def test_trust_is_idempotent(self, _redirect_settings, tmp_path: Path):
        """重复 trust 同一目录不应产生重复条目。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        tg.trust_directory(target)
        trusted = tg.load_trusted_directories()
        assert trusted.count(str(target.resolve())) == 1

    def test_trust_removes_from_untrusted_list(self, _redirect_settings, tmp_path: Path):
        """trust_directory 应同时将该目录从取消受信列表中移除。"""
        target = tmp_path / "project"
        target.mkdir()
        # 先 trust 再 untrust，使其进入取消受信列表
        tg.trust_directory(target)
        tg.untrust_directory(target)
        assert tg.get_directory_trust_status(target) == "untrusted"

        # 重新 trust 应清除取消受信标记
        tg.trust_directory(target)
        assert tg.get_directory_trust_status(target) == "trusted"

    def test_untrust_removes_from_list(self, _redirect_settings, tmp_path: Path):
        """untrust_directory 应将路径从受信列表移除。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        tg.untrust_directory(target)
        assert tg.is_trusted_directory(target) is False

    def test_untrust_is_idempotent(self, _redirect_settings, tmp_path: Path):
        """对不在受信列表中的目录调用 untrust 不应报错。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.untrust_directory(target)  # 从未 trust 过，不应抛异常
        assert tg.is_trusted_directory(target) is False

    def test_untrust_records_in_untrusted_list(self, _redirect_settings, tmp_path: Path):
        """untrust 后目录应被记录到取消受信列表，以便区分 unknown 状态。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        tg.untrust_directory(target)

        settings_file = tmp_path / ".harness" / "settings.json"
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert str(target.resolve()) in data.get("untrusted_directories", [])


# ---------------------------------------------------------------------------
# get_directory_trust_status
# ---------------------------------------------------------------------------


class TestGetDirectoryTrustStatus:
    """返回目录受信状态：trusted / untrusted / unknown。"""

    def test_returns_trusted_for_trusted_directory(self, _redirect_settings, tmp_path: Path):
        """受信列表中的目录状态为 trusted。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        assert tg.get_directory_trust_status(target) == "trusted"

    def test_returns_unknown_for_new_directory(self, _redirect_settings, tmp_path: Path):
        """从未标记过的目录状态为 unknown。"""
        assert tg.get_directory_trust_status(tmp_path / "new") == "unknown"

    def test_returns_untrusted_after_untrust(self, _redirect_settings, tmp_path: Path):
        """被 untrust 移除的目录状态为 untrusted（而非 unknown）。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        tg.untrust_directory(target)
        assert tg.get_directory_trust_status(target) == "untrusted"

    def test_status_transitions(self, _redirect_settings, tmp_path: Path):
        """验证状态完整流转：unknown → trusted → untrusted → trusted。"""
        target = tmp_path / "project"
        target.mkdir()

        assert tg.get_directory_trust_status(target) == "unknown"

        tg.trust_directory(target)
        assert tg.get_directory_trust_status(target) == "trusted"

        tg.untrust_directory(target)
        assert tg.get_directory_trust_status(target) == "untrusted"

        tg.trust_directory(target)
        assert tg.get_directory_trust_status(target) == "trusted"


# ---------------------------------------------------------------------------
# is_restricted_mode_for_untrusted
# ---------------------------------------------------------------------------


class TestIsRestrictedModeForUntrusted:
    """未受信目录的审批模式限制。"""

    def test_trusted_directory_never_restricted(self, _redirect_settings, tmp_path: Path):
        """受信目录在任何审批模式下都不受限制。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)

        for mode in ("default", "auto", "yolo"):
            restricted, reason = tg.is_restricted_mode_for_untrusted(mode, target)
            assert restricted is False
            assert reason is None

    def test_untrusted_yolo_is_restricted(self, _redirect_settings, tmp_path: Path):
        """未受信目录使用 yolo 模式时应被锁定为 default。"""
        target = tmp_path / "project"
        target.mkdir()
        restricted, reason = tg.is_restricted_mode_for_untrusted("yolo", target)
        assert restricted is True
        assert reason is not None
        assert "未受信" in reason

    def test_untrusted_auto_is_restricted(self, _redirect_settings, tmp_path: Path):
        """未受信目录使用 auto 模式时应被锁定为 default。"""
        target = tmp_path / "project"
        target.mkdir()
        restricted, reason = tg.is_restricted_mode_for_untrusted("auto", target)
        assert restricted is True
        assert "未受信" in reason

    def test_untrusted_default_mode_not_restricted(self, _redirect_settings, tmp_path: Path):
        """未受信目录使用 default 模式时无需额外限制（本身就是 default）。"""
        target = tmp_path / "project"
        target.mkdir()
        restricted, reason = tg.is_restricted_mode_for_untrusted("default", target)
        assert restricted is False
        assert reason is None


# ---------------------------------------------------------------------------
# should_hide_always_allow
# ---------------------------------------------------------------------------


class TestShouldHideAlwaysAllow:
    """未受信目录中隐藏 Always-allow 选项。"""

    def test_returns_true_for_untrusted_directory(self, _redirect_settings, tmp_path: Path):
        """未受信目录应隐藏 Always-allow 选项。"""
        target = tmp_path / "project"
        target.mkdir()
        # 从未 trust 过 → 不受信
        assert tg.should_hide_always_allow(target) is True

    def test_returns_false_for_trusted_directory(self, _redirect_settings, tmp_path: Path):
        """受信目录不应隐藏 Always-allow 选项。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        assert tg.should_hide_always_allow(target) is False

    def test_returns_true_for_explicitly_untrusted(self, _redirect_settings, tmp_path: Path):
        """被显式取消受信的目录同样应隐藏 Always-allow。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)
        tg.untrust_directory(target)
        assert tg.should_hide_always_allow(target) is True


# ---------------------------------------------------------------------------
# 持久化完整性
# ---------------------------------------------------------------------------


class TestPersistence:
    """验证 settings.json 的持久化行为。"""

    def test_data_survives_reload(self, _redirect_settings, tmp_path: Path):
        """写入后重新加载应得到相同结果。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)

        # 模拟重新加载（直接调用 load 函数，内部会重新读文件）
        loaded = tg.load_trusted_directories()
        assert str(target.resolve()) in loaded

    def test_settings_file_is_valid_json(self, _redirect_settings, tmp_path: Path):
        """写入后文件内容应为合法 JSON。"""
        target = tmp_path / "project"
        target.mkdir()
        tg.trust_directory(target)

        settings_file = tmp_path / ".harness" / "settings.json"
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "trusted_directories" in data

    def test_multiple_directories_persisted(self, _redirect_settings, tmp_path: Path):
        """多个受信目录应全部持久化。"""
        dirs = []
        for name in ("alpha", "beta", "gamma"):
            d = tmp_path / name
            d.mkdir()
            dirs.append(d)
            tg.trust_directory(d)

        loaded = tg.load_trusted_directories()
        for d in dirs:
            assert str(d.resolve()) in loaded

    def test_untrusted_list_coexists_with_trusted(self, _redirect_settings, tmp_path: Path):
        """受信列表和取消受信列表应在同一文件中共存，互不干扰。"""
        trusted_dir = tmp_path / "trusted-proj"
        trusted_dir.mkdir()
        untrusted_dir = tmp_path / "untrusted-proj"
        untrusted_dir.mkdir()

        tg.trust_directory(trusted_dir)
        tg.trust_directory(untrusted_dir)
        tg.untrust_directory(untrusted_dir)

        settings_file = tmp_path / ".harness" / "settings.json"
        data = json.loads(settings_file.read_text(encoding="utf-8"))

        assert str(trusted_dir.resolve()) in data["trusted_directories"]
        assert str(untrusted_dir.resolve()) not in data["trusted_directories"]
        assert str(untrusted_dir.resolve()) in data["untrusted_directories"]
