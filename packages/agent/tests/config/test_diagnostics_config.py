"""[diagnostics] 配置、环境优先级和交互修改范围测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.config.config import ConfigError, load_config
from harness_agent.config.config_change_service import ConfigChange, ConfigChangeService


def test_diagnostics_defaults_toml_and_environment_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '''[config]
version = 1

[diagnostics]
level = "warn"
retention_days = 30
max_total_mib = 128
max_file_mib = 8
''',
        encoding="utf-8",
    )
    config = load_config(
        workspace=tmp_path / "workspace",
        home=home,
        environ={"HARNESS_LOG_LEVEL": "debug"},
    )
    assert config.diagnostics.level == "debug"
    assert config.diagnostics.retention_days == 30
    assert config.diagnostics.max_total_mib == 128
    assert config.diagnostics.max_file_mib == 8
    assert config.sources["diagnostics"] == "environment"


@pytest.mark.parametrize(
    "body",
    [
        'level = "verbose"',
        "retention_days = 0",
        "max_total_mib = 8",
        "max_file_mib = 257",
        "max_total_mib = 16\nmax_file_mib = 32",
    ],
)
def test_diagnostics_rejects_invalid_values(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[config]\nversion = 1\n\n[diagnostics]\n{body}\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(workspace=tmp_path / "workspace", home=tmp_path / "home", config_path=path, environ={})


def test_diagnostics_changes_apply_on_restart(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[config]\nversion = 1\n', encoding="utf-8")
    service = ConfigChangeService(workspace=tmp_path / "workspace", home=home, environ={})
    details = service.details()
    diagnostics = [field for field in details["fields"] if field["path"].startswith("diagnostics.")]
    assert len(diagnostics) == 4
    assert all(field["applies_to"] == "restart" for field in diagnostics)
    preview = service.preview([ConfigChange("diagnostics.level", "error")])
    assert preview.applies_to == ("restart",)
