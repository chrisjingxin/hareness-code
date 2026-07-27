"""MCP 服务器配置的 TOML 文件读写。

负责在用户级 ``~/.harness/config.toml`` 中持久化 ``[[mcp.servers]]`` 条目，
使用原子写入（临时文件 + rename）避免写入中断导致配置损坏。
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

logger = logging.getLogger(__name__)


def _default_config_path() -> Path:
    """返回默认用户配置文件路径 ``~/.harness/config.toml``。"""
    return Path.home() / ".harness" / "config.toml"


def add_server_to_config(
    server: dict[str, Any],
    config_path: Path | None = None,
) -> Path:
    """将一个 MCP 服务器条目追加到用户 TOML 配置文件。

    Args:
        server: 服务器配置字典，必须包含 ``name`` 和 ``transport`` 键。
        config_path: 配置文件路径，默认 ``~/.harness/config.toml``。

    Returns:
        实际写入的配置文件路径。

    Raises:
        ValueError: 同名服务器已存在。
    """
    path = config_path or _default_config_path()
    data = _read_toml(path)

    # 检查重复
    mcp_section = data.setdefault("mcp", {})
    servers = mcp_section.setdefault("servers", [])
    name = server["name"]
    for existing in servers:
        if existing.get("name") == name:
            raise ValueError(f"MCP server '{name}' already exists in {path}")

    servers.append(server)
    _write_toml_atomic(path, data)
    logger.info("MCP server '%s' added to %s", name, path)
    return path


def remove_server_from_config(
    name: str,
    config_path: Path | None = None,
) -> Path:
    """从用户 TOML 配置文件中删除指定名称的 MCP 服务器。

    Args:
        name: 要删除的服务器名称。
        config_path: 配置文件路径，默认 ``~/.harness/config.toml``。

    Returns:
        实际写入的配置文件路径。

    Raises:
        ValueError: 指定名称的服务器不存在。
    """
    path = config_path or _default_config_path()
    data = _read_toml(path)

    mcp_section = data.get("mcp", {})
    servers = mcp_section.get("servers", [])
    original_count = len(servers)
    mcp_section["servers"] = [s for s in servers if s.get("name") != name]

    if len(mcp_section["servers"]) == original_count:
        raise ValueError(f"MCP server '{name}' not found in {path}")

    data["mcp"] = mcp_section
    _write_toml_atomic(path, data)
    logger.info("MCP server '%s' removed from %s", name, path)
    return path


def list_servers_in_config(
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """列出配置文件中所有已配置的 MCP 服务器。"""
    path = config_path or _default_config_path()
    data = _read_toml(path)
    return data.get("mcp", {}).get("servers", [])


def _read_toml(path: Path) -> dict[str, Any]:
    """读取 TOML 文件，文件不存在时返回空字典。"""
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc


def _write_toml_atomic(path: Path, data: dict[str, Any]) -> None:
    """原子写入 TOML：先写临时文件再 rename，避免中断损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = tomli_w.dumps(data)
    # 在同一目录下创建临时文件，确保 rename 是原子操作
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
