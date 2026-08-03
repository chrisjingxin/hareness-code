"""子代理发现与注册表测试。"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from harness_agent.subagents import (
    AgentColor,
    AgentDefinition,
    AgentRegistry,
    AgentSource,
    BUILTIN_AGENTS,
    discover_agents,
)


def _write_agent(root: Path, filename: str, content: str) -> Path:
    """将 Agent 定义写入临时目录并返回路径。"""
    agents_dir = root / ".harness" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


VALID_AGENT_MD = """\
---
name: test-agent
description: A test agent
---

Test system prompt
"""


def test_discover_project_agents(tmp_path: Path):
    """在项目目录下发现 agent 定义文件。"""
    workspace = tmp_path / "project"
    user_home = tmp_path / "home"
    _write_agent(workspace, "test-agent.md", VALID_AGENT_MD)

    agents = discover_agents(workspace, user_home)

    assert len(agents) == 1
    assert agents[0].name == "test-agent"
    assert agents[0].source == AgentSource.PROJECT
    assert agents[0].system_prompt == "Test system prompt"


def test_discover_user_agents(tmp_path: Path):
    """在用户目录下发现 agent 定义文件。"""
    workspace = tmp_path / "project"
    user_home = tmp_path / "home"
    _write_agent(user_home, "user-agent.md", """\
---
name: user-agent
description: A user agent
---

User prompt
""")

    agents = discover_agents(workspace, user_home)

    assert len(agents) == 1
    assert agents[0].name == "user-agent"
    assert agents[0].source == AgentSource.USER


def test_discover_missing_dir(tmp_path: Path):
    """目录不存在时返回空列表。"""
    workspace = tmp_path / "no-project"
    user_home = tmp_path / "no-home"

    agents = discover_agents(workspace, user_home)

    assert agents == []


def test_discover_invalid_file_skipped(tmp_path: Path):
    """解析失败的文件被跳过且不抛异常。"""
    workspace = tmp_path / "project"
    user_home = tmp_path / "home"
    _write_agent(workspace, "bad.md", "这不是有效的 frontmatter 格式")
    _write_agent(workspace, "good.md", VALID_AGENT_MD)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        agents = discover_agents(workspace, user_home)

    assert len(agents) == 1
    assert agents[0].name == "test-agent"
    assert len(w) == 1
    assert "跳过无法解析" in str(w[0].message)


def test_registry_priority(tmp_path: Path):
    """project 覆盖 user 覆盖 builtin。"""
    workspace = tmp_path / "project"
    user_home = tmp_path / "home"

    # 项目和用户目录都定义同名 agent
    _write_agent(workspace, "explore.md", """\
---
name: explore
description: Project override
---

Project explore prompt
""")
    _write_agent(user_home, "explore.md", """\
---
name: explore
description: User override
---

User explore prompt
""")

    registry = AgentRegistry()
    registry.load(workspace, user_home)

    explore = registry.get("explore")
    assert explore is not None
    assert explore.description == "Project override"
    assert explore.source == AgentSource.PROJECT


def test_registry_crud():
    """register/get/list/unregister 基本操作。"""
    registry = AgentRegistry()

    defn = AgentDefinition(
        name="custom",
        description="自定义代理",
        system_prompt="你是自定义代理。",
        source=AgentSource.PROJECT,
    )

    # register + get
    registry.register(defn)
    assert registry.get("custom") is defn

    # list
    assert defn in registry.list()

    # unregister 存在
    assert registry.unregister("custom") is True
    assert registry.get("custom") is None

    # unregister 不存在
    assert registry.unregister("custom") is False


def test_registry_builtin_loaded(tmp_path: Path):
    """load 后包含 general-purpose/explore/plan。"""
    registry = AgentRegistry()
    registry.load(None, tmp_path / "empty-home")

    names = {a.name for a in registry.list()}
    assert "general-purpose" in names
    assert "explore" in names
    assert "plan" in names

    # 验证内置属性
    gp = registry.get("general-purpose")
    assert gp is not None
    assert gp.source == AgentSource.BUILTIN
    assert gp.color == AgentColor.BLUE
