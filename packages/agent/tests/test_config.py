"""Tests for the OpenAI-compatible configuration contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from za38_agent.config import ConfigError, load_config
from za38_agent.config import ModelSettings
from za38_agent.providers.za38_gateway import create_openai_compatible_model


def _write_config(path: Path, *, name: str, base_url: str, api_key_env: str = "ZA38_API_KEY") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''[model]
provider = "openai-compatible"
name = "{name}"
base_url = "{base_url}"
api_key_env = "{api_key_env}"

[model.headers]
X-Client = "za38"

[model.headers_env]
X-Tenant = "ZA38_TENANT"
''',
        encoding="utf-8",
    )


def test_config_precedence_and_redaction(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    explicit = tmp_path / "explicit.toml"
    _write_config(home / ".za38" / "config.toml", name="user", base_url="https://user.example/v1")
    _write_config(workspace / ".za38" / "config.toml", name="project", base_url="https://project.example/v1")
    _write_config(explicit, name="explicit", base_url="https://explicit.example/v1", api_key_env="EXPLICIT_KEY")

    config = load_config(
        workspace=workspace,
        home=home,
        config_path=explicit,
        environ={"ZA38_MODEL": "environment", "ZA38_BASE_URL": "https://env.example/v1", "EXPLICIT_KEY": "secret", "ZA38_TENANT": "team-a"},
    )

    model = config.require_model()
    assert model.name == "explicit"
    assert model.base_url == "https://explicit.example/v1"
    assert model.resolve_headers({"ZA38_TENANT": "team-a"})["X-Tenant"] == "team-a"
    view = config.redacted({"EXPLICIT_KEY": "secret"})
    assert view["model"]["api_key_configured"] is True
    assert "secret" not in str(view)


def test_config_requires_complete_model_table(tmp_path: Path):
    path = tmp_path / ".za38" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[model]\nname = 'missing-url'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="base_url"):
        load_config(workspace=tmp_path, home=tmp_path / "home")


def test_openai_compatible_adapter_is_constructed_without_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZA38_TEST_KEY", "test-key")
    model = create_openai_compatible_model(
        ModelSettings(
            name="enterprise-model",
            base_url="https://gateway.example.internal/v1",
            api_key_env="ZA38_TEST_KEY",
        )
    )
    assert model.model_name == "enterprise-model"
