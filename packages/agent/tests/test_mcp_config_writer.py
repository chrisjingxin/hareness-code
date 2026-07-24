"""mcp_config_writer 模块的单元测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from harness_agent.mcp_config_writer import (
    add_server_to_config,
    list_servers_in_config,
    remove_server_from_config,
)


class TestAddServerToConfig:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        server = {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "server"]}
        add_server_to_config(server, config_path=config)
        assert config.is_file()
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        assert data["mcp"]["servers"][0]["name"] == "fs"

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[model]\nprofile = "fast"\n', encoding="utf-8")
        server = {"name": "gh", "transport": "http", "url": "http://localhost:3001"}
        add_server_to_config(server, config_path=config)
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        assert data["model"]["profile"] == "fast"
        assert data["mcp"]["servers"][0]["name"] == "gh"

    def test_rejects_duplicate_name(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        server = {"name": "fs", "transport": "stdio", "command": "npx"}
        add_server_to_config(server, config_path=config)
        with pytest.raises(ValueError, match="already exists"):
            add_server_to_config(server, config_path=config)

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config = tmp_path / "sub" / "dir" / "config.toml"
        server = {"name": "fs", "transport": "stdio", "command": "npx"}
        add_server_to_config(server, config_path=config)
        assert config.is_file()


class TestRemoveServerFromConfig:
    def test_removes_existing_server(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        add_server_to_config({"name": "fs", "transport": "stdio", "command": "npx"}, config_path=config)
        add_server_to_config({"name": "gh", "transport": "http", "url": "http://x"}, config_path=config)
        remove_server_from_config("fs", config_path=config)
        servers = list_servers_in_config(config_path=config)
        assert len(servers) == 1
        assert servers[0]["name"] == "gh"

    def test_raises_for_missing_server(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="not found"):
            remove_server_from_config("nonexistent", config_path=config)

    def test_preserves_other_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[model]\nprofile = "fast"\n', encoding="utf-8")
        add_server_to_config({"name": "fs", "transport": "stdio", "command": "npx"}, config_path=config)
        remove_server_from_config("fs", config_path=config)
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        assert data["model"]["profile"] == "fast"


class TestListServersInConfig:
    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert list_servers_in_config(config_path=tmp_path / "missing.toml") == []

    def test_returns_all_servers(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        add_server_to_config({"name": "a", "transport": "stdio", "command": "x"}, config_path=config)
        add_server_to_config({"name": "b", "transport": "http", "url": "http://y"}, config_path=config)
        servers = list_servers_in_config(config_path=config)
        assert [s["name"] for s in servers] == ["a", "b"]
